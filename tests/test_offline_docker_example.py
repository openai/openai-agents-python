import asyncio
import json
import os
import subprocess
import sys
import threading
from uuid import uuid4

import pytest

from examples.offline_docker import create_offline_job, open_exec_stdio

CONTAINER_ID = "ba" * 32
EXEC_SERVER = "/usr/local/bin/exec-server"


@pytest.fixture
def docker_cli(monkeypatch):
    """Supply daemon responses that a real offline job cannot produce on demand."""
    calls = []
    output = {
        "create": CONTAINER_ID.encode() + b"\n",
        "start": CONTAINER_ID.encode() + b"\n",
        "inspect": json.dumps([CONTAINER_ID, True, "none", {"none": {}}]).encode(),
        "rm": b"",
    }

    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == "docker"
        assert kwargs["stderr"] == subprocess.DEVNULL
        data = output[command[1]]
        return subprocess.CompletedProcess(command, int(data is None), stdout=data)

    launch = asyncio.create_subprocess_exec

    async def exec_stream(*command, **kwargs):
        calls.append(list(command))
        # Use real pipes and a real process so relay teardown has observable effects.
        script = (
            "import sys, time\n"
            "print('private diagnostic', file=sys.stderr)\n"
            "for line in sys.stdin.buffer:\n"
            "    sys.stdout.buffer.write(b'reply ' + line)\n"
            "    if line == b'stall\\n': time.sleep(60)\n"
            "    if line == b'flood\\n':\n"
            "        sys.stdout.buffer.write(bytes(12 << 20))\n"
            "        time.sleep(60)\n"
        )
        return await launch(sys.executable, "-uc", script, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stream)
    return calls, output


async def exchange(process, message):
    process.stdin.write(message + b"\n")
    await process.stdin.drain()
    return await asyncio.wait_for(process.stdout.readline(), 10)


async def test_create_and_recheck_before_each_exec(docker_cli):
    calls, output = docker_cli
    job = create_offline_job("job:test")
    assert job == CONTAINER_ID
    assert calls[0] == [
        "docker",
        "create",
        "--pull=never",
        "--network",
        "none",
        "--entrypoint",
        "/bin/sleep",
        "--",
        "job:test",
        "infinity",
    ]
    assert calls[1] == ["docker", "start", job]
    output["inspect"] = json.dumps([job, True, "none", {}]).encode()
    async with open_exec_stdio(job) as process:
        assert await exchange(process, b"challenge") == b"reply challenge\n"
    assert process.returncode == 0 and process.stdout.at_eof()
    assert calls[-1] == ["docker", "exec", "-i", job, EXEC_SERVER, "stdio"]

    output["inspect"] = json.dumps([job, True, "none", {"none": {}, "bridge": {}}]).encode()
    with pytest.raises(RuntimeError, match="network mode"):
        async with open_exec_stdio(job):
            pytest.fail("Reattached to a job with another network")
    assert [call[1] for call in calls] == [
        "create",
        "start",
        "inspect",
        "inspect",
        "exec",
        "inspect",
    ]
    assert all(call[-1] == job for call in calls if call[1] == "inspect")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ([CONTAINER_ID, True, "bridge", {}], "network mode"),
        ([CONTAINER_ID, True, None, {}], "network mode"),
        ([CONTAINER_ID, True, "none", None], "network mode"),
        ([CONTAINER_ID, True, "none", []], "network mode"),
        ([CONTAINER_ID, False, "none", {}], "not running"),
        (["cd" * 32, True, "none", {}], "different container"),
        (None, "inspect failed"),
    ],
)
async def test_failed_restore_only_inspects(docker_cli, fields, message):
    calls, output = docker_cli
    output["inspect"] = None if fields is None else json.dumps(fields).encode()
    with pytest.raises(RuntimeError, match=message):
        async with open_exec_stdio(CONTAINER_ID):
            pytest.fail("Opened an unverified job")
    assert [call[1] for call in calls] == ["inspect"]


def test_shutdown_joins_inspection_before_propagating_cancellation(monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    async def attach():
        try:
            async with open_exec_stdio(CONTAINER_ID):
                pytest.fail("Entered a cancelled attachment")
        except asyncio.CancelledError as error:
            return error.args, finished.is_set()

    async def main():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def inspect(container_id):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10)
            finished.set()

        monkeypatch.setattr("examples.offline_docker._inspect", inspect)
        attachment = asyncio.create_task(attach())
        await asyncio.wait_for(started.wait(), 10)
        # Let asyncio.run release the worker when it cancels this sibling task.
        sibling = asyncio.create_task(asyncio.sleep(10))
        sibling.add_done_callback(lambda _: release.set())
        attachment.cancel("client disconnected")
        await asyncio.sleep(0)
        return attachment

    assert asyncio.run(main()).result() == (("client disconnected",), True)


@pytest.mark.parametrize(("cancel_count", "inspect_fails"), [(1, False), (2, True)])
async def test_cancelled_attachment_joins_inspection(
    docker_cli, monkeypatch, cancel_count, inspect_fails
):
    calls, output = docker_cli
    if inspect_fails:
        output["inspect"] = None
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    run = subprocess.run

    def paused_inspect(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10)
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", paused_inspect)

    async def attach():
        async with open_exec_stdio(CONTAINER_ID):
            pytest.fail("Entered a cancelled attachment")

    attachment = asyncio.create_task(attach())
    try:
        await asyncio.wait_for(started.wait(), 10)
        for _ in range(cancel_count):
            attachment.cancel()
            await asyncio.sleep(0)
            assert not attachment.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(attachment, 10)
    assert [call[1] for call in calls] == ["inspect"]


@pytest.mark.parametrize("selector", [CONTAINER_ID[:12], "--privileged"])
async def test_reject_ambiguous_selectors_before_docker(docker_cli, selector):
    calls, _ = docker_cli
    with pytest.raises(ValueError, match="full Docker container ID"):
        async with open_exec_stdio(selector):
            pytest.fail("Accepted an ambiguous selector")
    assert not calls


@pytest.mark.parametrize("failure", ["start", "inspect"])
def test_failed_setup_only_removes_new_job_and_preserves_error(docker_cli, failure):
    calls, output = docker_cli
    output["start" if failure == "start" else "rm"] = None
    output["inspect"] = json.dumps([CONTAINER_ID, True, "bridge", {}]).encode()
    with pytest.raises(
        RuntimeError, match="start failed" if failure == "start" else "network mode"
    ):
        create_offline_job("job:test")
    assert calls[-1] == ["docker", "rm", "-f", CONTAINER_ID]
    assert sum(call[1] == "create" for call in calls) == 1


def test_ambiguous_create_cannot_select_a_cleanup_target(docker_cli):
    calls, output = docker_cli
    output["create"] = b""
    with pytest.raises(ValueError, match="full Docker container ID"):
        create_offline_job("--privileged")
    assert len(calls) == 1
    assert calls[0][-3:] == ["--", "--privileged", "infinity"]


async def test_disconnect_reaps_stalled_cli_and_joins_pipes(docker_cli):
    with pytest.raises(ConnectionError):
        async with open_exec_stdio(CONTAINER_ID) as process:
            assert await exchange(process, b"stall") == b"reply stall\n"
            raise ConnectionError("gateway disconnected")
    assert process.returncode and process.stdin.is_closing() and process.stdout.at_eof()


async def test_cancelled_cleanup_reaps_cli_and_closes_full_pipe(docker_cli, monkeypatch):
    attachment = open_exec_stdio(CONTAINER_ID)
    process = await attachment.__aenter__()
    kill = process.kill
    try:
        assert await exchange(process, b"flood") == b"reply flood\n"
        pipe = process.stdout._transport.get_extra_info("pipe")
        close = asyncio.create_task(attachment.__aexit__(None, None, None))

        def kill_then_cancel():
            kill()
            asyncio.get_running_loop().call_soon(close.cancel)

        monkeypatch.setattr(process, "kill", kill_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close, 10)
        assert process.returncode and process.stdin.is_closing()
        assert process.stdout.at_eof()
        with pytest.raises(ValueError):
            pipe.fileno()
    finally:
        if process.returncode is None:
            kill()
        await process.communicate()


_SOCKET_PROBE = """#!/usr/bin/python3
import errno, socket, sys
assert sys.argv[1:] == ["stdio"]
line = sys.stdin.buffer.readline().strip()
with socket.socket(socket.AF_UNIX) as local:
    if line == b"listen":
        local.bind("/tmp/job.sock")
        local.listen()
        print("ready", flush=True)
        for _ in range(2):
            conn, _ = local.accept()
            with conn, conn.makefile("rb") as stream:
                conn.sendall(b"reply " + stream.readline())
    else:
        address, challenge = line.split()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.settimeout(2)
            assert tcp.connect_ex((address.decode(), 443)) == errno.ENETUNREACH
        local.connect("/tmp/job.sock")
        local.sendall(challenge + b"\\n")
        with local.makefile("rb") as stream:
            sys.stdout.buffer.write(stream.readline())
"""


@pytest.mark.skipif(
    not os.environ.get("OFFLINE_DOCKER_TEST_IMAGE"), reason="requires OFFLINE_DOCKER_TEST_IMAGE"
)
async def test_real_docker_reconnect_to_private_unix_socket(tmp_path):
    """The opt-in Linux image needs /bin/sleep, /usr/bin/python3 and /usr/local/bin."""
    probe = tmp_path / "exec-server"
    probe.write_text(_SOCKET_PROBE, encoding="utf-8")
    probe.chmod(0o755)
    bridge = subprocess.check_output(
        ["docker", "network", "inspect", "--format", "{{(index .IPAM.Config 0).Gateway}}", "bridge"]
    ).strip()
    job = create_offline_job(os.environ["OFFLINE_DOCKER_TEST_IMAGE"])
    try:
        subprocess.run(["docker", "cp", str(probe), f"{job}:{EXEC_SERVER}"], check=True)
        async with open_exec_stdio(job) as peer:
            assert await exchange(peer, b"listen") == b"ready\n"
            for _ in range(2):
                async with open_exec_stdio(job) as client:
                    challenge = uuid4().hex.encode()
                    assert await exchange(client, bridge + b" " + challenge) == (
                        b"reply " + challenge + b"\n"
                    )
    finally:
        subprocess.run(["docker", "rm", "-f", job], check=True, stdout=subprocess.DEVNULL)

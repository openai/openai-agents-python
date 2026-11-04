"""Hosted qualification of the installed SDK's public local Session on macOS."""

import asyncio
import contextlib
import errno
import os
from pathlib import Path
import platform
import plistlib
import re
import selectors
import shlex
import shutil
import stat
import struct
import subprocess
import tempfile
import time
from uuid import uuid4

import pytest

from agents import Agent, Environment, NativeProvider
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.native_fixtures import responses as responses


def launchctl(*args):
    return subprocess.run(
        ["/bin/launchctl", *map(str, args)], capture_output=True, text=True, timeout=5
    )


def remove_job(service):
    result = launchctl("bootout", service)
    if result.returncode:
        assert launchctl("print", service).returncode != 0, result.stderr


def write_job(workspace, domain, marker, *, run_at_load=True):
    label = f"org.openai.sdk.seatbelt-delegation.{uuid4().hex}"
    path = workspace / f"{label}.plist"
    spec = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/touch", str(marker)],
        "RunAtLoad": run_at_load,
        "KeepAlive": False,
        "ExitTimeOut": 2,
    }
    if domain.startswith("user/"):
        spec["LimitLoadToSessionType"] = "Background"
    path.write_bytes(plistlib.dumps(spec))
    return path, f"{domain}/{label}"


@contextlib.contextmanager
def nonce_directory():
    root = Path("/tmp").resolve() / f"codex-daemon-{os.geteuid()}"
    made_root = False
    try:
        try:
            root.mkdir(mode=0o700)
            made_root = True
        except FileExistsError:
            pass
        metadata = root.lstat()
        assert stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        with tempfile.TemporaryDirectory(prefix="sdk-seatbelt-", dir=root) as name:
            yield Path(name)
    finally:
        if made_root:
            try:
                root.rmdir()
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise


@contextlib.contextmanager
def trusted_control(executable):
    control = subprocess.Popen(
        [executable, "control"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0
    )
    try:
        yield control
    finally:
        try:
            control.stdin.close()
            try:
                control.wait(timeout=2)
            except subprocess.TimeoutExpired:
                control.kill()
                control.wait(timeout=2)
        finally:
            control.stdout.close()


def read_control(control, size):
    data = b""
    deadline = time.monotonic() + 2
    with selectors.SelectSelector() as events:
        events.register(control.stdout, selectors.EVENT_READ)
        while len(data) < size:
            assert events.select(max(0, deadline - time.monotonic())), "canary read timed out"
            chunk = os.read(control.stdout.fileno(), size - len(data))
            assert chunk, "trusted control stopped early"
            data += chunk
    return data


def ping_control(control, command=b"p"):
    assert control.stdin.write(command) == 1
    return read_control(control, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_nonce_directory", [False, True], ids=["workspace", "nonce-grant"])
async def test_sdk_seatbelt_delegation(responses, tmp_path, grant_nonce_directory):
    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    assert os.getuid() == os.geteuid() != 0
    kind = os.environ.get("SDK_SEATBELT_DOMAIN", "gui")
    assert kind in {"gui", "user"}
    domain = f"{kind}/{os.geteuid()}"
    private = tmp_path.resolve()
    workspace = private / "workspace"
    workspace.mkdir(mode=0o700)
    trusted = private / "trusted-probe"
    probe = workspace / "probe"
    subprocess.run(
        ["/usr/bin/clang", "-std=c11", "-D_DARWIN_C_SOURCE", "-Wall", "-Wextra", "-Werror",
         str(Path(__file__).resolve().with_name("probe.c")), "-o", str(trusted)],
        check=True,
        timeout=20,
    )
    shutil.copy2(trusted, probe)

    with contextlib.ExitStack() as cleanup:
        protected = cleanup.enter_context(nonce_directory())
        nonce = protected / "nonce"
        original = os.urandom(32)
        nonce.write_bytes(original)
        nonce.chmod(0o600)
        positive_marker = private / "trusted.marker"
        untrusted_marker = private / "delegated.marker"
        kickstart_marker = workspace / "kickstarted.marker"
        positive_plist, positive_service = write_job(workspace, domain, positive_marker)
        untrusted_plist, untrusted_service = write_job(workspace, domain, untrusted_marker)
        kickstart_plist, kickstart_service = write_job(
            workspace, domain, kickstart_marker, run_at_load=False
        )
        cleanup.callback(remove_job, positive_service)
        cleanup.callback(remove_job, untrusted_service)
        cleanup.callback(remove_job, kickstart_service)
        print(f"trusted service {positive_service}; attempted bootstrap {untrusted_service}; "
              f"attempted kickstart {kickstart_service}")
        result = launchctl("bootstrap", domain, positive_plist)
        assert result.returncode == 0, f"trusted launchctl bootstrap failed: {result.stderr}"
        async with asyncio.timeout(4):
            while not positive_marker.exists():
                await asyncio.sleep(0.025)
        assert not untrusted_marker.exists()
        result = launchctl("bootstrap", domain, kickstart_plist)
        assert result.returncode == 0, f"trusted idle service registration failed: {result.stderr}"
        idle = launchctl("print", kickstart_service)
        assert idle.returncode == 0, idle.stderr
        assert re.search(r"^\s*pid = \d+", idle.stdout, re.MULTILINE) is None
        assert not kickstart_marker.exists()

        control = cleanup.enter_context(trusted_control(trusted))
        token = struct.unpack("=8I", read_control(control, 32))
        assert token[1] == token[3] == os.getuid() and token[5] == control.pid
        words = list(map(str, token))
        signal_check = subprocess.run(
            [trusted, "signal", *words], capture_output=True, text=True, timeout=3
        )
        assert signal_check.returncode == 0, signal_check.stdout + signal_check.stderr
        async with asyncio.timeout(2):
            while ping_control(control) != b"1":
                await asyncio.sleep(0.025)
        assert ping_control(control, b"c") == b"0"

        command = (
            "rc=0; "
            + shlex.join([str(probe), "probe", domain, str(untrusted_plist), str(nonce), *words])
            + ' || rc=$?; printf "bootstrap_exit=%s\n" "$rc"; rc=0; '
            + shlex.join([str(probe), "kickstart", kickstart_service])
            + ' || rc=$?; printf "kickstart_exit=%s\n" "$rc"'
        )
        responses.respond(
            function_call("exec_command", {"cmd": command, "login": False}, "seatbelt-probe")
        )
        responses.respond(assistant_output("probe complete"))
        roots = [protected] if grant_nonce_directory else []
        environment = Environment.local(
            cwd=workspace, writable_roots=roots, tools=["exec_command"]
        )
        agent = Agent(
            instructions="Use the selected tools.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
        )
        print(f"workspace={workspace}; extra writable roots={roots}; protected nonce={nonce}")
        async with asyncio.timeout(28), contextlib.AsyncExitStack() as sessions:
            try:
                session = await sessions.enter_async_context(agent.session(environment=environment))
            except (RuntimeError, ValueError) as exc:
                if grant_nonce_directory and re.search(
                    r"(reserved|protected).*daemon|daemon.*(reserved|protected)", str(exc), re.I
                ):
                    assert not responses.requests
                    pytest.skip(f"SDK rejected the reserved synthetic daemon grant before dispatch: {exc}")
                raise
            turn = await session.submit("Run the Seatbelt qualification command.")
            assert (await turn.get_result()).final_output == "probe complete"

        assert len(responses.requests) == 2
        output = tool_output(responses.requests[-1], "seatbelt-probe")
        print(output)
        assert (workspace / "workspace-proof").read_text() == "ok\n"
        assert "inherited_bootstrap=present" in output.splitlines()
        assert "kickstart_inherited_bootstrap=present" in output.splitlines()
        statuses = dict(re.findall(
            r"^(nonce_read_errno|nonce_write_errno|audit_signal_errno|bootstrap_exit|kickstart_exit)=(\d+)$",
            output,
            re.MULTILINE,
        ))
        assert set(statuses) == {
            "nonce_read_errno", "nonce_write_errno", "audit_signal_errno",
            "bootstrap_exit", "kickstart_exit",
        }, output
        denied = {errno.EPERM, errno.EACCES}
        assert int(statuses["nonce_read_errno"]) in denied, output
        assert int(statuses["nonce_write_errno"]) in denied, output
        assert int(statuses["audit_signal_errno"]) in denied, output
        assert 0 < int(statuses["bootstrap_exit"]) < 125, output
        assert nonce.read_bytes() == original
        assert ping_control(control) == b"0"
        assert launchctl("print", positive_service).returncode == 0
        assert launchctl("print", untrusted_service).returncode != 0
        assert not untrusted_marker.exists()
        print("PASS: launchd bootstrap, nonce access and audit-token signal were denied")

        # The attempted service stays registered, but no job run or marker may
        # appear until the trusted host tries the same command.
        assert 0 < int(statuses["kickstart_exit"]) < 125, output
        await asyncio.sleep(0.5)
        still_idle = launchctl("print", kickstart_service)
        assert still_idle.returncode == 0, still_idle.stderr
        assert re.search(r"^\s*pid = \d+", still_idle.stdout, re.MULTILINE) is None
        assert re.findall(r"^\s*runs = (\d+)", idle.stdout, re.MULTILINE) == re.findall(
            r"^\s*runs = (\d+)", still_idle.stdout, re.MULTILINE
        ), "kickstart changed the service run count"
        assert not kickstart_marker.exists(), "sandboxed kickstart ran the idle job"
        result = launchctl("kickstart", kickstart_service)
        if result.returncode != 0:
            pytest.fail(
                f"Trusted ordinary-user kickstart control failed with exit {result.returncode}. "
                f"Sandbox-specific kickstart denial is unqualified. "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        async with asyncio.timeout(4):
            while not kickstart_marker.exists():
                await asyncio.sleep(0.025)
        print("PASS: sandboxed kickstart left the job idle; trusted kickstart ran it")

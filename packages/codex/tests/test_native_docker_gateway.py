"""Opt-in local Docker qualification with an existing image and Linux fixture.

Set CODEX_DOCKER_TEST_IMAGE and CODEX_DOCKER_TEST_SOCKET explicitly. The
CODEX_EXEC_SERVER_TEST_BINARY fixture and its existing glibc libraries are
copied into a fresh owned container; no image pull, host mount or extra
container capability is allowed.
"""

import asyncio
import io
import json
import os
import sys
import tarfile
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agents import Agent
from agents.classic.sandbox.entries import File
from agents.classic.sandbox.util.blocking_io import run_blocking_workspace_io
from agents.sandbox import DockerGateway, Manifest, sandbox_environment
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider

docker = pytest.importorskip("docker")
provider_module = pytest.importorskip("agents.classic.sandbox.sandboxes.docker")
DockerSandboxClient = provider_module.DockerSandboxClient
DockerSandboxClientOptions = provider_module.DockerSandboxClientOptions
NotFound = docker.errors.NotFound
IMAGE = os.environ.get("CODEX_DOCKER_TEST_IMAGE")
SOCKET = os.environ.get("CODEX_DOCKER_TEST_SOCKET")
BINARY = Path(os.environ.get("CODEX_EXEC_SERVER_TEST_BINARY", "/missing-exec-server-fixture"))
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not IMAGE or not SOCKET or not BINARY.is_file(),
    reason="requires an explicit existing Docker image, Unix socket and Linux exec-server fixture",
)


def fixture_archive():
    data = io.BytesIO()
    files = {
        "usr/local/bin/exec-server": BINARY,
        "lib64/ld-linux-x86-64.so.2": Path("/lib64/ld-linux-x86-64.so.2"),
        **{
            "lib/x86_64-linux-gnu/" + name: Path("/lib/x86_64-linux-gnu") / name
            for name in ("libssl.so.3", "libcrypto.so.3", "libgcc_s.so.1", "libm.so.6", "libc.so.6")
        },
    }
    with tarfile.open(fileobj=data, mode="w", dereference=True) as archive:
        for name in ("usr/local/bin", "lib64", "lib/x86_64-linux-gnu"):
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(info)
        for name, path in files.items():
            archive.add(path, arcname=name, recursive=False)
        helper = tarfile.TarInfo("usr/local/bin/codex-linux-sandbox")
        helper.type, helper.linkname = tarfile.SYMTYPE, "exec-server"
        archive.addfile(helper)
    return data.getvalue()


def owned_cli_pids(cid):
    children = set()
    for task in Path("/proc/self/task").iterdir():
        try:
            children.update(int(pid) for pid in (task / "children").read_text().split())
        except FileNotFoundError:
            pass
    result = set()
    for pid in children:
        try:
            if cid.encode() in Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0"):
                result.add(pid)
        except FileNotFoundError:
            pass
    return result


class FixtureClient(DockerSandboxClient):
    def __init__(self, connection):
        super().__init__(connection)
        self.created_ids = []
        self.deleted_ids = []
        self.helper_pids = set()

    def image_exists(self, image):
        self.docker_client.images.get(image)
        return True

    async def create(self, **options):
        resource = await super().create(**options)
        cid = resource.state.container_id
        self.created_ids.append(cid)
        inspected = await run_blocking_workspace_io(
            lambda: self.docker_client.api.inspect_container(cid)
        )
        assert inspected["HostConfig"]["NetworkMode"] == "none"
        assert inspected["HostConfig"]["Privileged"] is False
        assert not inspected["HostConfig"].get("CapAdd")
        assert not inspected["HostConfig"].get("Devices")
        assert not inspected["HostConfig"].get("SecurityOpt")
        assert inspected["Mounts"] == []
        try:
            payload = await run_blocking_workspace_io(fixture_archive)
            await run_blocking_workspace_io(
                lambda: self.docker_client.api.put_archive(cid, "/", payload)
            )
            return resource
        except BaseException:
            await resource.aclose()
            await self.delete(resource)
            raise

    async def delete(self, resource):
        self.deleted_ids.append(resource.state.container_id)
        await super().delete(resource)


class ObservedGateway(DockerGateway):
    configs = None

    @asynccontextmanager
    async def __call__(self, resource):
        async with super().__call__(resource) as selected:
            if self.configs is not None:
                self.configs.append(Path(selected._argv[selected._argv.index("--config") + 1]))
            yield selected


@pytest.mark.parametrize("mode", ["read", "cancel", "missing-server", "headers"])
async def test_selected_native_docker_gateway(responses, monkeypatch, mode):
    assert IMAGE is not None and SOCKET is not None and Path(SOCKET).is_absolute()
    connection = docker.DockerClient(base_url="unix://" + SOCKET)
    selected_image = connection.images.get(IMAGE).id
    client = FixtureClient(connection)
    requests_b = []
    gateway = ObservedGateway(
        client,
        exec_server_path="/missing-exec-server"
        if mode == "missing-server"
        else "/usr/local/bin/exec-server",
        tools=("project_glob",),
    )
    configs = []
    object.__setattr__(gateway, "configs", configs)
    if mode == "headers":
        from _agents_codex.sandbox import _DockerStdioEnvironment

        original = _DockerStdioEnvironment._to_native

        def inherited_headers(environment):
            options = original(environment)
            options["headers"] = {"Authorization": "synthetic-must-not-forward"}
            return options

        monkeypatch.setattr(_DockerStdioEnvironment, "_to_native", inherited_headers)
    native = provider(responses)
    try:
        with tempfile.TemporaryDirectory(prefix="codex-native-docker-") as directory:
            socket = Path(directory) / "B.sock"

            async def deny(reader, writer):
                requests_b.append(await reader.read(8192))
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(deny, path=socket)
            async with server:
                monkeypatch.setenv("DOCKER_HOST", "unix://" + str(socket))
                monkeypatch.setenv("DOCKER_CONTEXT", "unselected-synthetic-context")
                monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
                monkeypatch.setenv("DOCKER_CUSTOM_HEADERS", "X-Unselected=synthetic")
                source = sandbox_environment(
                    client,
                    manifest=Manifest(
                        root="/workspace",
                        entries={Path("proof.txt"): File(content=b"owned-selected-resource")},
                    ),
                    options=DockerSandboxClientOptions(image=selected_image, network_mode="none"),
                    connect=gateway,
                )
                agent = Agent(instructions="Inspect the selected workspace.", provider=native)
                if mode == "headers":
                    with pytest.raises(
                        RuntimeError, match="stdio executor cannot send HTTP headers"
                    ):
                        async with agent.session(environment=source):
                            pytest.fail("Published a stdio source with HTTP headers")
                    assert not responses.requests
                elif mode == "missing-server":
                    with pytest.raises(RuntimeError) as failure:
                        async with agent.session(environment=source):
                            pytest.fail("Published a missing exec-server")
                    assert "unknown field" not in str(failure.value)
                    assert not responses.requests
                elif mode == "cancel":
                    entered = asyncio.Event()

                    async def live():
                        async with agent.session(environment=source):
                            client.helper_pids.update(owned_cli_pids(client.created_ids[-1]))
                            assert client.helper_pids
                            entered.set()
                            await asyncio.Future()

                    task = asyncio.create_task(live())
                    try:
                        await asyncio.wait_for(entered.wait(), 30)
                    finally:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                else:
                    responses.respond(
                        function_call("project_glob", {"pattern": "**/*.txt"}, "selected-read")
                    )
                    responses.respond(assistant_output("done"))
                    async with agent.session(environment=source) as session:
                        result = await asyncio.wait_for(
                            (await session.submit("Inspect.")).get_result(), 30
                        )
                        assert result.final_output == "done"
                        client.helper_pids.update(owned_cli_pids(client.created_ids[-1]))
                        assert client.helper_pids
                    output = json.loads(tool_output(responses.requests[-1], "selected-read"))
                    assert output["matches"] == ["proof.txt"], output
                    assert output["errors"] == [], output
                assert requests_b == []
        assert len(client.created_ids) == 1
        assert client.deleted_ids == client.created_ids
        with pytest.raises(NotFound):
            connection.api.inspect_container(client.created_ids[0])
        assert configs and all(not path.exists() for path in configs)
        for _ in range(100):
            if not owned_cli_pids(client.created_ids[0]) and all(
                not Path("/proc", str(pid)).exists() for pid in client.helper_pids
            ):
                break
            await asyncio.sleep(0.02)
        assert not owned_cli_pids(client.created_ids[0])
        assert all(not Path("/proc", str(pid)).exists() for pid in client.helper_pids)
    finally:
        await native.aclose()
        connection.close()

"""Run this opt-in test with an existing Docker image that supplies Bash and bubblewrap.

Use the Docker gateway fixture's image, socket, and matching exec-server settings.
The guest must support the fixture's glibc ABI and Core's standard nested sandbox
under the runner's ordinary Docker settings.
"""

import os
import sys
from pathlib import Path

import pytest

from agents import Agent, SessionState
from agents.sandbox import DockerGateway, Manifest, sandbox_environment
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_docker_gateway import (
    BINARY,
    IMAGE,
    SOCKET,
    DockerSandboxClientOptions,
    NotFound,
    docker,
)
from tests.test_native_docker_path_authority import MountedFixtureClient

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEX_DOCKER_STRICT_POSITIVE_TEST") != "1",
    reason="requires opt-in on a Docker runner supporting Core's nested sandbox",
)


@pytest.fixture
def docker_inputs(monkeypatch, tmp_path):
    assert sys.platform == "linux"
    assert IMAGE, "set CODEX_DOCKER_TEST_IMAGE to an existing compatible image"
    assert SOCKET and Path(SOCKET).is_absolute() and Path(SOCKET).is_socket()
    assert BINARY.is_file() and os.access(BINARY, os.X_OK)
    config = tmp_path / "docker-config"
    config.mkdir()
    (config / "config.json").write_text("{}")
    netrc = tmp_path / "empty-netrc"
    netrc.touch()
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("NETRC", str(netrc))
    return IMAGE, SOCKET


async def test_native_docker_mapped_shell_and_patch_after_cold_reopen(
    responses, tmp_path, docker_inputs
):
    requested_image, socket = docker_inputs
    read_target = "/mnt/native-readonly"
    write_target = "/mnt/native-writable"
    daemon = docker.DockerClient(base_url="unix://" + socket)
    client = MountedFixtureClient(daemon)
    try:
        image = daemon.images.get(requested_image).id

        def source(phase: str):
            read_root = tmp_path / f"{phase}-readonly"
            write_root = tmp_path / f"{phase}-writable"
            read_root.mkdir()
            write_root.mkdir()
            (read_root / "marker.txt").write_bytes(f"{phase}\n".encode())
            environment = sandbox_environment(
                client,
                manifest=Manifest(
                    root="/workspace",
                    extra_path_grants=(
                        {"path": read_target, "host_path": str(read_root), "read_only": True},
                        {"path": write_target, "host_path": str(write_root), "read_only": False},
                    ),
                ),
                options=DockerSandboxClientOptions(image=image, network_mode="none"),
                connect=DockerGateway(client, tools=("exec_command", "apply_patch")),
            )
            return environment, read_root, write_root

        async def prove(session, phase: str, read_root: Path, write_root: Path):
            start = len(responses.requests)
            shell_id = f"{phase}-shell"
            patch_id = f"{phase}-patch"
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": (
                            f'set -eu; printf "cwd=%s\\n" "$PWD"; test "$PWD" = {write_target}; '
                            f"cat {read_target}/marker.txt > shell.txt; cat shell.txt; "
                            f"if (printf denied > {read_target}/forbidden.txt) 2>/dev/null; "
                            "then exit 31; fi"
                        ),
                        "workdir": write_target,
                        "shell": "/bin/bash",
                        "login": False,
                    },
                    shell_id,
                )
            )
            responses.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": patch_id,
                        "input": (
                            f"*** Begin Patch\n*** Add File: {write_target}/patch.txt\n"
                            f"+patch-{phase}\n*** End Patch"
                        ),
                    },
                }
            )
            responses.respond(assistant_output(phase))
            turn = await session.submit("Use the selected shell and patch tools.")
            assert (await turn.get_result()).final_output == phase
            assert len(responses.requests) == start + 3
            shell = tool_output(responses.requests[start + 1], shell_id)
            assert "Process exited with code 0" in shell, shell
            assert f"cwd={write_target}\n{phase}" in shell, shell
            patch = tool_output(responses.requests[start + 2], patch_id, "custom_tool_call_output")
            assert patch.startswith("Exit code: 0\n"), patch
            assert (read_root / "marker.txt").read_bytes() == f"{phase}\n".encode()
            assert not (read_root / "forbidden.txt").exists()
            assert (write_root / "shell.txt").read_bytes() == f"{phase}\n".encode()
            assert (write_root / "patch.txt").read_bytes() == f"patch-{phase}\n".encode()

        async with timeout(60), provider(responses) as native:
            agent = Agent(instructions="Use the selected native tools.", provider=native)
            initial, initial_read, initial_write = source("initial")
            async with agent.session(environment=initial) as session:
                await prove(session, "initial", initial_read, initial_write)
                saved = SessionState.from_json((await session.save_state()).to_json())
            assert client.created_ids == client.deleted_ids and len(client.created_ids) == 1
            with pytest.raises(NotFound):
                daemon.api.inspect_container(client.created_ids[0])

            rebound, rebound_read, rebound_write = source("rebound")
            async with agent.session(state=saved, environment=rebound) as session:
                await prove(session, "rebound", rebound_read, rebound_write)
            assert (initial_write / "shell.txt").read_bytes() == b"initial\n"
            assert (initial_write / "patch.txt").read_bytes() == b"patch-initial\n"
            assert client.created_ids == client.deleted_ids and len(set(client.created_ids)) == 2
            with pytest.raises(NotFound):
                daemon.api.inspect_container(client.created_ids[1])
    finally:
        daemon.close()

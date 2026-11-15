"""Run real mapped Docker path grants through the native process tool."""

import json
import os
import stat
from pathlib import Path

import pytest

from agents import Agent, SessionState
from agents.classic.sandbox.util.blocking_io import run_blocking_workspace_io
from agents.sandbox import DockerGateway, Manifest, sandbox_environment
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_docker_gateway import (
    IMAGE,
    SOCKET,
    DockerSandboxClient,
    DockerSandboxClientOptions,
    FixtureClient,
    NotFound,
    docker,
    fixture_archive,
    pytestmark as pytestmark,
)


class MountedFixtureClient(FixtureClient):
    async def create(self, **options):
        resource = await DockerSandboxClient.create(self, **options)
        cid = resource.state.container_id
        self.created_ids.append(cid)
        try:
            inspection = await run_blocking_workspace_io(
                lambda: self.docker_client.api.inspect_container(cid)
            )
            assert inspection["HostConfig"]["NetworkMode"] == "none"
            assert not inspection["HostConfig"]["Privileged"]
            assert not inspection["HostConfig"].get("CapAdd")
            assert not inspection["HostConfig"].get("Devices")
            assert (inspection["HostConfig"].get("SecurityOpt") or []) == list(
                self.security_options
            )
            grants = resource.state.manifest.extra_path_grants
            mounts = {mount["Destination"]: mount for mount in inspection["Mounts"]}
            assert len(mounts) == len(inspection["Mounts"])
            assert set(mounts) == {grant.path for grant in grants}
            for grant in grants:
                mount = mounts[grant.path]
                assert mount["Type"] == "bind"
                assert mount["Source"] == grant.host_path
                assert mount["RW"] == (not grant.read_only)
            if os.environ.get("AGENTS_RECORD_DOCKER_BIND_METADATA") == "1":
                targets = ("/mnt/native-readonly", "/mnt/native-writable")
                host = {}
                for target in targets:
                    source = Path(mounts[target]["Source"])
                    info = source.stat()
                    host[target] = {
                        "uid": info.st_uid,
                        "gid": info.st_gid,
                        "mode": stat.S_IMODE(info.st_mode),
                        "has_git": (source / ".git").exists(),
                        "mount_rw": mounts[target]["RW"],
                    }
                capture = await run_blocking_workspace_io(
                    lambda: self.docker_client.containers.get(cid).exec_run(
                        [
                            "python3",
                            "-c",
                            "import json,os,pathlib,stat,sys;"
                            "mounts={p:os.stat(p) for p in sys.argv[1:]};"
                            "print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),"
                            "'uid_map':[[int(n) for n in l.split()] for l in "
                            "pathlib.Path('/proc/self/uid_map').read_text().splitlines()],"
                            "'gid_map':[[int(n) for n in l.split()] for l in "
                            "pathlib.Path('/proc/self/gid_map').read_text().splitlines()],"
                            "'mounts':{p:{'uid':s.st_uid,'gid':s.st_gid,"
                            "'mode':stat.S_IMODE(s.st_mode),"
                            "'has_git':os.path.exists(p+'/.git')} for p,s in mounts.items()}}))",
                            *targets,
                        ],
                    ),
                )
                print(
                    "Docker fixture bind metadata "
                    + json.dumps(
                        {
                            "resource_number": len(self.created_ids),
                            "config_uses_default_user": not inspection["Config"].get("User"),
                            "host": host,
                            "outer_container": json.loads(capture.output)
                            if capture.exit_code == 0
                            else None,
                            "inspect_ok": capture.exit_code == 0,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            archive = await run_blocking_workspace_io(fixture_archive)
            await run_blocking_workspace_io(
                lambda: self.docker_client.api.put_archive(cid, "/", archive)
            )
            return resource
        except BaseException:
            await resource.aclose()
            await self.delete(resource)
            raise


async def test_native_docker_path_grants_are_mapped_and_writes_are_scoped(
    responses, monkeypatch, tmp_path
):
    config = tmp_path / "docker-config"
    config.mkdir()
    (config / "config.json").write_text("{}")
    netrc = tmp_path / "empty.netrc"
    netrc.write_text("")
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("NETRC", str(netrc))

    read_dir = tmp_path / "readonly"
    write_dir = tmp_path / "writable"
    read_dir.mkdir()
    write_dir.mkdir()
    # Only the private fixture's writable bind needs to work from the nested
    # user namespace, where the runner's UID and GID are unmapped.
    write_dir.chmod(0o1777)
    (read_dir / "original").write_text("SYNTHETIC_READONLY_MOUNT")
    assert IMAGE and SOCKET
    daemon = docker.DockerClient(base_url="unix://" + SOCKET)
    client = MountedFixtureClient(daemon)
    native = provider(responses)
    try:
        image = daemon.images.get(IMAGE).id

        def source(*, writable: bool = True):
            return sandbox_environment(
                client,
                manifest=Manifest(
                    root="/workspace",
                    extra_path_grants=(
                        {
                            "path": "/mnt/native-readonly",
                            "host_path": str(read_dir),
                            "read_only": True,
                        },
                        {
                            "path": "/mnt/native-writable",
                            "host_path": str(write_dir),
                            "read_only": not writable,
                        },
                    ),
                ),
                options=DockerSandboxClientOptions(image=image, network_mode="none"),
                connect=DockerGateway(client, tools=("exec_command",)),
            )

        def respond(label):
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": (
                            "cat /mnt/native-readonly/original; "
                            "if touch /mnt/native-readonly/forbidden; then echo WRITE_ESCAPED; fi; "
                            f"printf '{label}' >/mnt/native-writable/{label}; "
                            "printf 'SELECTED_CWD='; pwd"
                        ),
                        "login": False,
                    },
                    label,
                )
            )
            responses.respond(assistant_output(label))

        agent = Agent(instructions="Use the assigned workspace.", provider=native)
        respond("first")
        async with agent.session(environment=source()) as session:
            assert (await (await session.submit("Inspect.")).get_result()).final_output == "first"
            saved = SessionState.from_json((await session.save_state()).to_json())
        respond("restored")
        async with agent.session(state=saved, environment=source()) as session:
            result = await (await session.submit("Inspect after reopening.")).get_result()
            assert result.final_output == "restored"
        with pytest.raises(
            ValueError, match="Checkpoint and Environment must agree on the applied executor policy"
        ):
            async with agent.session(state=saved, environment=source(writable=False)):
                pytest.fail("A weaker current grant cannot reopen the recorded executor authority")
        assert len(responses.requests) == 4
        for index, label in ((1, "first"), (3, "restored")):
            output = tool_output(responses.requests[index], label)
            if os.environ.get("AGENTS_RECORD_DOCKER_BIND_METADATA") == "1":
                print(
                    "Docker fixture synthetic tool response "
                    + json.dumps(
                        {
                            "stage": label,
                            "positive_marker": "SYNTHETIC_READONLY_MOUNT" in output,
                            "git_permission_denied": ".git: Permission denied" in output,
                            "read_only_escape_marker": "WRITE_ESCAPED" in output,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            assert "SYNTHETIC_READONLY_MOUNT" in output and "WRITE_ESCAPED" not in output, output
            assert "SELECTED_CWD=/workspace" in output, output
            assert (write_dir / label).read_text() == label
        assert not (read_dir / "forbidden").exists()
        assert client.created_ids == client.deleted_ids and len(client.created_ids) == 3
        for container_id in client.created_ids:
            with pytest.raises(NotFound):
                daemon.api.inspect_container(container_id)
    finally:
        await native.aclose()
        daemon.close()


@pytest.mark.asyncio
async def test_docker_resource_keeps_read_and_write_grants_under_native_session(
    responses, monkeypatch, tmp_path
):
    config = tmp_path / "docker-config"
    config.mkdir()
    (config / "config.json").write_text("{}")
    netrc = tmp_path / "empty.netrc"
    netrc.write_text("")
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("NETRC", str(netrc))
    read_dir = tmp_path / "readonly"
    write_dir = tmp_path / "writable"
    read_dir.mkdir()
    write_dir.mkdir()
    (read_dir / "original").write_text("SYNTHETIC_READONLY_MOUNT")
    assert IMAGE and SOCKET
    daemon = docker.DockerClient(base_url="unix://" + SOCKET)
    client = MountedFixtureClient(daemon)
    native = provider(responses)
    try:
        image = daemon.images.get(IMAGE).id
        source = sandbox_environment(
            client,
            manifest=Manifest(
                root="/workspace",
                extra_path_grants=(
                    {"path": "/mnt/native-readonly", "host_path": str(read_dir), "read_only": True},
                    {
                        "path": "/mnt/native-writable",
                        "host_path": str(write_dir),
                        "read_only": False,
                    },
                ),
            ),
            options=DockerSandboxClientOptions(image=image, network_mode="none"),
            connect=DockerGateway(client, tools=("project_glob",)),
        )
        responses.respond(function_call("project_glob", {"pattern": "*"}, "list-current"))
        responses.respond(assistant_output("done"))
        async with Agent(instructions="Use the assigned workspace.", provider=native).session(
            environment=source
        ) as session:
            assert (await (await session.submit("Inspect.")).get_result()).final_output == "done"
            cid = client.created_ids[0]
            container = daemon.containers.get(cid)
            cmd = [
                "/bin/sh",
                "-c",
                "cat /mnt/native-readonly/original && "
                "! touch /mnt/native-readonly/forbidden && "
                "printf SYNTHETIC_WRITABLE_MOUNT >/mnt/native-writable/receipt",
            ]
            result = await run_blocking_workspace_io(lambda: container.exec_run(cmd))
            assert result.exit_code == 0 and b"SYNTHETIC_READONLY_MOUNT" in result.output
            assert not (read_dir / "forbidden").exists()
            assert (write_dir / "receipt").read_text() == "SYNTHETIC_WRITABLE_MOUNT"
        assert client.created_ids == client.deleted_ids and len(client.created_ids) == 1
        with pytest.raises(NotFound):
            daemon.api.inspect_container(client.created_ids[0])
    finally:
        await native.aclose()
        daemon.close()

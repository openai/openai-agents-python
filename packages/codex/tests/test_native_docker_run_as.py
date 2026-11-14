"""Native Docker identity qualification on the same opt-in fixture as the gateway suite."""

import io
import json
import tarfile
from pathlib import Path

import pytest

from agents import Agent, SessionState, ToolRef
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
    DockerSandboxClientOptions,
    FixtureClient,
    NotFound,
    docker,
    pytestmark as pytestmark,
)


@pytest.fixture(autouse=True)
def isolated_daemon_authority(monkeypatch, tmp_path):
    config = tmp_path / "docker-config"
    config.mkdir()
    (config / "config.json").write_text("{}")
    netrc = tmp_path / "empty.netrc"
    netrc.write_text("")
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("DOCKER_HOST", "unix:///unselected-native-identity-daemon.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "unselected-native-identity-context")


class IdentityClient(FixtureClient):
    async def create(self, **options):
        resource = await super().create(**options)
        cid = resource.state.container_id
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            directory = tarfile.TarInfo("workspace")
            directory.type, directory.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(directory)
            payload = cid.encode()
            info = tarfile.TarInfo("workspace/native-resource-" + cid + ".txt")
            info.mode, info.size = 0o644, len(payload)
            archive.addfile(info, io.BytesIO(payload))
            private = b"SYNTHETIC_ROOT_ONLY_NATIVE_IDENTITY"
            info = tarfile.TarInfo("workspace/native-root-only.txt")
            info.mode, info.size = 0o600, len(private)
            archive.addfile(info, io.BytesIO(private))
        try:
            await run_blocking_workspace_io(
                lambda: self.docker_client.api.put_archive(cid, "/", data.getvalue())
            )
            return resource
        except BaseException:
            await resource.aclose()
            await self.delete(resource)
            raise


async def assert_executor_user(client, cid, user):
    listing = await run_blocking_workspace_io(
        lambda: client.docker_client.api.top(cid, ps_args="-eo pid,euid,egid,args")
    )
    rows = [row for row in listing["Processes"] if "exec-server" in row[-1]]
    assert rows, listing
    assert all(row[1] == row[2] == str(user) for row in rows), listing


def source_for(client, image, user, tools=("project_grep",)):
    return sandbox_environment(
        client,
        manifest=Manifest(root="/workspace"),
        options=DockerSandboxClientOptions(image=image, network_mode="none"),
        connect=DockerGateway(client, tools=tools, run_as=user),
    )


async def test_native_selected_docker_users_have_distinct_current_resources(responses):
    assert IMAGE is not None and SOCKET is not None and Path(SOCKET).is_absolute()
    connection = docker.DockerClient(base_url="unix://" + SOCKET)
    native = provider(responses)
    client = IdentityClient(connection)
    try:
        info = await run_blocking_workspace_io(connection.info)
        if any("userns" in entry for entry in info.get("SecurityOptions", [])):
            pytest.skip("top UID comparison requires the explicitly selected non-remapped daemon")
        image = connection.images.get(IMAGE).id
        first = source_for(client, image, "10001:10001")
        second = source_for(client, image, "10002:10002")
        root = Agent(instructions="Inspect only this current resource.", provider=native)
        async with root.session(environment=first) as first_session:
            async with root.session(environment=second) as second_session:
                assert len(client.created_ids) == 2
                a, b = client.created_ids
                assert a != b
                await assert_executor_user(client, a, 10001)
                await assert_executor_user(client, b, 10002)
                responses.respond(
                    function_call("project_grep", {"pattern": a, "path": "."}, "root-read")
                )
                responses.respond(assistant_output("first done"))
                responses.respond(
                    function_call("project_grep", {"pattern": b, "path": "."}, "second-read")
                )
                responses.respond(assistant_output("second done"))
                assert (
                    await (await first_session.submit("Inspect the first resource.")).get_result()
                ).final_output == "first done"
                assert (
                    await (await second_session.submit("Inspect the second resource.")).get_result()
                ).final_output == "second done"
                first_output = json.loads(tool_output(responses.requests[1], "root-read"))
                second_output = json.loads(tool_output(responses.requests[-1], "second-read"))
                assert len(first_output["matches"]) == 1
                assert first_output["matches"][0]["path"] == "native-resource-" + a + ".txt"
                assert first_output["matches"][0]["line"] == a
                assert len(second_output["matches"]) == 1
                assert second_output["matches"][0]["path"] == "native-resource-" + b + ".txt"
                assert second_output["matches"][0]["line"] == b
                assert (
                    first_output["errors"]
                    == second_output["errors"]
                    == [{"code": "path_unavailable", "path": "native-root-only.txt"}]
                )
        assert sorted(client.deleted_ids) == sorted(client.created_ids)
        for cid in client.created_ids:
            with pytest.raises(NotFound):
                connection.api.inspect_container(cid)
    finally:
        await native.aclose()
        connection.close()


async def test_native_selected_docker_user_does_not_add_executor_grants(responses):
    assert IMAGE is not None and SOCKET is not None
    connection = docker.DockerClient(base_url="unix://" + SOCKET)
    native = provider(responses)
    client = IdentityClient(connection)
    try:
        image = connection.images.get(IMAGE).id
        async with Agent(instructions="No executor grants.", provider=native).session(
            environment=source_for(client, image, "10001:10001", tools=())
        ) as session:
            with pytest.raises(ValueError, match="allowed when the Session opened"):
                await session.submit(
                    "Try an unadmitted process.", tools=[ToolRef("exec_command", "functions")]
                )
            assert not responses.requests
        assert client.deleted_ids == client.created_ids
    finally:
        await native.aclose()
        connection.close()


async def test_native_missing_docker_user_fails_before_sampling_and_retires_resource(responses):
    assert IMAGE is not None and SOCKET is not None
    connection = docker.DockerClient(base_url="unix://" + SOCKET)
    native = provider(responses)
    client = IdentityClient(connection)
    try:
        image = connection.images.get(IMAGE).id
        with pytest.raises(RuntimeError):
            async with Agent(instructions="No fallback.", provider=native).session(
                environment=source_for(client, image, "native-sdk-no-user-6d2156a1")
            ):
                pytest.fail("A missing selected user opened a native session")
        assert not responses.requests
        assert len(client.created_ids) == 1
        assert client.deleted_ids == client.created_ids
        with pytest.raises(NotFound):
            connection.api.inspect_container(client.created_ids[0])
    finally:
        await native.aclose()
        connection.close()


@pytest.mark.parametrize(("user", "can_read"), [("0:0", True), ("10001:10001", False)])
async def test_native_docker_file_reads_follow_selected_process_user(responses, user, can_read):
    assert IMAGE is not None and SOCKET is not None
    connection = docker.DockerClient(base_url="unix://" + SOCKET)
    native = provider(responses)
    client = IdentityClient(connection)
    try:
        image = connection.images.get(IMAGE).id
        responses.respond(
            function_call(
                "project_grep",
                {"pattern": "SYNTHETIC_ROOT_ONLY_NATIVE_IDENTITY", "path": "."},
                "private-read",
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(instructions="Inspect current ownership.", provider=native).session(
            environment=source_for(client, image, user)
        ) as session:
            assert (await (await session.submit("Inspect.")).get_result()).final_output == "done"
            output = json.loads(tool_output(responses.requests[-1], "private-read"))
            if can_read:
                assert len(output["matches"]) == 1
                assert output["matches"][0]["line"] == "SYNTHETIC_ROOT_ONLY_NATIVE_IDENTITY"
                assert output["errors"] == []
            else:
                assert output["matches"] == []
        assert client.deleted_ids == client.created_ids
    finally:
        await native.aclose()
        connection.close()


async def test_native_plain_docker_identity_rebinds_on_cold_restore(responses):
    assert IMAGE is not None and SOCKET is not None
    connection = docker.DockerClient(base_url="unix://" + SOCKET)
    client = IdentityClient(connection)
    native = provider(responses)
    try:
        image = connection.images.get(IMAGE).id
        agent = Agent(instructions="Use the current workspace.", provider=native)
        responses.respond(assistant_output("first"))
        async with agent.session(environment=source_for(client, image, "10001:10001")) as session:
            result = await (await session.submit("First turn.")).get_result()
            assert result.final_output == "first"
            saved = SessionState.from_json((await session.save_state()).to_json())
            assert saved.agent_environment_policy_digest
        responses.respond(assistant_output("restored"))
        async with agent.session(
            state=saved, environment=source_for(client, image, "10001:10001")
        ) as session:
            result = await (await session.submit("Continue.")).get_result()
            assert result.final_output == "restored"
        with pytest.raises(RuntimeError, match="recorded Agent environment grants"):
            async with agent.session(
                state=saved, environment=source_for(client, image, "10002:10002")
            ):
                pytest.fail("A different selected user cannot restore the recorded grants")
        assert len(responses.requests) == 2
        assert sorted(client.deleted_ids) == sorted(client.created_ids)
    finally:
        await native.aclose()
        connection.close()

"""Authenticated read-only searches with canonical storage and cold resume."""

import json
from contextlib import asynccontextmanager

import pytest

from agents import Agent, CustomStorage, Environment, NativeProvider, SQLiteSessionStore
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


@pytest.mark.parametrize("kind", ["direct", "lease", "persistent"])
async def test_remote_readonly_search_with_canonical_storage(
    responses, tmp_path, monkeypatch, kind
):
    persistent = kind == "persistent"
    async with ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        project = peer.root / "project"
        project.mkdir()
        (project / "data.txt").write_text("retained needle\n")
        database = tmp_path / "canonical.sqlite"
        environment = Environment(
            url=peer.url,
            cwd=str(project),
            headers={"Authorization": "Bearer " + peer.token},
            tools=["project_glob", "project_grep"],
            read_only=True,
        )

        @asynccontextmanager
        async def lease():
            yield environment

        selected = lease() if kind == "lease" else environment
        responses.respond(function_call("project_glob", {"pattern": "**/*.txt"}, "stored-glob"))
        responses.respond(assistant_output("found"))
        if persistent:
            responses.respond(function_call("project_grep", {"pattern": "needle"}, "stored-grep"))
            responses.respond(assistant_output("restored"))
        async with NativeProvider(
            base_url=responses.url, api_key="synthetic-remote-readonly", supports_websockets=False
        ) as provider:
            agent = Agent(instructions="Search the selected project.", provider=provider)
            storage = CustomStorage(SQLiteSessionStore(database)) if persistent else None
            async with (
                timeout(25),
                agent.session(storage=storage, environment=selected) as source,
            ):
                identity = source.id
                assert (await (await source.submit("glob")).get_result()).final_output == "found"
            first = json.loads(tool_output(responses.requests[1], "stored-glob"))
            assert first["matches"] == ["data.txt"] and first["errors"] == []
            if persistent:
                async with (
                    timeout(25),
                    agent.session(
                        storage=CustomStorage(SQLiteSessionStore(database)),
                        environment=environment,
                        resume=identity,
                    ) as restored,
                ):
                    result = await (await restored.submit("grep")).get_result()
                    assert result.final_output == "restored"
                second = json.loads(tool_output(responses.requests[3], "stored-grep"))
                assert second["matches"][0]["line"] == "retained needle"
                assert second["errors"] == []


@pytest.mark.parametrize(
    "options",
    [
        {"tools": ["exec_command"], "read_only": True},
        {"tools": ["project_glob", "exec_command"], "read_only": True},
        {"tools": ["apply_patch"]},
        {"tools": ["project_glob"]},
        {"tools": [], "read_only": True},
        {"tools": ["project_glob"], "read_only": True, "capability_directories": ["/plugins"]},
    ],
    ids=["process", "mixed-process", "patch", "writable", "empty", "capability-plugins"],
)
async def test_unsupported_remote_storage_grants_reject_before_acquisition(
    responses, tmp_path, options
):
    class UnopenedStore(SQLiteSessionStore):
        def acquire(self, thread_id):
            pytest.fail("Unsupported environment acquired canonical storage")

    environment = Environment(url="wss://127.0.0.1:1", cwd="/project", **options)
    async with NativeProvider(base_url=responses.url, api_key="synthetic-admission") as provider:
        agent = Agent(instructions="Search safely.", provider=provider)
        with pytest.raises(ValueError, match="CustomStorage preview does not support"):
            async with agent.session(
                storage=CustomStorage(UnopenedStore(tmp_path / "store.sqlite")),
                environment=environment,
            ):
                pytest.fail("Unsupported environment opened")
    assert responses.requests == []


async def test_custom_storage_rejects_uncaptured_environment_lease(responses, tmp_path):
    @asynccontextmanager
    async def lease():
        pytest.fail("Unsupported lease was opened")
        yield

    async with NativeProvider(base_url=responses.url, api_key="synthetic-admission") as provider:
        agent = Agent(instructions="Search safely.", provider=provider)
        with pytest.raises(ValueError, match="CustomStorage preview does not support"):
            async with agent.session(
                storage=CustomStorage(SQLiteSessionStore(tmp_path / "store.sqlite")),
                environment=lease(),
            ):
                pytest.fail("Unsupported lease opened")
    assert responses.requests == []

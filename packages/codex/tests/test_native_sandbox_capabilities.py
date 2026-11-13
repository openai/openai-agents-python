"""Application capabilities use the existing provider and native opening owners."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agents import Agent, Environment, function_tool
from agents.classic.sandbox.entries import File
from agents.sandbox import Manifest, SandboxCapability, sandbox_environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options


class WorkspaceGuidance(SandboxCapability):
    name = "workspace-guide"

    def __init__(self):
        self.bound = None

    def prepare_manifest(self, manifest):
        manifest.entries[Path("provided.txt")] = File(content=b"source")
        return manifest

    async def bind(self, session, *, user, workspace):
        assert self.bound is None
        self.bound = session
        assert workspace.cwd.as_posix() == "child"

    async def instructions(self, manifest):
        assert self.bound is not None
        assert manifest.entries[Path("provided.txt")].content == b"source"
        return "Read the record contributed by this capability."

    def tools(self):
        @function_tool
        def sandbox_record() -> str:
            assert self.bound is not None
            return "bound-sandbox-value"

        return (sandbox_record,)

    def sampling_settings(self, settings):
        return {"temperature": 0.25}

    async def transform_context(self, request):
        return (
            *request.items,
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "sandbox-context-replacement"}],
            },
        )


async def test_sandbox_capability_reaches_the_live_resource_and_model(
    tmp_path, monkeypatch, responses
):
    declaration = WorkspaceGuidance()
    root = tmp_path / "executor"
    child = root / "child"
    manifest = Manifest(root=str(root))
    client = Client([])
    native = provider(responses)
    responses.respond(function_call("sandbox_record", {}, "sandbox-first"))
    responses.respond(assistant_output("done"))
    responses.respond(function_call("sandbox_record", {}, "sandbox-second"))
    responses.respond(assistant_output("done again"))
    try:
        async with ExecutorPeer(root).serve() as peer:
            child.mkdir()
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(sandbox):
                assert sandbox is client.session
                assert sandbox.state.manifest.entries[Path("provided.txt")].content == b"source"
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(child),
                    tools=(),
                )
                await peer.wait_disconnected()

            agent = Agent(
                instructions="Read the workspace.",
                model="fixture-unlisted-sandbox-capability",
                provider=native,
            )
            for expected in ("done", "done again"):
                source = sandbox_environment(
                    client,
                    manifest=manifest,
                    options=Options(),
                    connect=connect,
                    capabilities=[declaration],
                )
                async with agent.session(environment=source) as session:
                    result = await (await session.submit("Run")).get_result()
                    assert result.final_output == expected
        assert declaration.bound is None and not manifest.entries
        assert client.events == ["create", "start", "close", "delete"] * 2
        assert all(
            "Read the record contributed by this capability." in str(request["input"])
            for request in responses.requests
        )
        assert len(responses.requests) == 4
        assert all(request["temperature"] == 0.25 for request in responses.requests)
        assert all(
            "sandbox-context-replacement" in json.dumps(request["input"])
            for request in responses.requests
        )
        assert all(
            "bound-sandbox-value" in json.dumps(responses.requests[index]["input"])
            for index in (1, 3)
        )
    finally:
        await native.aclose()


def test_missing_sandbox_capability_rejects_before_provisioning():
    declaration = WorkspaceGuidance()
    declaration.requires = ("compaction",)
    client = Client([])
    with pytest.raises(ValueError, match="workspace-guide requires compaction"):
        sandbox_environment(
            client,
            manifest=Manifest(root="/workspace"),
            options=Options(),
            connect=None,
            capabilities=[declaration],
        )
    assert not client.events


async def test_failed_sandbox_capability_binding_retires_its_resource():
    class Failing(WorkspaceGuidance):
        async def bind(self, session, *, user, workspace):
            raise LookupError("bind failed")

    client = Client([])

    @asynccontextmanager
    async def connect(sandbox):
        assert sandbox is client.session
        try:
            yield Environment(url="wss://executor.invalid", cwd="/workspace/child")
        finally:
            client.events.append("disconnect")

    source = sandbox_environment(
        client,
        manifest=Manifest(root="/workspace"),
        options=Options(),
        connect=connect,
        capabilities=[Failing()],
    )
    with pytest.raises(LookupError, match="bind failed"):
        async with source:
            pytest.fail("A failed acquisition was published")
    assert client.events == ["create", "start", "disconnect", "close", "delete"]


async def test_sandbox_capability_tools_use_normal_catalog_collision_rules(
    tmp_path, monkeypatch, responses
):
    @function_tool
    def sandbox_record() -> str:
        return "original"

    client = Client([])
    native = provider(responses)
    try:
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            child = peer.root / "child"
            child.mkdir()

            @asynccontextmanager
            async def connect(sandbox):
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(child),
                )
                client.events.append("disconnect")

            source = sandbox_environment(
                client,
                manifest=Manifest(root=str(peer.root)),
                options=Options(),
                connect=connect,
                capabilities=[WorkspaceGuidance()],
            )
            agent = Agent(
                instructions="Read the workspace.", provider=native, tools=[sandbox_record]
            )
            with pytest.raises(ValueError, match="collid|[Dd]uplicate"):
                async with agent.session(environment=source):
                    pytest.fail("Duplicate capability tool was published")
        assert client.events == ["create", "start", "disconnect", "close", "delete"]
        assert not responses.requests
    finally:
        await native.aclose()

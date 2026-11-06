"""Declared roles, bounded delegation and root-only contracts at real native boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from agents import Agent, ChildInvocation, NativeAgents, NativeError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import (
    agent_for,
    child_peer,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_child_tool_integration import FamilyPeer, Memory, family_agent
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer


class Report(BaseModel):
    answer: str


class DelegationPeer(FamilyPeer):
    def __init__(self, *, programmatic_tool_calling: bool) -> None:
        super().__init__(programmatic_tool_calling=programmatic_tool_calling, grandchildren=True)
        self.roles = ("default", "research")
        self.entered["grandchild"] = asyncio.Event()
        self.release["grandchild"] = asyncio.Event()

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        path = metadata(body, self.request_headers[-1])["agent_name"]
        step = self.steps.get(path, 0)
        if path == "/root/alpha" and step == 1:
            self.steps[path] = step + 1
            return [
                collaboration(
                    "spawn_agent",
                    {"task_name": "grandchild", "message": "Read.", "fork_turns": "none"},
                    "spawn-grandchild",
                )
            ]
        if path.endswith("/grandchild") and step == 2:
            self.steps[path] = step + 1
            args = {"task_name": "too_deep", "message": "Read.", "fork_turns": "none"}
            return [collaboration("spawn_agent", args, "depth-limit")]
        result = super().response()
        if path == "/root" and step >= 2:
            return [assistant_output('{"answer":"root complete"}')]
        return result


@pytest.mark.parametrize("failure", ["unknown-model", "unsupported-effort"])
async def test_unusable_role_is_rejected_before_any_provider_or_executor_io(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(20),
        child_peer(("research",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        assert next(t for t in agent.tools if isinstance(t, NativeAgents)) is not None
        agent = replace(
            agent,
            tools=[
                *[t for t in agent.tools if not isinstance(t, NativeAgents)],
                replace(
                    next(t for t in agent.tools if isinstance(t, NativeAgents)),
                    additional_roles=[
                        replace(
                            role,
                            model="unknown-native-child-fixture-model"
                            if failure == "unknown-model"
                            else None,
                            model_settings={"reasoning_effort": "invalid"}
                            if failure == "unsupported-effort"
                            else None,
                        )
                        for role in next(
                            t for t in agent.tools if isinstance(t, NativeAgents)
                        ).additional_roles
                    ],
                ),
            ],
        )
        with pytest.raises(
            (RuntimeError, ValueError), match="metadata is incompatible|effort|variant"
        ):
            async with agent.session(environment=declaration(executor)):
                pytest.fail("Incompatible role opened")
        assert peer.requests == []
        assert executor.attempts == 0
        assert executor.requests == []


async def test_code_mode_rejects_a_new_direct_role_model_before_remote_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    peer = DelegationPeer(programmatic_tool_calling=True)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(25),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        agent = family_agent(peer, executor, url, memory)
        other_model = agent_for(peer, executor).model
        assert other_model != agent.model
        assert next(t for t in agent.tools if isinstance(t, NativeAgents)) is not None
        agent = replace(
            agent,
            tools=[
                *[t for t in agent.tools if not isinstance(t, NativeAgents)],
                replace(
                    next(t for t in agent.tools if isinstance(t, NativeAgents)),
                    additional_roles=[
                        replace(role, model=other_model)
                        for role in next(
                            t for t in agent.tools if isinstance(t, NativeAgents)
                        ).additional_roles
                    ],
                ),
            ],
        )
        with pytest.raises(RuntimeError, match="incompatible with Session Code Mode"):
            async with agent.session(context=memory, environment=declaration(executor)):
                pytest.fail("A role without the Session's code tool mode opened")
        assert peer.requests == []
        assert executor.attempts == 0
        assert executor.requests == []


@pytest.mark.parametrize("programmatic_tool_calling", [False, True], ids=["direct", "code"])
async def test_role_models_and_depth_preserve_typed_root_and_child_output(
    programmatic_tool_calling: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    peer = DelegationPeer(programmatic_tool_calling=programmatic_tool_calling)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(60),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        base = family_agent(peer, executor, url, memory)
        child_model = base.model if programmatic_tool_calling else agent_for(peer, executor).model
        if not programmatic_tool_calling:
            assert child_model != base.model
        assert next(t for t in base.tools if isinstance(t, NativeAgents)) is not None
        children = replace(
            next(t for t in base.tools if isinstance(t, NativeAgents)),
            max_depth=2,
            additional_roles=[
                replace(role, model=child_model, model_settings={"reasoning_effort": "low"})
                for role in next(
                    t for t in base.tools if isinstance(t, NativeAgents)
                ).additional_roles
            ],
        )
        agent: Agent[Memory, Report] = Agent(
            instructions=base.instructions,
            model=base.model,
            provider=base.provider,
            tools=[*[t for t in base.tools if not isinstance(t, NativeAgents)], children],
            programmatic_tool_calling=programmatic_tool_calling,
            output_type=Report,
        )
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            root = await session.submit("Read with alpha and beta.")
            try:
                await asyncio.gather(peer.entered["alpha"].wait(), peer.entered["beta"].wait())
                root_result = await (root).get_result()
                assert root_result.final_output == Report(answer="root complete")
                peer.release["alpha"].set()
                peer.release["beta"].set()
                await peer.entered["grandchild"].wait()
                turns = await handles(session, 3)
                peer.release["grandchild"].set()
                for turn in turns:
                    try:
                        completed = await (turn).get_result()
                        assert isinstance(completed.final_output, str)
                    except NativeError:
                        # The forbidden fourth-level dispatch may end the
                        # grandchild's turn or produce a failed call output.
                        assert turn.child_id == peer.child_metadata["grandchild"][0]["thread_id"]
                assert len([item async for item in session.subagents()]) == 3
                assert "too_deep" not in peer.child_requests
                assert set(memory.entries) == {"root", "alpha", "beta", "grandchild"}
                mcp_children = set()
                for ctx in memory.mcp:
                    assert isinstance(ctx.submission, ChildInvocation)
                    mcp_children.add(ctx.submission.child_id)
                assert mcp_children == {
                    peer.child_metadata[name][0]["thread_id"] for name in ("beta", "grandchild")
                }
                for name in ("alpha", "beta", "grandchild"):
                    invocation = memory.entries[name].submission
                    assert isinstance(invocation, ChildInvocation)
                    assert invocation.child_id == peer.child_metadata[name][0]["thread_id"]
                    for request in peer.child_requests[name]:
                        assert (
                            request.get("text", {}).get("format", {}).get("type") != "json_schema"
                        )
                        assert request["model"] == (child_model if name == "beta" else base.model)
                        if name == "beta":
                            assert request["reasoning"]["effort"] == "low"
                roots = [
                    request
                    for request, headers in zip(peer.requests, peer.request_headers, strict=True)
                    if metadata(request, headers)["agent_name"] == "/root"
                ]
                assert (
                    roots[-1]["text"]["format"]["schema"]["properties"]["answer"]["type"]
                    == "string"
                )
            finally:
                for release in peer.release.values():
                    release.set()

"""Collaboration approval retains native admission and applies to descendant calls."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ChildInvocation,
    NativeAgents,
    NativeError,
    NativeToolCall,
    ToolContext,
    Turn,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    responses as responses,
)
from tests.test_native_child_agents import agent_for, child_peer, collaboration, handles, trust
from tests.test_native_executor_tools import declaration
from tests.test_review_ii_code_cell_interrupt import CellGraph


@pytest.mark.parametrize("answer", [False, {"action": "continue"}], ids=["deny", "old-decision"])
async def test_native_agents_denies_spawn_before_child_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    seen: list[NativeToolCall] = []

    async def deny_spawn(context: ToolContext[Any], call: NativeToolCall) -> bool:
        assert isinstance(context.submission, Turn)
        assert context.submission.session_id == call.thread_id
        assert context.submission.turn_id == call.turn_id
        assert context.call_id == call.call_id
        seen.append(call)
        return answer

    async with (
        timeout(20),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        children = next(t for t in agent.tools if isinstance(t, NativeAgents))
        assert isinstance(children, NativeAgents)
        agent = replace(
            agent,
            tools=[
                *[t for t in agent.tools if not isinstance(t, NativeAgents)],
                replace(children, approval=deny_spawn),
            ],
        )
        async with agent.session(environment=declaration(executor)) as session:
            with pytest.raises(NativeError, match="interception failed or was denied"):
                await (await session.submit("Use two children")).get_result()
            assert [entry async for entry in session.subagents()] == []
            assert all(call.thread_id == session.id for call in seen)
        assert {(call.namespace, call.name, call.phase) for call in seen} == {
            ("collaboration", "spawn_agent", "before")
        }
        assert {call.call_id for call in seen} <= {"spawn-alpha", "spawn-beta"}
        assert peer.child_requests == {}
        assert not (executor.root / "alpha.txt").exists()
        assert not (executor.root / "beta.txt").exists()


async def test_native_agents_approval_uses_application_context_to_deny_only_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_calls: list[NativeToolCall] = []
    denied_calls: list[tuple[ChildInvocation, NativeToolCall]] = []
    application = {"allow_descendants": False}

    def approve(context: ToolContext[dict[str, bool]], call: NativeToolCall) -> bool:
        assert context.context is application
        assert context.call_id == call.call_id
        assert context.submission.turn_id == call.turn_id
        if isinstance(context.submission, ChildInvocation):
            assert context.submission.child_id == call.thread_id
            denied_calls.append((context.submission, call))
            return context.context["allow_descendants"]
        assert context.submission.session_id == call.thread_id
        allowed_calls.append(call)
        return True

    peer = CellGraph()
    peer.programs["/root"] = deque(
        [
            [
                collaboration(
                    "spawn_agent",
                    {"task_name": "alpha", "message": "Start beta.", "fork_turns": "none"},
                    "spawn-alpha",
                )
            ],
            [assistant_output("root idle")],
        ]
    )
    peer.programs["/root/alpha"] = deque(
        [
            [
                collaboration(
                    "spawn_agent",
                    {"task_name": "beta", "message": "Read once.", "fork_turns": "none"},
                    "spawn-beta",
                )
            ],
            [assistant_output("alpha idle")],
        ]
    )
    peer.programs["/root/alpha/beta"] = deque([[assistant_output("beta started")]])
    peer.by_thread["/root/alpha/beta"] = []
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(35),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent: Agent[dict[str, bool], str] = Agent(
            instructions="Delegate the requested task.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[NativeAgents(max_children=2, max_depth=2, approval=approve)],
        )
        async with agent.session(context=application, environment=declaration(executor)) as session:
            root = await session.submit("Start alpha.")
            assert (await root.get_result()).final_output == "root idle"
            [child] = await handles(session, 1)
            with pytest.raises(NativeError, match="Native child turn failed"):
                await child.get_result()
            assert [call.call_id for call in allowed_calls] == ["spawn-alpha"]
            [(invocation, denied)] = denied_calls
            assert invocation.child_id == child.child_id
            assert invocation.submission_id == child.submission_id
            assert denied.call_id == "spawn-beta"
            assert len([entry async for entry in session.subagents()]) == 1
            assert peer.by_thread["/root/alpha/beta"] == []

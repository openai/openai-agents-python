"""A cold descendant keeps the current family's tool ownership on restore."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import ChildInvocation, FunctionTool, HttpMCP, NativeAgents, Turn
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_child_agents import collaboration, handles, metadata, trust
from tests.test_native_child_capabilities import DelegationPeer
from tests.test_native_child_tool_integration import FamilyPeer, Memory, family_agent, family_peer
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import Peer


class ColdGrandchild(DelegationPeer):
    def __init__(self) -> None:
        super().__init__(programmatic_tool_calling=False)
        self.restored = False
        self.sent_followup = False
        self.cold_step = 0

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        path = meta["agent_name"]
        if not self.restored:
            # Save a completed grandchild, without the separate depth-denial
            # case in DelegationPeer.
            if path.endswith("/grandchild") and self.steps.get(path) == 2:
                return FamilyPeer.response(self)
            return super().response()
        if path == "/root":
            if not self.sent_followup:
                self.sent_followup = True
                return [
                    collaboration(
                        "followup_task",
                        {"target": "/root/alpha/grandchild", "message": "Read the new record."},
                        "cold-followup",
                    )
                ]
            return [assistant_output("restored root complete")]
        assert path == "/root/alpha/grandchild"
        self.child_requests["grandchild"].append(body)
        self.child_metadata["grandchild"].append(meta)
        step = self.cold_step
        self.cold_step += 1
        if step == 0:
            return [function_call("lookup", {"key": "cold-grandchild"}, "cold-function")]
        if step == 1:
            return [self.invoke("grandchild", 1)]
        if step == 2:
            return [function_call("manual_record", {"key": "cold-grandchild"}, "cold-manual")]
        return [assistant_output("cold grandchild complete")]


async def test_root_followup_to_cold_grandchild_inherits_exact_current_tool_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    peer = ColdGrandchild()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    manual = FunctionTool(
        name="manual_record",
        description="Obtain a record through the application.",
        input_type=LookupArgs,
        output_type=str,
    )
    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        agent = family_agent(peer, executor, url, memory)
        assert next(t for t in agent.tools if isinstance(t, NativeAgents)) is not None
        agent = replace(
            agent,
            tools=[
                *[t for t in (*agent.tools, manual) if not isinstance(t, NativeAgents)],
                replace(next(t for t in agent.tools if isinstance(t, NativeAgents)), max_depth=2),
            ],
        )
        async with agent.session(context=memory, environment=declaration(executor)) as first:
            root = await first.submit("Read with alpha, beta, and grandchild.")
            await asyncio.gather(peer.entered["alpha"].wait(), peer.entered["beta"].wait())
            await (root).get_result()
            peer.release["alpha"].set()
            peer.release["beta"].set()
            await peer.entered["grandchild"].wait()
            observed = await handles(first, 3)
            old = next(
                turn
                for turn in observed
                if turn.child_id == peer.child_metadata["grandchild"][0]["thread_id"]
            )
            peer.release["grandchild"].set()
            for turn in observed:
                assert isinstance((await (turn).get_result()).final_output, str)
            old_invocation = memory.entries["grandchild"].submission
            assert isinstance(old_invocation, ChildInvocation)
            saved = SessionState.from_json((await first.save_state()).to_json())
            assert len(saved.descendants) == 3
        before_requests = len(peer.child_requests["grandchild"])
        before_policies = len(memory.mcp)
        peer.restored = True
        environment = declaration(executor)
        async with agent.session(
            context=memory,
            state=saved,
            environment=environment,
        ) as restored:
            # Restore does not load any intermediate thread. Only the native
            # followup below may resume the named grandchild from the graph.
            rows = [item async for item in restored.subagents()]
            assert len(rows) == 3
            assert all(row.turn is None for row in rows)
            assert next(t for t in agent.tools if isinstance(t, NativeAgents)) is not None
            with pytest.raises(RuntimeError, match="Cannot replace tools while native children"):
                denied = await restored.submit(
                    "Drop the manual binding from the saved family.",
                    tools=[*(tool for tool in agent.tools if tool is not manual), environment],
                )
                await denied.get_result()
            assert len(peer.child_requests["grandchild"]) == before_requests
            assert len(memory.mcp) == before_policies
            root = await restored.submit("Continue the saved grandchild directly.")
            assert (await (root).get_result()).final_output == "restored root complete"
            request = await next_request(restored.interactions(), manual)
            assert request.arguments.key == "cold-grandchild"
            assert request.subagent_id == old.child_id
            await request.respond("accepted-manual:cold-grandchild")
            current = next(
                child.turn
                for child in [item async for item in restored.subagents()]
                if child.id == request.subagent_id
            )
            assert isinstance(current, Turn)
            assert current.child_id == old.child_id
            # The original Turn still reports its settled source result.
            assert (await old.get_result()).turn is old
            assert (await (current).get_result()).final_output == "cold grandchild complete"
            lookup = memory.entries["cold-grandchild"]
            assert isinstance(lookup.submission, ChildInvocation)
            assert lookup.submission.child_id == current.child_id
            assert lookup.submission.turn_id == current.turn_id
            assert lookup.submission.instance_id != old_invocation.instance_id
            assert len(memory.mcp) == before_policies + 1
            mcp = memory.mcp[-1].submission
            assert isinstance(mcp, ChildInvocation)
            assert mcp.child_id == current.child_id
            assert mcp.turn_id == current.turn_id
            assert mcp.instance_id == lookup.submission.instance_id
            requests = peer.child_requests["grandchild"][before_requests:]
            assert len(requests) == 4
            assert tool_output(requests[1], "cold-function") == "accepted:cold-grandchild"
            assert "accepted-mcp:" + current.child_id in json.dumps(requests[2])
            assert tool_output(requests[3], "cold-manual") == "accepted-manual:cold-grandchild"
            wire = json.dumps(requests, ensure_ascii=False)
            assert memory.secret not in wire
            assert "mcp-private-f126" not in wire
            # The native reload must not wake either saved sibling or parent.
            rows = [item async for item in restored.subagents()]
            assert all(row.turn is None for row in rows if row.id != current.child_id)


async def test_registered_children_keep_python_and_mcp_after_roster_change_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(48),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = family_agent(peer, executor, url, memory)
        environment = declaration(executor)
        assert next(t for t in agent.tools if isinstance(t, NativeAgents)) is not None
        async with agent.session(context=memory, environment=environment) as session:
            first = await session.submit("Read through alpha and beta.")
            await asyncio.gather(peer.entered["alpha"].wait(), peer.entered["beta"].wait())
            assert (await first.get_result()).final_output == "root complete"
            children = await handles(session, 2)
            assert len(children) == 2
            assert set(memory.entries) == {"root"}
            selected = [
                *(tool for tool in agent.tools if not isinstance(tool, HttpMCP)),
                environment,
            ]
            try:
                with pytest.raises(
                    RuntimeError, match="Cannot replace tools while native children"
                ):
                    denied = await session.submit(
                        "Keep the Python slots, drop MCP.", tools=selected
                    )
                    await denied.get_result()
            finally:
                peer.release["alpha"].set()
                peer.release["beta"].set()
            results = await asyncio.gather(*(turn.get_result() for turn in children))
            assert sorted(result.final_output for result in results) == [
                "alpha complete",
                "beta complete",
            ]
            assert set(memory.entries) == {"root", "alpha", "beta"}
            assert len(memory.mcp) == 2


async def test_loaded_child_keeps_tools_when_incarnation_observation_lags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(48),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = family_agent(peer, executor, url, memory)
        environment = declaration(executor)
        assert any(isinstance(tool, NativeAgents) for tool in agent.tools)
        async with agent.session(context=memory, environment=environment) as session:
            first = await session.submit("Read through alpha and beta.")
            await asyncio.gather(peer.entered["alpha"].wait(), peer.entered["beta"].wait())
            assert (await first.get_result()).final_output == "root complete"
            turns = await handles(session, 2)
            peer.release["alpha"].set()
            peer.release["beta"].set()
            await asyncio.gather(*(turn.get_result() for turn in turns))
            assert set(memory.entries) == {"root", "alpha", "beta"}
            assert len(memory.mcp) == 2
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            native = session._native

            class UnobservedIncarnation:
                def __getattr__(self, name: str) -> Any:
                    return getattr(native, name)

                async def _child_instance(self, child_id: str) -> None:
                    # A created notification need not be consumed before the
                    # root terminal. Hold only that observation, not the native
                    # runtime, generation or MCP publication.
                    return None

            # Busy callback/admission owners have completed. The private reader
            # cannot issue a live snapshot yet, which is not proof of coldness.
            monkeypatch.setattr(session, "_native", UnobservedIncarnation())
            try:
                rows = [row async for row in session.subagents()]
                assert all(not row.is_live for row in rows)
                selected = [
                    *(tool for tool in agent.tools if not isinstance(tool, HttpMCP)),
                    environment,
                ]
                with pytest.raises(
                    RuntimeError, match="Cannot replace tools while native children"
                ):
                    denied = await session.submit("Keep Python, drop MCP", tools=selected)
                    await denied.get_result()
            finally:
                monkeypatch.setattr(session, "_native", native)
            rows = [row async for row in session.subagents()]
            alpha = next(row for row in rows if row.id == alpha_id)
            peer.steps["/root/alpha"] = 0
            peer.child_requests["alpha"].clear()
            previous_invoke = peer.invoke

            def successor_invoke(name: str, step: int) -> dict[str, Any]:
                if name == "alpha" and step == 0:
                    # Old child history remains in the new turn's model input.
                    return function_call("lookup", {"key": name}, "successor-lookup")
                return previous_invoke(name, step)

            monkeypatch.setattr(peer, "invoke", successor_invoke)
            next_turn = await alpha.start("Read again after the refused roster change.")
            assert (await next_turn.get_result()).final_output == "alpha complete"
            assert len(memory.mcp) == 3
            assert (
                tool_output(peer.child_requests["alpha"][1], "successor-lookup") == "accepted:alpha"
            )
            assert "accepted-mcp:" + alpha_id in json.dumps(peer.child_requests["alpha"][2])

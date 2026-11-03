"""Settled descendants survive an explicit root manual continuation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Completed, FunctionTool, NativeError, PendingRequests, function_tool
from agents.state import CheckpointThread, SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.native_planning_fixtures import legacy_checklist, legacy_goal
from tests.test_native_child_agents import (
    ChildPeer,
    agent_for,
    child_peer,
    handles,
    metadata,
    trust,
)
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_planning import TerminalChildPeer
from tests.test_native_workflow_persistence import model_restore_agent


@pytest.mark.parametrize("queued_steer", [False, True], ids=["no-steer", "accepted-steer"])
async def test_failed_child_model_receipt_retires_manual_and_automatic_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued_steer: bool
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    entered = asyncio.Event()
    release_automatic = asyncio.Event()
    cleaned = asyncio.Event()
    receipt_ready = asyncio.Event()
    release_receipt = asyncio.Event()
    beta_ready = asyncio.Event()
    release_beta = asyncio.Event()
    receipt_text = "synthetic-untrusted-child-model-receipt"

    @function_tool
    async def held_child_record() -> str:
        """Read a child record while the request is live."""
        entered.set()
        try:
            await release_automatic.wait()
            return "held child record"
        finally:
            cleaned.set()

    async with (
        timeout(35),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        agent = replace(agent, tools=(*agent.tools, lookup, held_child_record))
        ordinary = peer.response

        def respond() -> list[dict[str, Any]]:
            result = ordinary()
            path = metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"]
            if path == "/root":
                return result
            name = path.rsplit("/", 1)[-1]
            if name == "alpha" and len(peer.child_requests[name]) == 1:
                return [
                    function_call("lookup", {"key": "alpha"}, "manual-alpha"),
                    function_call("held_child_record", {}, "held-alpha"),
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call_output",
                            "call_id": "unknown-alpha",
                            "output": receipt_text,
                        },
                    },
                ]
            answer = assistant_output("alpha recovered" if name == "alpha" else "beta complete")
            if name == "beta":
                answer["item"]["id"] = "held-beta-answer"
            return [answer]

        async def hold(event: dict[str, Any]) -> None:
            item = event.get("item", {})
            if item.get("output") == receipt_text:
                await entered.wait()
                receipt_ready.set()
                await release_receipt.wait()
            if item.get("id") == "held-beta-answer":
                beta_ready.set()
                await release_beta.wait()

        peer.response = respond
        peer.before_event = hold
        try:
            async with agent.session(environment=declaration(executor)) as session:
                try:
                    root = await session.submit("Start two native children")
                    assert (await root.get_result()).final_output == "root complete"
                    await asyncio.gather(receipt_ready.wait(), beta_ready.wait())
                    request = await next_request(session.interactions(), lookup)
                    by_id = {turn.child_id: turn for turn in await handles(session, 2)}
                    alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
                    beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
                    assert (request.subagent_id, request.turn_id) == (alpha.child_id, alpha.turn_id)
                    assert request.call.tool == lookup.ref
                    assert request.call.session_id == session.id
                    assert request.call.call_id == "manual-alpha"
                    assert request.call.child_id == alpha.child_id
                    assert request.call.child_incarnation == request.submission_id.split(":")[-2]
                    assert request.call.role is None
                    assert request.call.model in {body["model"] for body in peer.requests}
                    done_root = await root.wait_for_request_or_result()
                    assert isinstance(done_root, Completed) and done_root.turn is root
                    alpha_batch = await alpha.wait_for_request_or_result()
                    assert isinstance(alpha_batch, PendingRequests)
                    assert [item.submission_id for item in alpha_batch.requests] == [
                        alpha.submission_id
                    ]
                    beta_waiter = asyncio.create_task(beta.wait_for_request_or_result())
                    await asyncio.sleep(0)
                    beta_waiter.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await beta_waiter
                    assert entered.is_set() and not cleaned.is_set()
                    sampled = len(peer.child_requests["alpha"])
                    alpha_row = await anext(
                        row async for row in session.subagents() if row.id == alpha.child_id
                    )
                    if queued_steer:
                        await alpha.steer("Keep the accepted child steering input")
                    release_receipt.set()
                    with pytest.raises(
                        NativeError,
                        match="Native model response ended without complete supported output",
                    ):
                        async with timeout(4):
                            await alpha.get_result()
                    assert cleaned.is_set() and not release_automatic.is_set()
                    assert len(peer.child_requests["alpha"]) == sampled

                    # Failure belongs to this exact child turn, not the graph.
                    release_beta.set()
                    assert (await beta.get_result()).final_output == "beta complete"
                    later_root = await session.submit("Continue work in the root")
                    assert (await later_root.get_result()).final_output == "root complete"
                    fresh = await alpha_row.start("Read a fresh child response")
                    assert fresh.turn_id != alpha.turn_id
                    assert (await fresh.get_result()).final_output == "alpha recovered"
                    assert receipt_text not in json.dumps(peer.child_requests["alpha"][-1]["input"])
                finally:
                    release_receipt.set()
                    release_beta.set()
                    release_automatic.set()
        finally:
            await agent.provider.aclose()


def assert_stored_child_preserved(actual: CheckpointThread, expected: CheckpointThread) -> None:
    assert actual.model_dump(exclude={"history", "metadata"}) == expected.model_dump(
        exclude={"history", "metadata"}
    )
    assert actual.history[1:] == expected.history[1:]
    # Restore rebuilds the native header and uses the destination provider.
    assert actual.history[0]["type"] == expected.history[0]["type"] == "session_meta"
    for field in ("id", "parent_thread_id", "base_instructions"):
        assert actual.history[0]["payload"][field] == expected.history[0]["payload"][field]
    assert (actual.history[0]["payload"].get("dynamic_tools") or []) == (
        expected.history[0]["payload"].get("dynamic_tools") or []
    )
    for field in (
        "thread_id",
        "parent_thread_id",
        "forked_from_id",
        "created_at",
        "updated_at",
        "recency_at",
        "name",
        "preview",
        "first_user_message",
        "model",
        "reasoning_effort",
        "cwd",
        "approval_mode",
        "permission_profile",
        "agent_nickname",
        "agent_role",
        "agent_path",
        "token_usage",
        "extra_config",
        "originator",
        "source",
        "history_mode",
        "thread_source",
    ):
        assert actual.metadata[field] == expected.metadata[field], field


async def test_stored_terminal_child_survives_manual_reseal_with_legacy_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = TerminalChildPeer(("default",))
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(65),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        agent = replace(agent, tools=(*agent.tools, lookup))
        try:
            async with agent.session(environment=declaration(executor)) as session:
                root = await session.submit("Start the review")
                assert (await (root).get_result()).final_output == "root complete"
                await peer.entered["alpha"].wait()
                child = (await handles(session, 1))[0]
                peer.release["alpha"].set()
                assert (await (child).get_result()).final_output == "alpha complete"
                peer.action = function_call("lookup", {"key": "resident"}, "loaded-manual")
                await session.submit("Look up a record")
                resident_request = await next_request(session.interactions(), lookup)
                while not await session._native._manual_ready():
                    await asyncio.sleep(0)
                resident = await session.suspend()
                assert resident.format_version == 5
                assert resident.descendants[0].status == "active"
                assert resident.interrupted_thread_ids == []
                assert child.child_id is not None
                raw = json.loads(resident.to_json())
                raw["descendants"][0]["checklist"] = legacy_checklist(child.child_id, child.turn_id)
                raw["descendants"][0]["goal"] = legacy_goal(
                    child.child_id, objective="Audit the completed child"
                )
                resident = SessionState.from_json(json.dumps(raw))
            async with agent.session(state=resident, environment=declaration(executor)) as session:
                current = await next_request(session.interactions(), lookup)
                assert (current.call_id, current.turn_id) == (
                    resident_request.call_id,
                    resident_request.turn_id,
                )
                await current.respond("resident-answer")
                assert (await root.get_result()).final_output == "root complete"
                recovered = session.restored_turn
                assert recovered is not None
                await recovered.get_result()
                ordinary = await session.save_state()
                assert ordinary.format_version == 4
                original_child = ordinary.descendants[0]
                assert original_child.status == "active"
                checklist = original_child.checklist
                assert checklist is not None and checklist.thread_id == child.child_id
                assert [(step.step, step.status) for step in checklist.plan] == [
                    ("Read report", "completed"),
                    ("Write summary", "completed"),
                ]
                original_requests = len(peer.requests)

            async with agent.session(state=ordinary, environment=declaration(executor)) as session:
                rows = [item async for item in session.subagents()]
                assert len(rows) == 1 and rows[0].turn is None
                peer.action = function_call("lookup", {"key": "A"}, "root-manual")
                root = await session.submit("Look up A")
                original = await next_request(session.interactions(), lookup)
                assert original.subagent_id is None
                while True:
                    try:
                        manual = await session.suspend()
                        break
                    except RuntimeError as error:
                        if "not a settled manual boundary" not in str(error):
                            raise
                        await asyncio.sleep(0)
                assert manual.format_version == 5
                assert manual.interrupted_thread_ids == []
                assert len(peer.requests) == original_requests + 1
                assert len(manual.descendants) == len(ordinary.descendants) == 1
                assert_stored_child_preserved(manual.descendants[0], original_child)

            # Legacy descendant data survives another manual suspension.
            async with agent.session(state=manual, environment=declaration(executor)) as session:
                rows = [item async for item in session.subagents()]
                assert len(rows) == 1 and rows[0].turn is None
                resealed = await session.suspend()
                stored = resealed.descendants[0]
                assert (
                    stored.thread_id,
                    stored.parent_thread_id,
                    stored.status,
                    stored.closed_at,
                ) == (
                    original_child.thread_id,
                    original_child.parent_thread_id,
                    original_child.status,
                    original_child.closed_at,
                )
                goal = stored.goal
                assert goal is not None
                assert (goal.thread_id, goal.objective, goal.status) == (
                    child.child_id,
                    "Audit the completed child",
                    "paused",
                )
                assert stored.checklist == checklist
                assert stored.history[1 : len(original_child.history)] == original_child.history[1:]
                assert len(peer.requests) == original_requests + 1

            async with agent.session(
                state=SessionState.from_json(resealed.to_json()),
                environment=declaration(executor),
            ) as session:
                rows = [item async for item in session.subagents()]
                assert len(rows) == 1 and rows[0].turn is None
                request = await next_request(session.interactions(), lookup)
                assert (request.call_id, request.turn_id) == (original.call_id, original.turn_id)
                with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
                    await original.respond("stale")
                await request.respond("answer-A")
                recovered = session.restored_turn
                assert recovered is not None
                assert (await recovered.get_result()).final_output == "root complete"
                completed = await session.save_state()
                assert len(peer.requests) == original_requests + 2
                assert tool_output(peer.requests[-1], "root-manual") == "answer-A"
                assert len(peer.child_requests["alpha"]) == 1
                assert len(completed.descendants) == len(resealed.descendants) == 1
                assert_stored_child_preserved(completed.descendants[0], stored)
        finally:
            for event in peer.release.values():
                event.set()
            await agent.provider.aclose()


@pytest.mark.parametrize(
    "retained",
    ["code_state", pytest.param("live_terminal", marks=pytest.mark.requires_native_linux_sandbox)],
)
async def test_completed_child_settled_values_transfer_but_live_terminal_keeps_manual_reply_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retained: str
) -> None:
    class RetainingPeer(ChildPeer):
        def response(self) -> list[dict[str, Any]]:
            result = super().response()
            if retained == "code_state" and result[0]["item"].get("name") == "exec_command":
                return [code_call('store("retained", 17); text("remembered");', "shared-call-id")]
            return result

    peer = RetainingPeer(("default",))
    if retained == "live_terminal":
        peer.command = "sleep 30"
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(45),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        agent = replace(agent, tools=(*agent.tools, lookup))
        agent = replace(agent, programmatic_tool_calling=retained == "code_state")
        if agent.programmatic_tool_calling:
            agent = replace(agent, model=model_restore_agent(peer, executor).model)
        try:
            async with agent.session(environment=declaration(executor)) as session:
                root = await session.submit("Use one child")
                assert (await (root).get_result()).final_output == "root complete"
                await peer.entered["alpha"].wait()
                child = (await handles(session, 1))[0]
                peer.release["alpha"].set()
                assert (await (child).get_result()).final_output == "alpha complete"
                original = await session._native._child_turn(child.child_id)
                peer.action = function_call("lookup", {"key": "A"}, "call-A")
                root = await session.submit("Look up A")
                request = await next_request(session.interactions(), lookup)
                if retained == "code_state":
                    saved = await session.suspend()
                    [stored_child] = saved.descendants
                    assert stored_child.code_mode["stored_values"]["retained"] == 17
                    with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
                        await request.respond("stale")
                else:
                    with pytest.raises(RuntimeError, match="live background terminals"):
                        await session.suspend()
                    current = await session._native._child_turn(child.child_id)
                    assert current.instance_id == original.instance_id
                    assert await request.respond("answer-A") is None
                    assert (await (root).get_result()).final_output == "root complete"
            if retained == "code_state":
                async with agent.session(
                    state=saved, environment=declaration(executor)
                ) as restored:
                    current = await next_request(restored.interactions(), lookup)
                    await current.respond("answer-A")
                    assert (
                        await restored.restored_turn.get_result()
                    ).final_output == "root complete"
        finally:
            peer.release["alpha"].set()
            await agent.provider.aclose()


async def test_busy_sibling_refuses_before_retiring_completed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(45),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        agent = replace(agent, tools=(*agent.tools, lookup))
        try:
            async with agent.session(environment=declaration(executor)) as session:
                first = await session.submit("Use two children")
                assert (await (first).get_result()).final_output == "root complete"
                await asyncio.gather(*(event.wait() for event in peer.entered.values()))
                by_id = {handle.child_id: handle for handle in await handles(session, 2)}
                alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
                beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
                peer.release["alpha"].set()
                assert (await (alpha).get_result()).final_output == "alpha complete"
                original = await session._native._child_turn(alpha.child_id)
                peer.action = function_call("lookup", {"key": "A"}, "call-A")
                await session.submit("Look up A")
                request = await next_request(session.interactions(), lookup)
                with pytest.raises(RuntimeError, match="child work or callbacks remain active"):
                    await session.suspend()
                current = await session._native._child_turn(alpha.child_id)
                assert current.instance_id == original.instance_id
                peer.release["beta"].set()
                assert (await (beta).get_result()).final_output == "beta complete"
                while not await session._native._manual_ready():
                    await asyncio.sleep(0)
                saved = await session.suspend()
                assert len(saved.descendants) == 2
                assert all(child.status == "active" for child in saved.descendants)
            async with agent.session(state=saved, environment=declaration(executor)) as session:
                current_request = await next_request(session.interactions(), lookup)
                assert current_request.call_id == request.call_id
                await current_request.respond("answer-A")
                assert (await session.restored_turn.get_result()).final_output == "root complete"
            assert len(peer.child_requests["alpha"]) == len(peer.child_requests["beta"]) == 2
        finally:
            for event in peer.release.values():
                event.set()
            await agent.provider.aclose()

"""A settled native fork never carries an outstanding code-cell receipt."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from contextlib import aclosing

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    HttpMCP,
    NativeAgents,
    NativeError,
    NativeProvider,
    NativeRole,
    StopTurn,
    TurnStoppedError,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_child_agents import collaboration, handles, trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer
from tests.test_review_ii_code_cell_interrupt import CellGraph, inspect_and_reuse, yielded_cell


async def test_fork_refuses_live_cell_then_source_waits_and_forks_settled(responses):
    responses.respond(code_call("yield_control(); await new Promise(() => {});", "yielded-root"))
    responses.respond(assistant_output("root complete"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-fork-cells")
    agent = Agent(
        instructions="Only inspect the caller's native code work.",
        provider=provider,
        programmatic_tool_calling=True,
        model="gpt-5.5",
    )
    async with timeout(40), agent.session() as source:
        assert (
            await (await source.submit("Start one yielded code cell")).get_result()
        ).final_output == ("root complete")
        receipt = code_output(responses.requests[-1], "yielded-root")
        assert receipt.startswith("Script running with cell ID "), receipt
        cell = receipt.removeprefix("Script running with cell ID ").split()[0].rstrip(".")
        assert source.active_turn is None
        # No background work of a separate Session may veto a settled fork.
        responses.respond(assistant_output("unrelated complete"))
        async with agent.session() as unrelated:
            assert (
                await (await unrelated.submit("A different independent owner")).get_result()
            ).final_output == "unrelated complete"
            separate = await unrelated.fork()
            assert separate.source_session_id != unrelated.id
        before = len(responses.requests)
        with pytest.raises(RuntimeError, match="fork.*(cell|code)"):
            await source.fork()
        assert len(responses.requests) == before
        # Refusal neither terminates nor silently consumes the cell. A real
        # native wait still resolves the original ID, then terminates it.
        responses.respond(
            function_call("wait", {"cell_id": cell, "yield_time_ms": 1}, "still-live")
        )
        responses.respond(assistant_output("source alive"))
        assert (
            await (await source.submit("Inspect that cell")).get_result()
        ).final_output == "source alive"
        assert tool_output(responses.requests[-1], "still-live").startswith(
            "Script running with cell ID "
        )
        responses.respond(function_call("wait", {"cell_id": cell, "terminate": True}, "retire"))
        responses.respond(assistant_output("source retired cell"))
        assert (await (await source.submit("Terminate the cell")).get_result()).final_output == (
            "source retired cell"
        )
        retired = tool_output(responses.requests[-1], "retire")
        assert "not found" not in retired and "Script running with cell ID " not in retired
        settled = await source.fork()
        assert settled.source_session_id != source.id
        responses.respond(assistant_output("current owner"))
        async with agent.session(state=settled) as destination:
            value = await (await destination.submit("Continue settled work")).get_result()
            assert value.final_output == "current owner"
            assert "source retired cell" in json.dumps(responses.requests[-1]["input"])


async def test_completed_code_cell_does_not_prevent_a_native_fork(responses):
    responses.respond(code_call("text('real-native-done');", "settled-cell"))
    responses.respond(assistant_output("code finished"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-settled-cell")
    agent = Agent(
        instructions="Use the supplied summary of completed code.",
        provider=provider,
        programmatic_tool_calling=True,
        model="gpt-5.5",
    )
    async with timeout(35), agent.session() as source:
        assert (
            await (await source.submit("Use code")).get_result()
        ).final_output == "code finished"
        assert "real-native-done" in code_output(responses.requests[-1], "settled-cell")
        settled = await source.fork()
        responses.respond(assistant_output("continued"))
        async with agent.session(state=settled) as destination:
            assert (
                await (await destination.submit("Continue")).get_result()
            ).final_output == "continued"
            current = json.dumps(responses.requests[-1]["input"])
            assert "real-native-done" in current and "code finished" in current


async def test_idle_child_yielded_cell_blocks_parent_fork_without_retiring_source(
    tmp_path, monkeypatch
):
    peer = CellGraph()
    # The parent has no cells. A fork cannot copy its descendant graph even
    # after the child retires its receipt; a settled save must retain that graph.
    peer.programs["/root"] = deque(
        [
            [
                collaboration(
                    "spawn_agent",
                    {
                        "task_name": "alpha",
                        "message": "Start one background cell and yield.",
                        "agent_type": "default",
                        "fork_turns": "none",
                    },
                    "spawn-alpha",
                )
            ],
            [assistant_output("root idle")],
        ]
    )
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(50),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Perform only the requested native work.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[NativeAgents(max_children=2)],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor)) as source:
            root = await source.submit("Start an alpha child; do not use code yourself.")
            assert (await root.get_result()).final_output == "root idle"
            [child_turn] = await handles(source, 1)
            assert (await child_turn.get_result()).final_output == "alpha idle"
            [child] = [entry async for entry in source.subagents()]
            assert source.active_turn is None and child.is_live
            child_requests = peer.by_thread["/root/alpha"]
            cell = yielded_cell(child_requests[-1], "alpha")
            before = len(peer.requests)
            with pytest.raises(RuntimeError, match="fork.*descendant"):
                await source.fork()
            assert len(peer.requests) == before
            # An actual wait in the original native child proves the refusal
            # neither stops it nor silently consumes its cell. Fresh JS also
            # observes the stored value left by that owner.
            await inspect_and_reuse(
                child, peer.programs["/root/alpha"], child_requests, "alpha", cell, stopped=False
            )
            peer.programs["/root/alpha"].extend(
                [
                    [function_call("wait", {"cell_id": cell, "terminate": True}, "child-retire")],
                    [assistant_output("child retired cell")],
                ]
            )
            assert (await (await child.start("Retire that cell")).get_result()).final_output == (
                "child retired cell"
            )
            retired = tool_output(child_requests[-1], "child-retire")
            assert "not found" not in retired and "Script running with cell ID " not in retired
            # Parent can continue without replaying either child's prior turn.
            previous_child_requests = len(child_requests)
            peer.programs["/root"].append([assistant_output("parent still usable")])
            assert (
                await (await source.submit("Continue parent only")).get_result()
            ).final_output == "parent still usable"
            assert len(child_requests) == previous_child_requests
            before = len(peer.requests)
            with pytest.raises(RuntimeError, match="fork.*descendant"):
                await source.fork()
            settled = await source.save_state()
            assert settled.source_session_id == source.id
            [saved_child] = [row for row in settled.descendants if row.thread_id == child.id]
            assert saved_child.parent_thread_id == source.id and saved_child.history
            assert len(peer.requests) == before


async def test_stopped_native_code_fork_rebinds_without_replaying_approval(responses):
    selected = Peer(names=("find-record",), label="source")
    decisions = []

    def approval(ctx, value):
        decisions.append(ctx)
        return StopTurn("private-fork-stop-reason")

    async with (
        timeout(40),
        selected.serve() as url,
        aclosing(
            NativeProvider(
                base_url=responses.url, api_key="synthetic-fork-stop", supports_websockets=False
            )
        ) as provider,
    ):
        tool = HttpMCP(name="vault", url=url, tools=["find-record"], approval=approval)
        agent = Agent(
            instructions="Use only the requested work.",
            provider=provider,
            tools=[tool],
            programmatic_tool_calling=True,
        )
        async with agent.session() as source:
            responses.respond(
                code_call(
                    'try {await tools.mcp__vault__find_record({key:"source"});} catch(e) {}'
                    'text("AFTER-STOP");',
                    "stopped-cell",
                )
            )
            stopped = await source.submit("Run the selected cell.")
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                await stopped.get_result()
            assert len(decisions) == 1 and not selected.calls
            assert decisions[0].submission is stopped
            assert source.active_turn is None
            snapshot = await source.fork()
            assert snapshot.source_session_id != source.id
            # Original bindings and approvals are not the destination's authority.
            async with agent.session(state=snapshot, tools=[]) as destination:
                responses.respond(assistant_output("forked without replay"))
                assert (
                    await (await destination.submit("Continue independently")).get_result()
                ).final_output == ("forked without replay")
                assert len(decisions) == 1 and not selected.calls
                assert "private-fork-stop-reason" not in json.dumps(responses.requests)
            responses.respond(assistant_output("source remains valid"))
            assert (await (await source.submit("Continue source")).get_result()).final_output == (
                "source remains valid"
            )
            assert len(decisions) == 1 and not selected.calls


@pytest.mark.parametrize("spawn", ["legacy-unqualified", "v2"])
async def test_child_policy_stop_leaves_sibling_cell_as_the_only_fork_barrier(
    tmp_path, monkeypatch, spawn
):
    peer = CellGraph()
    peer.by_thread["/root/beta"] = []
    make_spawn = (
        (
            lambda name, role, call: function_call(
                "spawn_agent", {"task": "Start " + name, "agent_type": role}, call
            )
        )
        if spawn == "legacy-unqualified"
        else (
            lambda name, role, call: collaboration(
                "spawn_agent",
                {
                    "task_name": name,
                    "message": "Start one background cell: " + name,
                    "agent_type": role,
                    "fork_turns": "none",
                },
                call,
            )
        )
    )
    peer.programs["/root"] = deque(
        [
            [make_spawn("alpha", "default", "alpha-start")],
            [make_spawn("beta", "research", "beta-start")],
            [assistant_output("root idle")],
        ]
    )
    peer.programs["/root/alpha"] = deque(
        [
            [code_call("yield_control(); await new Promise(() => {});", "alpha-live")],
            [
                code_call(
                    'text(await tools.exec_command({cmd:"printf blocked > forbidden"}));',
                    "alpha-stop",
                )
            ],
        ]
    )
    peer.programs["/root/beta"] = deque(
        [
            [code_call("yield_control(); await new Promise(() => {});", "beta-live")],
            [assistant_output("beta idle")],
        ]
    )
    decisions = []

    def approval(ctx, value):
        decisions.append(ctx)
        return StopTurn("private-child-fork-reason") if ctx.agent_role == "default" else True

    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(55),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with aclosing(peer.provider(websocket=False)) as native:
            agent = Agent(
                instructions="Run exactly the requested work.",
                model="gpt-6.1-sol",
                provider=native,
                programmatic_tool_calling=True,
                tools=[
                    NativeAgents(
                        additional_roles=[
                            NativeRole(name="research", description="Read", instructions="Read.")
                        ]
                    )
                ],
            )
            async with agent.session(
                environment=declaration(executor, approval=approval)
            ) as source:
                root = await source.submit("Spawn two native children.")
                if spawn == "legacy-unqualified":
                    assert (await root.get_result()).final_output == "root idle"
                    root_requests = peer.by_thread["/root"]
                    assert len(root_requests) == 3
                    for request, call_id in zip(
                        root_requests[1:], ("alpha-start", "beta-start"), strict=True
                    ):
                        denied = tool_output(request, call_id)
                        assert "unsupported call:" in denied and "spawn_agent" in denied
                    assert not decisions
                    assert not [child async for child in source.subagents()]
                    assert not any(
                        req.get("method") == "process/start" for req in executor.requests
                    )
                    assert not (executor.root / "forbidden").exists()
                    return
                assert (await root.get_result()).final_output == "root idle"
                turns = await handles(source, 2)
                results = await asyncio.gather(
                    *(turn.get_result() for turn in turns), return_exceptions=True
                )
                assert len(decisions) == 1 and not (executor.root / "forbidden").exists()
                assert decisions[0].agent is agent and decisions[0].session is source
                assert decisions[0].agent_role == "default"
                assert not any(req.get("method") == "process/start" for req in executor.requests)
                for turn, result in zip(turns, results, strict=True):
                    if turn.child_id == decisions[0].thread_id:
                        assert turn.turn_id == decisions[0].turn_id
                        assert isinstance(result, NativeError), result
                    else:
                        assert not isinstance(result, BaseException)
                        assert result.final_output == "beta idle"
                count = len(peer.requests)
                with pytest.raises(RuntimeError, match="fork.*descendant"):
                    await source.fork()
                assert len(peer.requests) == count
                beta_requests = peer.by_thread["/root/beta"]
                output = code_output(beta_requests[-1], "beta-live")
                assert output.startswith("Script running with cell ID "), output
                cell = output.removeprefix("Script running with cell ID ").split()[0].rstrip(".")
                [beta] = [
                    child
                    async for child in source.subagents()
                    if child.id != decisions[0].thread_id
                ]
                peer.programs["/root/beta"].extend(
                    [
                        [
                            function_call(
                                "wait", {"cell_id": cell, "terminate": True}, "beta-retire"
                            )
                        ],
                        [assistant_output("beta retired")],
                    ]
                )
                assert (
                    await (await beta.start("Retire only beta's cell.")).get_result()
                ).final_output == "beta retired"
                retired = tool_output(beta_requests[-1], "beta-retire")
                assert "not found" not in retired and "Script running with cell ID " not in retired
                before = len(peer.requests)
                with pytest.raises(RuntimeError, match="fork.*descendant"):
                    await source.fork()
                settled = await source.save_state()
                assert settled.source_session_id == source.id
                records = {row.thread_id: row for row in settled.descendants}
                assert {turn.child_id for turn in turns} <= records.keys()
                assert all(records[turn.child_id].history for turn in turns)
                assert len(peer.requests) == before
                assert (await root.get_result()).final_output == "root idle"
                assert len(decisions) == 1 and not (executor.root / "forbidden").exists()
                assert "private-child-fork-reason" not in json.dumps(peer.requests)

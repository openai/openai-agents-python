"""Caller-supplied context is published by Core at a settled owned boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool, ToolContext, function_tool
from agents.observation import SessionEnded
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses
from tests.test_native_agent_checkpoints import call, message, peer
from tests.test_native_instruction_updates import application_texts


async def test_supplied_summary_keeps_native_history_tools_and_round_trips():
    effects: list[str] = []

    @function_tool
    async def record(ctx: ToolContext[str]) -> str:
        effects.append(ctx.context)
        return "receipt:" + ctx.context

    async with timeout(30), peer() as wire:
        agent = Agent[str, str](instructions="Original.", tools=[record], provider=wire.provider())
        wire.replies.extend((call("record", "settled-call"), message("settled", "settled-answer")))
        async with agent.session(context="live") as session:
            stream = session.events()
            owner_id = session.id
            await session.update(instructions="Current.")
            original = await session.submit("Remember this long original input.")
            completed = await original.get_result()
            before = await session.save_state()
            items = [item async for item in session.items()]
            sent = len(wire.requests)
            assert await session.compact(summary="Supplied short version: atlas-17.") is None
            assert len(wire.requests) == sent
            assert session.id == owner_id and await original.get_result() is completed
            assert [item async for item in session.items()] == items
            saved = await session.save_state()
            assert saved.history[: len(before.history)] == before.history
            assert saved.instructions == "Current."
            compact = next(
                r["payload"] for r in reversed(saved.history) if r["type"] == "compacted"
            )
            assert compact["message"] == "Supplied short version: atlas-17."
            assert any(
                row.get("metadata") is not None
                for row in saved.history
                if row["type"] == "response_item"
            )
            wire.replies.extend(
                (call("record", "following-call"), message("following", "following-answer"))
            )
            outcome = await (await session.submit("Work now.")).get_result()
            assert outcome.final_output == "following"
            consumed = wire.requests[sent]
            assert "Supplied short version: atlas-17." in json.dumps(consumed["input"])
            assert "Remember this long original input." not in json.dumps(consumed["input"])
            assert application_texts(consumed)[-1] == "Current."
            state = SessionState.from_json((await session.save_state()).to_json())
        events = [event async for event in stream]
        assert isinstance(events[-1], SessionEnded)
        assert effects == ["live", "live"]
        agent = replace(agent, instructions="Current.")
        wire.replies.extend(
            (call("record", "restored-call"), message("restored", "restored-answer"))
        )
        async with agent.session(context="fresh", state=state) as restored:
            assert (
                await (await restored.submit("Use current context")).get_result()
            ).final_output == "restored"
        assert effects == ["live", "live", "fresh"]


async def test_supplied_repeated_and_ordinary_compaction_use_their_own_paths():
    async with timeout(30), peer() as wire:
        agent = Agent(instructions="Answer.", provider=wire.provider())
        async with agent.session() as session:
            await session.compact(summary="Short empty context.")
            await session.compact(summary="Updated caller summary.")
            assert wire.requests == []
            wire.replies.append(message("Core-generated compacted summary.", "model-summary"))
            await session.compact()
            assert len(wire.requests) == 1
            wire.replies.append(message("continued", "after-model-compact"))
            assert (await (await session.submit("Next.")).get_result()).final_output == "continued"
            assert "Core-generated compacted summary." in json.dumps(wire.requests[-1]["input"])
            assert (
                sum(row["type"] == "compacted" for row in (await session.save_state()).history) == 3
            )


@pytest.mark.parametrize(
    "bad,exception", [("", ValueError), ("  \n\t ", ValueError), (42, TypeError), ([], TypeError)]
)
async def test_invalid_supplied_summaries_leave_session_usable(bad, exception):
    async with timeout(20), peer() as wire:
        async with Agent(instructions="Answer.", provider=wire.provider()).session() as session:
            before = await session.save_state()
            with pytest.raises(exception, match="summary"):
                await session.compact(summary=bad)
            assert wire.requests == []
            assert not any(
                row["type"] == "compacted" for row in (await session.save_state()).history
            )
            assert (await session.save_state()).source_session_id == before.source_session_id
            wire.replies.append(message("valid"))
            assert (await (await session.submit("next")).get_result()).final_output == "valid"


async def test_manual_boundary_refuses_without_consuming_or_sealing():
    class Empty(BaseModel):
        pass

    manual = FunctionTool(name="record", description="Record", input_type=Empty, output_type=str)
    async with timeout(20), peer() as wire:
        wire.replies.extend((call("record", "pending"), message("completed", "after-pending")))
        async with Agent(
            instructions="Record.", tools=[manual], provider=wire.provider()
        ).session() as session:
            turn = await session.submit("record now")
            async for event in session.interactions():
                action = manual.match(event)
                assert action is not None
                with pytest.raises(RuntimeError, match="busy|active|idle"):
                    await session.compact(summary="Never omit this call.")
                await action.respond("accepted")
                break
            assert (await turn.get_result()).final_output == "completed"
            assert not any(
                row["type"] == "compacted" for row in (await session.save_state()).history
            )
            assert len(wire.requests) == 2


async def test_callback_cannot_compact_its_own_session():
    sessions = []

    @function_tool
    async def inner() -> str:
        with pytest.raises(RuntimeError, match="callback"):
            await sessions[0].compact(summary="Do not change the calling turn.")
        return "accepted"

    async with timeout(20), peer() as wire:
        wire.replies.extend((call("inner", "callback"), message("finished", "answer")))
        async with Agent(
            instructions="Call inner.", tools=[inner], provider=wire.provider()
        ).session() as session:
            sessions.append(session)
            assert (await (await session.submit("run")).get_result()).final_output == "finished"
            assert not any(
                row["type"] == "compacted" for row in (await session.save_state()).history
            )


async def test_cancellation_joins_source_fence_and_cannot_compact_a_successor(monkeypatch):
    from agents import _native

    entered, release = asyncio.Event(), asyncio.Event()
    original = _native.NativeThread._begin_context_change

    async def hold(thread):
        owned = await original(thread)
        entered.set()
        await release.wait()
        return owned

    monkeypatch.setattr(_native.NativeThread, "_begin_context_change", hold)
    async with timeout(20), peer() as wire:
        async with Agent(instructions="Answer.", provider=wire.provider()).session() as session:
            pending = asyncio.create_task(session.compact(summary="Before cancellation."))
            await entered.wait()
            pending.cancel("caller")
            await asyncio.sleep(0)
            assert not pending.done()
            for conflicting in (
                lambda: session.submit("too early"),
                session.save_state,
                lambda: session.submit("too early", tools=[]),
                lambda: session.compact(summary="too early"),
            ):
                with pytest.raises(
                    RuntimeError, match="busy|mutation|changing|checkpoint|active|idle"
                ):
                    await conflicting()
            pending.cancel("again")
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            monkeypatch.setattr(_native.NativeThread, "_begin_context_change", original)
            wire.replies.append(message("successor", "next-answer"))
            next_turn = await session.submit("next input")
            assert (await next_turn.get_result()).final_output == "successor"
            assert "Before cancellation." in json.dumps(wire.requests[-1]["input"])
            assert (
                sum(row["type"] == "compacted" for row in (await session.save_state()).history) == 1
            )


async def test_supplied_preserves_current_mcp_credentials_disabled_roster_and_result_policy(
    responses,
):
    from agents import HttpMCP, MCPResultContext
    from tests.native_fixtures import assistant_output, tool_output
    from tests.test_native_agent import provider
    from tests.test_native_mcp_result_policies import result
    from tests.test_native_mcp_tools import Peer, declarations, mcp_call

    calls = []

    async def redact(ctx: MCPResultContext[str], value: dict) -> dict:
        calls.append((ctx.context, ctx.call_id))
        return result("safe:" + ctx.context)

    main, unused = Peer(label="private value"), Peer()
    async with timeout(30), main.serve() as url, unused.serve() as dormant_url:
        first = HttpMCP(
            name="docs",
            url=url,
            tools=["search"],
            bearer_token="first",
            transform_result=redact,
        )
        reserved = HttpMCP(name="dormant", url=dormant_url, tools=["search"])
        current = replace(first, bearer_token="current")
        roster = [current, reserved]
        agent = Agent[str, str](
            instructions="Read.", tools=roster, model="gpt-5.5", provider=provider(responses)
        )
        async with agent.session(context="tenant") as session:
            responses.respond(mcp_call("search", "mcp__docs", "old-effect"))
            responses.respond(assistant_output("old receipt completed"))
            assert (
                await (await session.submit("Read", tools=[current])).get_result()
            ).final_output == "old receipt completed"
            sent = len(responses.requests)
            observed_auth = len(main.auth)
            await session.compact(summary="One previous read completed.")
            assert not unused.requests and len(main.calls) == len(calls) == 1
            assert all(value == "Bearer current" for value in main.auth[observed_auth:])
            assert len(responses.requests) == sent
            responses.respond(mcp_call("search", "mcp__docs", "new-effect"))
            responses.respond(assistant_output("new receipt completed"))
            assert (
                await (await session.submit("Read again", tools=[current])).get_result()
            ).final_output == "new receipt completed"
            assert ("mcp__dormant", "search") not in declarations(responses.requests[sent])
            assert "safe:tenant" in str(tool_output(responses.requests[-1], "new-effect"))
            assert calls == [("tenant", "old-effect"), ("tenant", "new-effect")]
            saved = await session.save_state()
            assert "Bearer current" not in saved.to_json()
        assert len(main.calls) == 2 and not unused.requests


async def test_supplied_keeps_source_lease_workspace_and_updated_model_until_final_close(
    tmp_path, responses, monkeypatch
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.native_fixtures import assistant_output
    from tests.test_environment_leases_checklist import Source, trust
    from tests.test_native_agent import provider

    async with timeout(45), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        source = Source(executor)
        owner = provider(responses)
        agent = Agent(
            instructions="Write in the allowed workspace.", model="gpt-5.5", provider=owner
        )
        async with agent.session(environment=source.lease()) as session:
            await session.update(model="gpt-5.4", model_settings={"reasoning_effort": "high"})
            responses.respond(assistant_output("opened"))
            assert (await (await session.submit("Inspect.")).get_result()).final_output == "opened"
            requests = len(executor.requests)
            for index in range(3):
                retired_engine = session._engine
                await session.compact(summary=f"The remote workspace was inspected. Pass {index}.")
                # The private owner must reject entry before allocating a future.
                # A closed EngineState alone rejects only after that future starts.
                try:
                    pending = retired_engine.start_replaceable_thread("{}", "[]")
                except RuntimeError as error:
                    assert str(error) == "Codex engine is closed"
                else:
                    with pytest.raises(RuntimeError, match="Codex engine is closed"):
                        await pending
                    pytest.fail("Retired native Engine owner still retains its runtime state")
            assert (source.opened, source.released) == (1, 0)
            with pytest.raises(RuntimeError, match="Session|open"):
                await owner.aclose()
            # Restored native work still reads the same authenticated peer and
            # cwd, even though the Source itself must not be acquired again.
            assert any(
                req["method"] == "environmentConfig/read"
                and req["params"]["cwd"] == executor.root.as_uri()
                for req in executor.requests[requests:]
            )
            responses.respond(assistant_output("still current"))
            assert (
                await (await session.submit("Continue")).get_result()
            ).final_output == "still current"
            assert responses.requests[-1]["model"] == "gpt-5.4"
            assert "remote workspace was inspected" in json.dumps(responses.requests[-1]["input"])
            assert "Bearer " + executor.token not in (await session.save_state()).to_json()
        assert (source.opened, source.released) == (1, 1)
        await owner.aclose()


async def test_live_children_refuse_before_native_mutation_and_finish_normally(
    tmp_path, monkeypatch
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_output, child_peer, handles, trust
    from tests.test_native_executor_tools import declaration

    async with (
        timeout(40),
        child_peer(("default", "research")) as wire,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(wire, executor).session(environment=declaration(executor)) as session:
            root = await session.submit("two children")
            await asyncio.gather(*(event.wait() for event in wire.entered.values()))
            assert (await root.get_result()).final_output == "root complete"
            owned = await handles(session, 2)
            with pytest.raises(RuntimeError, match="child|children|progress|idle"):
                await session.compact(summary="Do not take child work away.")
            for release in wire.release.values():
                release.set()
            assert set(await asyncio.gather(*(child_output(session, turn) for turn in owned))) == {
                "alpha complete",
                "beta complete",
            }
            # Both original native child turns continued after the refusal;
            # their execution result is retained, never copied to a new owner.
            for name in ("alpha", "beta"):
                assert wire.child_metadata[name][0]["root_turn_id"] == root.turn_id
                assert len(wire.child_requests[name]) == 2


async def test_supplied_refuses_running_native_code_without_retiring_source(responses):
    from tests.native_fixtures import assistant_output, function_call, tool_output
    from tests.test_native_agent import provider
    from tests.test_native_code_mode import code_call, code_output

    responses.respond(code_call("yield_control(); await new Promise(() => {});", "running"))
    responses.respond(assistant_output("root idle"))
    agent = Agent(
        instructions="Keep the original native cell waitable.",
        provider=provider(responses),
        model="gpt-5.5",
        programmatic_tool_calling=True,
    )
    async with timeout(35), agent.session() as session:
        assert (
            await (await session.submit("Start a cell.")).get_result()
        ).final_output == "root idle"
        receipt = code_output(responses.requests[-1], "running")
        assert receipt.startswith("Script running with cell ID "), receipt
        cell = receipt.removeprefix("Script running with cell ID ").split()[0].rstrip(".")
        assert session.active_turn is None
        with pytest.raises(RuntimeError, match="compact.*(cell|code|programmatic)|background"):
            await session.compact(summary="A cell is still outstanding.")
        responses.respond(function_call("wait", {"cell_id": cell, "yield_time_ms": 1}, "peek"))
        responses.respond(assistant_output("still usable"))
        assert (
            await (await session.submit("Inspect it.")).get_result()
        ).final_output == "still usable"
        assert tool_output(responses.requests[-1], "peek").startswith(
            "Script running with cell ID "
        )
        responses.respond(function_call("wait", {"cell_id": cell, "terminate": True}, "retire"))
        responses.respond(assistant_output("source retired cell"))
        assert (
            await (await session.submit("Retire it.")).get_result()
        ).final_output == "source retired cell"
        assert "not found" not in tool_output(responses.requests[-1], "retire")


async def test_unused_programmatic_declaration_accepts_summary_then_real_code_and_fork(responses):
    from agents import Agent, NativeProvider
    from tests.native_fixtures import assistant_output
    from tests.test_native_code_mode import code_call, code_output

    agent = Agent(
        instructions="Answer only as requested.",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-unused-code", supports_websockets=False
        ),
        programmatic_tool_calling=True,
    )
    async with timeout(35), agent.session() as session:
        responses.respond(assistant_output("native prior idle turn"))
        assert (await (await session.submit("Use no code here.")).get_result()).final_output == (
            "native prior idle turn"
        )
        source = session.id
        count = len(responses.requests)
        await session.compact(summary="User's summary before using code.")
        assert len(responses.requests) == count and session.id == source
        responses.respond(code_call('text("native VM ready after compaction");', "after-compact"))
        responses.respond(assistant_output("done after summary"))
        assert (
            await (await session.submit("Now run requested code.")).get_result()
        ).final_output == ("done after summary")
        assert "native VM ready after compaction" in code_output(
            responses.requests[-1], "after-compact"
        )
        assert "User's summary before using code." in json.dumps(responses.requests[-2]["input"])
        saved = await session.fork()
        assert saved.source_session_id != source


@pytest.mark.parametrize("already_compacted", [False, True], ids=["full", "model-compacted"])
async def test_supplied_after_completed_native_code_retains_live_vm_on_refusal(
    responses, already_compacted
):
    from agents import Agent, NativeProvider
    from tests.native_fixtures import assistant_output
    from tests.test_native_code_mode import code_call, code_output

    agent = Agent(
        instructions="Use only the requested native code.",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-used-code", supports_websockets=False
        ),
        programmatic_tool_calling=True,
    )
    async with timeout(35), agent.session() as session:
        responses.respond(code_call('store("before", "still-owned"); text("seeded");', "seed"))
        responses.respond(assistant_output("seed settled"))
        seed = await session.submit("Store a value.")
        assert (await seed.get_result()).final_output == "seed settled"
        assert "seeded" in code_output(responses.requests[-1], "seed")
        if already_compacted:
            responses.respond(assistant_output("Native compacted summary without the old code."))
            await session.compact()
        before = len(responses.requests)
        with pytest.raises(RuntimeError, match="compact.*(cell|code|programmatic)|background"):
            await session.compact(summary="Do not lose the stored value.")
        assert len(responses.requests) == before
        responses.respond(code_call('text(load("before"));', "read-original"))
        responses.respond(assistant_output("unchanged"))
        assert (
            await (await session.submit("Read the original value.")).get_result()
        ).final_output == ("unchanged")
        assert "still-owned" in code_output(responses.requests[-1], "read-original")


async def test_supplied_rejects_previously_compacted_programmatic_history(responses):
    from agents import NativeProvider
    from tests.native_fixtures import assistant_output

    agent = Agent(
        instructions="Answer only as requested.",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-compacted", supports_websockets=False
        ),
        programmatic_tool_calling=True,
    )
    async with timeout(35), agent.session() as session:
        responses.respond(assistant_output("First result."))
        await (await session.submit("Initial request.")).get_result()
        responses.respond(assistant_output("Ordinary native summary."))
        await session.compact()
        before = len(responses.requests)
        with pytest.raises(RuntimeError, match="compact.*(code|programmatic)|background"):
            await session.compact(summary="Cannot infer absent VM from the current summary.")
        assert len(responses.requests) == before
        responses.respond(assistant_output("Source remains usable."))
        assert (
            await (await session.submit("Continue the same session.")).get_result()
        ).final_output == ("Source remains usable.")


@pytest.mark.parametrize("pause_goal", [False, True], ids=["active", "paused"])
async def test_supplied_local_goal_refuses_without_downshifting_profile(
    responses, tmp_path, pause_goal
):
    from agents import Agent, LocalStorage, NativeGoals, NativeProvider
    from tests.native_fixtures import assistant_output, function_call

    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-local-compact", supports_websockets=False
    )
    agent = Agent(
        instructions="Only manage the requested goal.", provider=provider, tools=[NativeGoals()]
    )
    store = LocalStorage(tmp_path / "goals")
    async with timeout(35), provider:
        async with agent.session(storage=store) as session:
            responses.respond(
                function_call("create_goal", {"objective": "Keep the native goal"}, "g")
            )
            if pause_goal:
                responses.respond(function_call("update_goal", {"status": "paused"}, "pause"))
            responses.respond(assistant_output("goal remains active"))
            assert (await (await session.submit("Create the goal.")).get_result()).final_output == (
                "goal remains active"
            )
            identity = session.id
            goal = await session.goal()
            before = len(responses.requests)
            with pytest.raises(
                (ValueError, RuntimeError), match="local|LocalStorage|profile|portable"
            ):
                await session.compact(summary="Never replace the native local profile.")
            assert len(responses.requests) == before
            assert await session.goal() == goal
        async with agent.session(storage=store, resume=identity) as reopened:
            assert await reopened.goal() == goal


@pytest.mark.parametrize("failure", ["connection", "listing", "interaction"])
async def test_supplied_mcp_preflight_failure_leaves_source_usable(responses, failure):
    from agents import HttpMCP, InteractionRequired, NativeError
    from tests.native_fixtures import assistant_output
    from tests.test_native_agent import provider
    from tests.test_native_scoped_policies import AskingPeer

    remote = AskingPeer()
    async with timeout(30), remote.serve() as url:
        agent = Agent(
            instructions="Keep the source.",
            tools=[HttpMCP(name="records", url=url, tools=["search"])],
            provider=provider(responses),
        )
        async with agent.session() as session:
            responses.respond(assistant_output("original result"))
            assert (await (await session.submit("Original input.")).get_result()).final_output == (
                "original result"
            )
            before = await session.save_state()
            if failure == "connection":
                remote.denied = True
            elif failure == "listing":
                remote.names = ()
            else:
                remote.startup = True
            exception = InteractionRequired if failure == "interaction" else NativeError
            match = (
                "startup" if failure == "interaction" else "MCP.*(connection|listing|unavailable)"
            )
            with pytest.raises(exception, match=match):
                await session.compact(summary="Unpublished replacement.")
            if failure == "interaction":
                await remote.answered.wait()
                assert remote.prompted.is_set()
                assert remote.answers == [
                    {"id": 873, "jsonrpc": "2.0", "result": {"action": "cancel"}}
                ]
            assert not remote.calls and len(responses.requests) == 1
            retained = await session.save_state()
            assert retained.history[: len(before.history)] == before.history
            assert not any(row["type"] == "compacted" for row in retained.history)
            remote.startup = False
            remote.denied = False
            remote.names = ("search",)
            responses.respond(assistant_output("source survived"))
            successor = await session.submit("Continue original.")
            assert (await successor.get_result()).final_output == "source survived"
            assert "Original input." in json.dumps(responses.requests[-1]["input"])
            assert "Unpublished replacement." not in json.dumps(responses.requests[-1]["input"])
            await session.compact(summary="Accepted replacement.")
            assert any(row["type"] == "compacted" for row in (await session.save_state()).history)
            assert not remote.calls and len(responses.requests) == 2


async def test_supplied_setup_failure_remains_primary_when_candidate_cleanup_fails(
    responses, monkeypatch
):
    from agents import HttpMCP, NativeError, _native
    from tests.native_fixtures import assistant_output
    from tests.test_native_agent import provider
    from tests.test_native_scoped_policies import AskingPeer

    remote = AskingPeer()
    async with timeout(30), remote.serve() as url:
        agent = Agent(
            instructions="Keep the source.",
            tools=[HttpMCP(name="records", url=url, tools=["search"])],
            provider=provider(responses),
        )
        async with agent.session() as session:
            responses.respond(assistant_output("original result"))
            await (await session.submit("Original input.")).get_result()
            source_thread, source_engine = session._native, session._engine
            close_thread, close_engine = _native.NativeThread.close, _native.Engine.close
            closed = []

            async def thread_cleanup(value):
                await close_thread(value)
                if value is not source_thread:
                    closed.append("thread")
                    raise RuntimeError("synthetic candidate thread cleanup failure")

            async def engine_cleanup(value):
                await close_engine(value)
                if value is not source_engine:
                    closed.append("engine")
                    raise RuntimeError("synthetic candidate engine cleanup failure")

            remote.names = ()
            # Native supplies the setup rejection. Wrappers inject cleanup errors
            # only after actually closing each native candidate resource.
            with monkeypatch.context() as patch:
                patch.setattr(_native.NativeThread, "close", thread_cleanup)
                patch.setattr(_native.Engine, "close", engine_cleanup)
                with pytest.raises(NativeError, match="MCP.*(listing|unavailable)") as raised:
                    await session.compact(summary="Rejected replacement.")
            assert set(closed) == {"thread", "engine"}
            errors = []
            error = raised.value.__context__
            while error is not None:
                errors.append(str(error))
                error = error.__context__
            assert "synthetic candidate thread cleanup failure" in errors
            assert "synthetic candidate engine cleanup failure" in errors
            remote.names = ("search",)
            responses.respond(assistant_output("source survived"))
            assert (await (await session.submit("Continue.")).get_result()).final_output == (
                "source survived"
            )

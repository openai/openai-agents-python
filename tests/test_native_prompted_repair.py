"""Opt-in output correction: actual native owners, receipts and authority."""

from __future__ import annotations

import asyncio
import json
import shlex
from contextlib import AsyncExitStack
from itertools import groupby

import pytest
from pydantic import BaseModel, Field

from agents import (
    Agent,
    CapabilityError,
    HttpMCP,
    MCPResultContext,
    NativeError,
    NativeProvider,
    OutputValidationError,
    PromptedOutput,
    Session,
    SessionBusyError,
    TurnStoppedError,
    function_tool,
)
from agents.observation import TurnFinished, TurnStarted, UsageUpdated
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import function_declarations
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_mcp_tools import Peer


class Report(BaseModel):
    label: str = Field(min_length=2)


@pytest.mark.parametrize("policy_fails", [False, True])
async def test_yielded_original_mcp_result_during_repair_is_joined(responses, policy_fails):
    peer = Peer(names=("search",), release_call=asyncio.Event())
    at_repair, release_repair = asyncio.Event(), asyncio.Event()
    at_policy, release_policy = asyncio.Event(), asyncio.Event()
    seen = []
    invalid = assistant_output('{"label":"x"}')
    correction = assistant_output('{"label":"corrected"}')

    async def hold(event):
        if event == invalid:
            # Establish an admitted original call before ending the original turn.
            # A yielded cell alone does not prove the MCP peer has received it.
            await peer.call_entered.wait()
        if event == correction:
            at_repair.set()
            await release_repair.wait()

    async def after(ctx: MCPResultContext[None], value):
        seen.append(ctx)
        at_policy.set()
        await release_policy.wait()
        if policy_fails:
            raise ValueError("private original callback error")
        return value

    responses.before_event = hold
    responses.respond(
        code_call(
            'const pending = tools.mcp__records__search({key:"original"});'
            "yield_control(); text(await pending);",
            "original-cell",
        )
    )
    responses.respond(invalid)
    responses.respond(correction)
    try:
        async with timeout(35), peer.serve() as url:
            agent = Agent(
                instructions="original task",
                provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
                tools=[HttpMCP(name="records", url=url, tools=["search"], transform_result=after)],
                programmatic_tool_calling=True,
                output_type=PromptedOutput(Report, 1),
            )
            async with agent.session() as session:
                turn = await session.submit("read a record")
                result = asyncio.create_task(turn.get_result())
                await peer.call_entered.wait()
                await at_repair.wait()
                assert code_output(responses.requests[1], "original-cell").startswith(
                    "Script running with cell ID "
                )
                assert function_declarations(responses.requests[-1]) == []
                peer.release_call.set()
                await at_policy.wait()
                assert seen[0].submission is turn
                release_repair.set()
                # Completion is gated by the actual original callback, even
                # after the correction response has been delivered.
                assert not result.done()
                release_policy.set()
                if policy_fails:
                    with pytest.raises(NativeError, match="MCP result policy failed"):
                        await result
                else:
                    completed = await result
                    assert completed.final_output == Report(label="corrected")
                    assert len(completed.repair_attempts) == 1
                    assert await turn.get_result() is completed
                assert len(peer.calls) == len(seen) == 1
                assert len(responses.requests) == 3
    finally:
        peer.release_call.set()
        release_repair.set()
        release_policy.set()


@pytest.mark.parametrize("at_failure", ["opening", "invalid-correction", "active-repair"])
async def test_original_callback_failure_stops_further_repair_work(
    responses, monkeypatch, at_failure
):
    peer = Peer(names=("search",), release_call=asyncio.Event())
    at_repair, release_repair = asyncio.Event(), asyncio.Event()
    seen = []
    invalid_original = assistant_output('{"label":"x"}')
    invalid_correction = assistant_output('{"label":"y"}')

    async def after(ctx: MCPResultContext[None], value):
        seen.append(ctx)
        raise ValueError("private original callback error")

    async def hold(event):
        if event == invalid_original:
            # Both failure phases require a real original call before repair.
            # Receipt stays held until the selected repair phase below.
            await peer.call_entered.wait()
        if at_failure in {"invalid-correction", "active-repair"} and event == invalid_correction:
            at_repair.set()
            await release_repair.wait()

    responses.before_event = hold
    responses.respond(
        code_call(
            'const pending = tools.mcp__records__search({key:"original"});'
            "yield_control(); text(await pending);",
            "original-cell",
        )
    )
    responses.respond(invalid_original)
    responses.respond(invalid_correction)
    responses.respond(assistant_output('{"label":"unwanted-repair"}'))
    try:
        async with timeout(35), peer.serve() as url:
            agent = Agent(
                instructions="original task",
                provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
                tools=[HttpMCP(name="records", url=url, tools=["search"], transform_result=after)],
                programmatic_tool_calling=True,
                output_type=PromptedOutput(Report, 3),
            )
            async with agent.session() as session:
                original_open = Session._open

                async def hold_open(cls, *args, **kwargs):
                    repair = await original_open(*args, **kwargs)
                    if at_failure == "opening" and kwargs.get("_captured_api_key") is not None:
                        at_repair.set()
                        await release_repair.wait()
                    return repair

                monkeypatch.setattr(Session, "_open", classmethod(hold_open))
                turn = await session.submit("read a record")
                await peer.call_entered.wait()
                await at_repair.wait()
                count = 2 if at_failure == "opening" else 3
                assert len(responses.requests) == count
                assert code_output(responses.requests[1], "original-cell").startswith(
                    "Script running with cell ID "
                )
                peer.release_call.set()
                # Synchronize on the actual original callback failure before
                # releasing real native opening or the held model response.
                assert session._active is not None
                failure = await asyncio.shield(session._active.failure)
                assert isinstance(failure, NativeError)
                assert len(seen) == len(peer.calls) == 1 and seen[0].submission is turn
                if at_failure != "active-repair":
                    release_repair.set()
                # An original failure must stop a still-held billable repair.
                # Keep its provider response blocked through result delivery.
                try:
                    for _ in range(2):
                        async with timeout(3):
                            with pytest.raises(NativeError, match="MCP result policy failed"):
                                await turn.get_result()
                finally:
                    release_repair.set()
                assert len(responses.requests) == count
                if at_failure == "invalid-correction":
                    assert function_declarations(responses.requests[-1]) == []
    finally:
        peer.release_call.set()
        release_repair.set()


@pytest.mark.parametrize("value", [True, False, -1, 1.0, "2", None])
def test_repair_budget_rejects_non_integer_or_negative(value):
    with pytest.raises(ValueError if value == -1 else TypeError):
        PromptedOutput(Report, max_retries=value)


@pytest.mark.parametrize("budget, failures", [(0, 1), (1, 2), (3, 4)])
async def test_exhaustion_never_replays_and_repeated_waits_stay_settled(
    responses, tmp_path, budget, failures
):
    effect = tmp_path / "effect"

    @function_tool
    def record() -> str:
        with effect.open("a") as stream:
            stream.write("once\n")
        return "private-effect-receipt"

    responses.respond(function_call("record", {}, "original-call"))
    for i in range(failures):
        responses.respond(assistant_output(json.dumps({"label": str(i)})))
    agent = Agent(
        instructions="private-instructions",
        tools=[record],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, max_retries=budget),
    )
    async with timeout(30), agent.session() as session:
        turn = await session.submit("private-task")
        for _ in range(3):
            with pytest.raises(OutputValidationError) as failure:
                await turn.get_result()
            assert json.loads(failure.value.raw_output) == {"label": str(failures - 1)}
            assert "label" not in str(failure.value)
            assert failure.value.__context__ is None
        assert effect.read_text() == "once\n"
        assert len(responses.requests) == 1 + failures
        assert tool_output(responses.requests[1], "original-call") == "private-effect-receipt"
        turns = [t async for t in session.turns()]
        assert len(turns) == 1 + budget
        repairs = [t for t in turns if "parent_turn_id" in t]
        assert len(repairs) == budget
        assert all(t["parent_turn_id"] == turn.turn_id for t in repairs)
        assert len({(t["session_id"], t["id"]) for t in turns}) == len(turns)
        if budget:
            state = await session.save_state()
            assert len(state.repair_turns) == budget
        for request in responses.requests[2:]:
            assert function_declarations(request) == []
            actual = json.dumps(request)
            for private in ("private-task", "private-instructions", "private-effect-receipt"):
                assert private not in actual


async def test_repair_has_effective_updated_model_auth_accounting_and_real_history(
    responses, monkeypatch, tmp_path
):
    effect = tmp_path / "effect"

    @function_tool
    def record() -> str:
        effect.write_text("owned once")
        return "private-result"

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-captured-key")
    responses.respond(assistant_output("preceding-history"))
    responses.respond(function_call("record", {}, "effect-call"))
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(assistant_output('{"label":"y"}'))
    responses.respond(assistant_output('{"label":"corrected"}'))
    responses.respond(assistant_output("later-original-root"))
    agent = Agent(
        instructions="private-task-instructions",
        tools=[record],
        provider=NativeProvider(base_url=responses.url),
    )
    async with timeout(40), agent.session() as session:
        prior = await session.submit("preceding-private-task")
        assert (await prior.get_result()).final_output == "preceding-history"
        await session.update(
            model="gpt-5.1-codex",
            model_settings={"reasoning_effort": "low", "service_tier": "fast"},
        )
        # Env changes must not replace already accepted model auth.
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-later-key")
        turn = await session.submit(
            "private-task", output_type=PromptedOutput(Report, max_retries=3)
        )
        one, two = await asyncio.gather(turn.get_result(), turn.get_result())
        assert one is two and one is await turn.get_result()
        assert one.final_output == Report(label="corrected")
        assert effect.read_text() == "owned once"
        assert len(responses.requests) == 5
        assert len(one.repair_attempts) == 2
        assert one.usage is not None and one.usage.total_token_usage.total_tokens == 42
        all_turns = [t async for t in session.turns()]
        assert len(all_turns) == 4
        assert tuple(t for t in all_turns if "parent_turn_id" in t) == one.repair_attempts
        # RecordedTurn and SessionItem are mutable detached snapshots, just as
        # for original native history. A caller cannot rewrite later receipts.
        for exposed in all_turns:
            if "parent_turn_id" in exposed:
                exposed.clear()
        fresh_receipts = [t async for t in session.turns() if "parent_turn_id" in t]
        assert tuple(fresh_receipts) == one.repair_attempts
        for t in one.repair_attempts:
            assert t["parent_session_id"] == session.id
            assert t["parent_turn_id"] == turn.turn_id
            assert t["session_id"] != session.id
            assert t["status"] == "completed"
            assert t["usage"] is not None and t["usage"]["total_tokens"] == 14
        items = [i async for i in session.items()]
        history = json.dumps(items)
        assert "preceding-history" in history and "private-result" in history
        assert "corrected" in history and "label" in history
        for request, headers in zip(
            responses.requests[1:], responses.request_headers[1:], strict=True
        ):
            assert request["model"] == "gpt-5.1-codex"
            assert request.get("reasoning", {}).get("effort") == "low"
            assert headers["authorization"] == "Bearer synthetic-captured-key"
        for request in responses.requests[3:]:
            assert function_declarations(request) == []
            for private in (
                "preceding-history",
                "private-task",
                "private-result",
                "effect-call",
            ):
                assert private not in json.dumps(request)
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].turn_id == turn.turn_id and events[-1].status == "completed"
        starts = [e for e in events if isinstance(e, TurnStarted)]
        assert len(starts) == 3
        assert starts[0].turn_id == turn.turn_id
        assert {(e.session_id, e.turn_id) for e in starts[1:]} == {
            (t["session_id"], t["id"]) for t in one.repair_attempts
        }
        assert all(e.parent_submission_id == turn.submission_id for e in starts[1:])
        usages = [e for e in events if isinstance(e, UsageUpdated) and e.parent_submission_id]
        assert usages and {e.turn_id for e in usages} == {t["id"] for t in one.repair_attempts}
        assert (await session.checkpoint_status(kind="idle")).status == "ready"
        assert len((await session.fork()).repair_turns) == 2
        later = await session.submit("later")
        assert (await later.get_result()).final_output == "later-original-root"
        assert await turn.get_result() is one
        assert "corrected" not in json.dumps(responses.requests[-1]["input"])


@pytest.mark.parametrize("iterator", ["items", "turns"])
async def test_history_snapshot_keeps_only_its_originals_and_owned_repairs(responses, iterator):
    responses.respond(assistant_output('{"label":"a"}'))
    responses.respond(assistant_output('{"label":"first-repaired"}'))
    responses.respond(assistant_output('{"label":"b"}'))
    responses.respond(assistant_output('{"label":"second-repaired"}'))
    agent = Agent(
        instructions="synthetic",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 1),
    )
    async with timeout(30), agent.session() as session:
        first = await session.submit("first")
        first_result = await first.get_result()
        assert first_result.final_output.label == "first-repaired"
        first_repairs = {turn["id"] for turn in first_result.repair_attempts}
        assert len(first_repairs) == 1 and first.turn_id not in first_repairs
        records = getattr(session, iterator)
        old = records()
        leading = await anext(old)

        later = await session.submit("second")
        later_result = await later.get_result()
        assert later_result.final_output.label == "second-repaired"
        later_repairs = {turn["id"] for turn in later_result.repair_attempts}
        assert len(later_repairs) == 1 and not later_repairs & (first_repairs | {first.turn_id})
        old_snapshot = [leading] + [record async for record in old]
        fresh_snapshot = [record async for record in records()]
        id_key = "turn_id" if iterator == "items" else "id"
        assert {record[id_key] for record in old_snapshot} == {first.turn_id} | first_repairs
        assert {record[id_key] for record in fresh_snapshot} == (
            {first.turn_id, later.turn_id} | first_repairs | later_repairs
        )
        assert [key for key, _ in groupby(r[id_key] for r in old_snapshot)] == [
            first.turn_id,
            *[r["id"] for r in first_result.repair_attempts],
        ]
        assert [key for key, _ in groupby(r[id_key] for r in fresh_snapshot)] == [
            first.turn_id,
            *[r["id"] for r in first_result.repair_attempts],
            later.turn_id,
            *[r["id"] for r in later_result.repair_attempts],
        ]
        if iterator == "turns":
            for snapshot in (old_snapshot, fresh_snapshot):
                originals = {turn["id"] for turn in snapshot if "parent_turn_id" not in turn}
                for turn in snapshot:
                    if "parent_turn_id" in turn:
                        assert turn["parent_turn_id"] in originals
                        assert turn["parent_session_id"] == session.id
                        assert turn["session_id"] != session.id
        for record in fresh_snapshot:
            record.clear()
        detached = [record async for record in records()]
        assert {record[id_key] for record in detached} == (
            {first.turn_id, later.turn_id} | first_repairs | later_repairs
        )
        assert await first.get_result() is first_result
        assert await later.get_result() is later_result
        assert len(responses.requests) == 4


@pytest.mark.parametrize("finish", ["cancel", "close", "release"])
async def test_original_owner_holds_and_joins_repair_but_waiters_are_passive(responses, finish):
    arrived = asyncio.Event()
    release = asyncio.Event()
    correction = assistant_output('{"label":"corrected"}')

    async def hold(event):
        if event == correction:
            arrived.set()
            await release.wait()

    responses.before_event = hold
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(correction)
    responses.respond(assistant_output("next"))
    agent = Agent(
        instructions="task",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, max_retries=4),
    )
    try:
        context = agent.session()
        async with timeout(32), context as session:
            turn = await session.submit("original")
            waiter = asyncio.create_task(turn.get_result())
            await arrived.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert len(responses.requests) == 2
            with pytest.raises(SessionBusyError):
                await session.submit("must-not-enter", output_type=str)
            with pytest.raises(RuntimeError, match="steering"):
                await turn.steer("must-not-steer-retired-native-root")
            with pytest.raises(CapabilityError, match="repair"):
                await session.suspend()
            assert (await session.checkpoint_status(kind="manual_wait")).status == "not_ready"
            if finish == "release":
                release.set()
                assert (await turn.get_result()).final_output == Report(label="corrected")
            else:
                if finish == "cancel":
                    await turn.cancel()
                else:
                    await context.__aexit__(None, None, None)
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
                release.set()
            if finish != "close":
                following = await session.submit("next", output_type=str)
                assert (await following.get_result()).final_output == "next"
            assert len(responses.requests) == (2 if finish == "close" else 3)
    finally:
        release.set()


@pytest.mark.parametrize("scope", ["root", "all"])
async def test_graceful_stop_during_repair_open_does_not_start_model_work(
    responses, monkeypatch, scope
):
    opened, release = asyncio.Event(), asyncio.Event()
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(assistant_output('{"label":"independent-next"}'))
    agent = Agent(
        instructions="synthetic",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 2),
    )
    async with timeout(30), agent.session() as session:
        original_open = Session._open

        async def hold_open(cls, *args, **kwargs):
            repair = await original_open(*args, **kwargs)
            if kwargs.get("_captured_api_key") is not None:
                opened.set()
                await release.wait()
            return repair

        monkeypatch.setattr(Session, "_open", classmethod(hold_open))
        try:
            turn = await session.submit("invalid original")
            await opened.wait()
            assert len(responses.requests) == 1
            stopping = asyncio.create_task(
                turn.cancel(mode="graceful")
                if scope == "root"
                else session.cancel_all(mode="graceful")
            )
            # Observe the accepted public stop before returning from real
            # native opening; never synthesize a cancellation flag.
            while session._active is not None and not session._active.graceful_requested:
                await asyncio.sleep(0)
            assert session._active is not None and not stopping.done()
            release.set()
            await stopping
            assert len(responses.requests) == 1
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            assert not [t async for t in session.turns() if "parent_turn_id" in t]
            events = [event async for event in turn.events()]
            assert len([event for event in events if isinstance(event, TurnStarted)]) == 1
            assert isinstance(events[-1], TurnFinished) and events[-1].status == "cancelled"
            later = await session.submit("continue original")
            result = await later.get_result()
            assert result.final_output == Report(label="independent-next")
            assert not result.repair_attempts and len(responses.requests) == 2
        finally:
            release.set()


@pytest.mark.parametrize("scope", ["root", "all"])
@pytest.mark.parametrize("correction_valid", [False, True], ids=["needs-retry", "naturally-done"])
async def test_graceful_repair_stops_at_next_attempt_not_at_natural_completion(
    responses, scope, correction_valid
):
    entered = asyncio.Event()
    release = asyncio.Event()
    held = assistant_output('{"label":"corrected"}' if correction_valid else '{"label":"y"}')

    async def hold(event):
        if event == held:
            entered.set()
            await release.wait()

    responses.before_event = hold
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(held)
    # If a second repair leaks through cancellation, it would consume the
    # next task's answer. Counts and repeated public outcomes must catch that.
    responses.respond(assistant_output('{"label":"independent-next"}'))
    agent = Agent(
        instructions="synthetic",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 3),
    )
    async with timeout(30), agent.session() as session:
        try:
            turn = await session.submit("original")
            await entered.wait()
            assert len(responses.requests) == 2
            cancelling = asyncio.create_task(
                turn.cancel(mode="graceful")
                if scope == "root"
                else session.cancel_all(mode="graceful")
            )
            # Observe, never set, the actual graceful owner marker to order
            # the real public cancel request before the model's held reply.
            while session._active is not None and not session._active.graceful_requested:
                await asyncio.sleep(0)
            assert session._active is not None and not cancelling.done()
            release.set()
            await cancelling
            assert len(responses.requests) == 2
            for _ in range(2):
                if correction_valid:
                    result = await turn.get_result()
                    assert result.final_output == Report(label="corrected")
                    assert result.usage is not None
                    assert result.usage.total_token_usage.total_tokens == 14
                    assert len(result.repair_attempts) == 1
                else:
                    with pytest.raises(TurnStoppedError):
                        await turn.get_result()
            recorded = [row async for row in session.turns()]
            repairs = [row for row in recorded if "parent_turn_id" in row]
            assert len(repairs) == 1
            assert repairs[0]["parent_turn_id"] == turn.turn_id
            assert repairs[0]["session_id"] != session.id and repairs[0]["id"] != turn.turn_id
            assert repairs[0]["status"] == "completed"
            assert repairs[0]["usage"] is not None
            assert repairs[0]["usage"]["total_tokens"] == 14
            events = [event async for event in turn.events()]
            assert isinstance(events[-1], TurnFinished) and events[-1].turn_id == turn.turn_id
            assert events[-1].status == ("completed" if correction_valid else "cancelled")
            following = await session.submit("next-independent")
            done = await following.get_result()
            assert done.final_output == Report(label="independent-next")
            assert done.turn.turn_id != turn.turn_id and not done.repair_attempts
            assert await following.get_result() is done
            if correction_valid:
                assert await turn.get_result() is result
            else:
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            assert len(responses.requests) == 3
        finally:
            release.set()


@pytest.mark.parametrize("location", ["host", "environment"])
async def test_repair_rejects_actual_unregistered_capabilities_without_original_effect(
    responses, tmp_path, monkeypatch, location
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_executor_tools import declaration
    from tests.test_native_mcp_stdio import records, stdio
    from tests.test_native_mcp_tools import declarations, mcp_call

    mcp_log = tmp_path / "mcp-records"
    shell_effect = tmp_path / "shell-effect"
    python_effect = tmp_path / "python-effect"

    @function_tool
    def record() -> str:
        python_effect.write_text("forbidden")
        return "forbidden"

    responses.respond(mcp_call("search", "mcp__local", "actual-original-mcp"))
    responses.respond(assistant_output('{"label":"x"}'))
    child = function_call("spawn_agent", {"message": "forbidden"}, "child-forbidden")
    child["item"]["namespace"] = "collaboration"
    responses.respond(
        function_call("record", {}, "python-forbidden"),
        mcp_call("search", "mcp__local", "mcp-forbidden"),
        function_call("exec_command", {"cmd": "touch " + shlex.quote(str(shell_effect))}, "shell"),
        child,
    )
    responses.respond(assistant_output('{"label":"recovered"}'))
    async with timeout(45), AsyncExitStack() as stack:
        environment = None
        if location == "environment":
            executor = await stack.enter_async_context(ExecutorPeer(tmp_path / "remote").serve())
            trust(executor, tmp_path, monkeypatch)
            mcp_log = executor.root / "mcp-records"
            shell_effect = executor.root / "shell-effect"
            environment = declaration(executor)
        # Put the attempted shell operation on the actual attached executor's
        # filesystem, and prove the original session really used its stdio MCP.
        responses.replies[2][2] = function_call(
            "exec_command", {"cmd": "touch " + shlex.quote(str(shell_effect))}, "shell"
        )
        agent = Agent(
            instructions="private-original-mcp-task",
            tools=[record, stdio(mcp_log, location=location)],
            provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
            output_type=PromptedOutput(Report, max_retries=3),
        )
        session = await stack.enter_async_context(agent.session(environment=environment))
        turn = await session.submit("use local search")
        try:
            result = await turn.get_result()
        except NativeError:
            # The native primitive may reject unregistered calls as a native
            # failure. That error must never trigger another correction attempt.
            assert len(responses.requests) == 3
        else:
            assert result.final_output == Report(label="recovered")
            assert len(result.repair_attempts) == 1
            assert len(responses.requests) == 4
        calls = [
            row["message"]["params"]
            for row in records(mcp_log)
            if row.get("message", {}).get("method") == "tools/call"
        ]
        assert len(calls) == 1 and calls[0]["name"] == "search"
        assert ("mcp__local", "search") in declarations(responses.requests[0])
        assert all(function_declarations(request) == [] for request in responses.requests[2:])
        assert not python_effect.exists()
        assert not shell_effect.exists()
        assert [child async for child in session.subagents()] == []


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("finish", ["cancel", "complete"])
async def test_native_children_survive_owned_root_repair_and_cannot_supply_its_tools(
    tmp_path, monkeypatch, finish
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration
    from tests.test_native_mcp_tools import declarations

    repair_entered = asyncio.Event()
    repair_release = asyncio.Event()
    result = assistant_output('{"label":"corrected"}')
    async with (
        timeout(45),
        child_peer(("research", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original_response = peer.response
        original_hold = peer.hold
        repair_requests = []

        def reply():
            body = peer.requests[-1]
            meta = metadata(body, peer.request_headers[-1])
            if meta["agent_name"] == "/root" and (
                "collaboration",
                "spawn_agent",
            ) not in declarations(body):
                repair_requests.append(body)
                assert function_declarations(body) == []
                return [result]
            events = original_response()
            for event in events:
                item = event.get("item", {})
                if item.get("type") == "message" and meta["agent_name"] == "/root":
                    item["content"] = [{"type": "output_text", "text": '{"label":"x"}'}]
            return events

        async def hold(event):
            if event == result:
                repair_entered.set()
                await repair_release.wait()
            else:
                await original_hold(event)

        monkeypatch.setattr(peer, "response", reply)
        peer.before_event = hold
        try:
            async with agent_for(peer, executor).session(environment=declaration(executor)) as s:
                turn = await s.submit("Start both children", output_type=PromptedOutput(Report, 2))
                await asyncio.gather(
                    repair_entered.wait(), *(event.wait() for event in peer.entered.values())
                )
                child_turns = await handles(s, 2)
                pending = [asyncio.create_task(child.get_result()) for child in child_turns]
                await asyncio.sleep(0)
                assert all(not wait.done() for wait in pending)
                if finish == "cancel":
                    await turn.cancel()
                    with pytest.raises(TurnStoppedError):
                        await turn.get_result()
                else:
                    repair_release.set()
                    done = await turn.get_result()
                    assert done.final_output.label == "corrected"
                    assert len(done.repair_attempts) == 1
                assert len(repair_requests) == 1
                assert all(not wait.done() for wait in pending)
                for release in peer.release.values():
                    release.set()
                done_children = await asyncio.gather(*pending)
                assert {done.final_output for done in done_children} == {
                    "alpha complete",
                    "beta complete",
                }
                assert (executor.root / "alpha.txt").read_text() == "alpha"
                assert (executor.root / "beta.txt").read_text() == "beta"
        finally:
            repair_release.set()


async def test_original_native_policy_stop_and_callback_error_are_never_output_retried(
    responses, tmp_path
):
    from agents import StopTurn
    from tests.test_native_mcp_stdio import records, stdio
    from tests.test_native_mcp_tools import mcp_call

    log = tmp_path / "stopped-mcp"

    @function_tool
    def failing() -> str:
        raise RuntimeError("private-failure")

    responses.respond(mcp_call("search", "mcp__local", "stopped"))
    responses.respond(function_call("failing", {}, "failing"))
    agent = Agent(
        instructions="use the selected tool",
        tools=[
            stdio(log, before=lambda ctx, call: StopTurn("stopped")),
            failing,
        ],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 3),
    )
    async with timeout(25), agent.session() as session:
        stopped_turn = await session.submit("first")
        with pytest.raises(TurnStoppedError):
            await stopped_turn.get_result()
        failed_turn = await session.submit("second")
        with pytest.raises(RuntimeError, match="private-failure"):
            await failed_turn.get_result()
        assert len(responses.requests) == 2
        assert not [
            row for row in records(log) if row.get("message", {}).get("method") == "tools/call"
        ]
        assert not [t async for t in session.turns() if "parent_turn_id" in t]


async def test_zero_or_valid_output_can_restore_but_local_retry_admission_is_rejected(
    responses, tmp_path
):
    from agents import LocalStorage

    agent = Agent(
        instructions="answer",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 4),
    )
    responses.respond(assistant_output('{"label":"already valid"}'))
    async with timeout(24):
        async with agent.session() as session:
            support = session.capabilities.get("prompted_output")
            assert support.status == "supported"
            assert "tool-free" in support.reason
            session.capabilities.require("prompted_output")
            assert not responses.requests
            done = await (await session.submit("valid")).get_result()
            assert done.final_output == Report(label="already valid") and not done.repair_attempts
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert restored.restored_turn is None
            assert len(responses.requests) == 1
        async with agent.session(storage=LocalStorage(tmp_path / "profile")) as local:
            support = local.capabilities.get("prompted_output")
            assert support.status == "limited"
            assert "max_retries=0" in support.reason
            with pytest.raises(CapabilityError):
                local.capabilities.require("prompted_output")
            local.capabilities.require("prompted_output", allow_limited=True)
            assert len(responses.requests) == 1
            with pytest.raises(CapabilityError, match="LocalStorage"):
                await local.submit("must not execute an unrestorable task")
            assert len(responses.requests) == 1
            responses.respond(assistant_output('{"label":"explicit zero"}'))
            zero = await local.submit("zero", output_type=PromptedOutput(Report))
            assert (await zero.get_result()).final_output.label == "explicit zero"


async def test_manual_before_repair_restores_only_matching_budget_and_fresh_request(responses):
    from agents import FunctionTool
    from tests.test_native_manual_tools import LookupArgs, next_request

    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(
        instructions="Read",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 2),
    )
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    async with timeout(36):
        async with agent.session() as session:
            original = await session.submit("read one record")
            stale = await next_request(session.interactions(), lookup)
            state = await session.suspend()
        with pytest.raises(ValueError, match="output"):
            async with agent.with_output_type(PromptedOutput(Report, 1)).session(state=state):
                pytest.fail("restored with a different retry budget")
        responses.respond(assistant_output('{"label":"x"}'))
        responses.respond(assistant_output('{"label":"fixed"}'))
        async with agent.session(state=state) as restored:
            turn = restored.restored_turn
            assert turn is not None and turn.turn_id == original.turn_id
            fresh = await next_request(restored.interactions(), lookup)
            with pytest.raises((RuntimeError, ValueError)):
                await stale.respond("stale")
            await fresh.respond("from-current-owner")
            completed = await turn.get_result()
            assert completed.final_output.label == "fixed"
            assert len(completed.repair_attempts) == 1
            assert completed.repair_attempts[0]["parent_turn_id"] == original.turn_id
            assert len(responses.requests) == 3
            assert "from-current-owner" in json.dumps(responses.requests[1])
            assert "from-current-owner" not in json.dumps(responses.requests[2])


async def test_broken_separate_repair_cannot_disappear_from_portable_history(
    responses, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()
    correction = assistant_output('{"label":"corrected"}')
    repairs = []

    async def hold(event):
        if event == correction:
            entered.set()
            await release.wait()

    responses.before_event = hold
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(correction)
    agent = Agent(
        instructions="synthetic",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 1),
    )
    async with timeout(30), agent.session() as session:
        real_open = Session._open.__func__

        async def observe_open(cls, declaration, *args, **kwargs):
            opened = await real_open(cls, declaration, *args, **kwargs)
            if kwargs.get("_captured_api_key") is not None:
                repairs.append(opened)
            return opened

        monkeypatch.setattr(Session, "_open", classmethod(observe_open))
        try:
            turn = await session.submit("original")
            await entered.wait()
            assert len(responses.requests) == 2 and len(repairs) == 1
            # Stop only the real, separate repair event reader after native
            # admission and the real provider request, not the parent reader.
            repairs[0]._reader.cancel()
            await asyncio.gather(repairs[0]._reader, return_exceptions=True)
            release.set()
            with pytest.raises((NativeError, TurnStoppedError)):
                await turn.get_result()
            assert not session._broken
            assert not [row async for row in session.turns() if "parent_turn_id" in row]
            with pytest.raises(CapabilityError, match="repair"):
                await session.save_state()
            with pytest.raises(CapabilityError, match="repair"):
                await session.fork()
        finally:
            release.set()


async def test_failed_repair_reader_before_admission_keeps_parent_portable(responses, monkeypatch):
    repairs = []
    responses.respond(assistant_output('{"label":"x"}'))
    agent = Agent(
        instructions="synthetic",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 1),
    )
    async with timeout(30), agent.session() as session:
        real_open = Session._open.__func__

        async def observe_open(cls, declaration, *args, **kwargs):
            opened = await real_open(cls, declaration, *args, **kwargs)
            if kwargs.get("_captured_api_key") is not None:
                repairs.append(opened)
                opened._reader.cancel()
                await asyncio.gather(opened._reader, return_exceptions=True)
            return opened

        monkeypatch.setattr(Session, "_open", classmethod(observe_open))
        turn = await session.submit("invalid original")
        with pytest.raises(RuntimeError, match="unconfirmed native operation"):
            await turn.get_result()
        assert len(repairs) == 1 and len(responses.requests) == 1
        assert not session._broken
        assert not [row async for row in session.turns() if "parent_turn_id" in row]
        saved = await session.save_state()
        forked = await session.fork()
        assert saved.source_session_id == session.id
        assert forked.source_session_id != session.id
        responses.respond(assistant_output('{"label":"independent-next"}'))
        next_turn = await session.submit("valid original")
        result = await next_turn.get_result()
        assert result.final_output == Report(label="independent-next")
        assert not result.repair_attempts and len(responses.requests) == 2
        assert (await session.save_state()).source_session_id == session.id

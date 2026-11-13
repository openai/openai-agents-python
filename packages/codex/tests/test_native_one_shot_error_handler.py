"""The native one-shot runner can settle a supported failure with typed output."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailResult,
    NativeError,
    NativeProvider,
    OutputGuardrail,
    OutputValidationError,
    PromptedOutput,
    RecoverRun,
    RunBudget,
    RunFailure,
    Session,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent_observation import provider


class Record(BaseModel):
    count: int


@pytest.mark.parametrize("persist", [True, False])
async def test_error_handler_returns_validated_output_and_controls_replay(responses, persist):
    responses.respond(assistant_output("bad output"))
    responses.respond(assistant_output('{"count":2}'))
    native = provider(responses)
    seen: list[RunFailure] = []

    def recover(failure: RunFailure) -> RecoverRun[dict[str, int]]:
        seen.append(failure)
        return RecoverRun({"count": 1}, include_in_history=persist)

    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    async with timeout(20):
        try:
            result = await agent.run(
                "first",
                error_handler=recover,
                return_state=True,
                return_history=True,
                return_items=True,
            )
            assert result.final_output == Record(count=1)
            assert result.state is not None and result.history is not None
            assert len(seen) == 1 and seen[0].kind == "invalid_output"
            assert isinstance(seen[0].error, OutputValidationError)
            assert seen[0].details.input == "first"
            assert len(seen[0].details.raw_responses) == 1
            assert any(
                item.get("role") == "user" and "first" in str(item.get("content"))
                for item in seen[0].details.history
            )
            assert (await result.turn.get_result()).final_output == Record(count=1)
            assert result.items is not None
            synthesized = [
                item.item
                for item in result.items
                if item.item["type"] == "message"
                and item.item["content"][0]["text"] == '{"count": 1}'
            ]
            assert len(synthesized) == int(persist)
            if synthesized:
                assert synthesized[0]["id"] in {item.get("id") for item in result.history}
            assert (
                any(
                    part.get("text") == '{"count": 1}'
                    for item in result.history
                    if item.get("role") == "assistant"
                    for part in item.get("content", [])
                )
                is persist
            )
            next_result = await agent.run("second", state=result.state)
            assert next_result.final_output == Record(count=2)
            assert (
                any(
                    part.get("text") == '{"count": 1}'
                    for item in responses.requests[1]["input"]
                    if item.get("role") == "assistant"
                    for part in item.get("content", [])
                )
                is persist
            )
        finally:
            await native.aclose()


async def test_request_limit_fallback_does_not_retry_or_repeat_effects(responses):
    calls: list[str] = []

    @function_tool
    def record() -> str:
        calls.append("record")
        return "done"

    responses.respond(function_call("record", {}, "first"))
    native = provider(responses)
    seen: list[str] = []

    async def recover(failure: RunFailure) -> RecoverRun[str]:
        seen.append(failure.kind)
        assert isinstance(failure.error, NativeError)
        assert failure.error.code == "run_budget_exceeded"
        assert failure.details.items
        return RecoverRun("Budget reached", include_in_history=False)

    agent = Agent(instructions="Record and answer.", provider=native, tools=[record])
    async with timeout(20):
        try:
            result = await agent.run(
                "begin", budget=RunBudget(max_requests=1), error_handler=recover
            )
            assert result.final_output == "Budget reached"
            assert seen == ["max_requests"] and calls == ["record"]
            assert len(responses.requests) == 1
        finally:
            await native.aclose()


async def test_refusal_fallback_and_unhandled_failure_keep_their_own_causes():
    from tests.test_native_response_facts import fact_peer

    async with timeout(20), fact_peer() as peer:
        peer.replies.extend([(True, False), (True, False)])
        native = NativeProvider(base_url=peer.url, api_key="synthetic-facts-key")
        agent = Agent(instructions="Answer.", model="gpt-5.2", provider=native)
        seen: list[RunFailure] = []

        def decline(failure: RunFailure) -> None:
            seen.append(failure)

        try:
            recovered = await agent.run("answer", error_handler=lambda _: RecoverRun("fallback"))
            assert recovered.final_output == "fallback"
            with pytest.raises(NativeError) as caught:
                await agent.run("answer again", error_handler=decline)
            assert len(seen) == 1 and seen[0].kind == "model_refusal"
            assert caught.value is seen[0].error
        finally:
            await native.aclose()


@pytest.mark.parametrize("tripwire", [False, True])
async def test_fallback_respects_existing_output_guardrails(responses, tripwire):
    from agents import GuardrailTripwire

    responses.respond(assistant_output("bad output"))
    native = provider(responses)
    seen: list[RunFailure] = []
    guarded = []

    def recover(failure: RunFailure) -> RecoverRun[Record]:
        seen.append(failure)
        return RecoverRun(Record(count=3))

    async def guard(context, actual_agent):
        guarded.append((context, actual_agent))
        assert context.final_output == Record(count=3)
        assert context.model == "gpt-5.5"
        return GuardrailResult(tripwire)

    agent = Agent(
        name="recovery owner",
        model="gpt-5.5",
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
        output_guardrails=[OutputGuardrail("final", guard)],
    )
    try:
        async with timeout(20), agent.session() as session:
            turn = await session.submit("first")
            if tripwire:
                with pytest.raises(GuardrailTripwire) as caught:
                    await session._one_shot_result(turn, recover, True, "first")
                assert caught.value.record.name == "final"
                assert all(
                    part.get("text") != '{"count": 3}'
                    for item in (await session.history_snapshot()).items
                    if item.get("role") == "assistant"
                    for part in item.get("content", [])
                )
            else:
                result = await session._one_shot_result(turn, recover, True, "first")
                assert result.final_output == Record(count=3)
                assert [record.name for record in result.guardrail_results] == ["final"]
                assert any(
                    item.get("role") == "assistant"
                    and any(part.get("text") == '{"count": 3}' for part in item.get("content", []))
                    for item in (await session.history_snapshot()).items
                )
            assert len(seen) == len(guarded) == 1
            context, actual_agent = guarded[0]
            assert actual_agent is agent and context.session_id == session.id
            assert context.turn_id == turn.turn_id
            assert len(responses.requests) == 1
    finally:
        await native.aclose()


async def test_invalid_fallback_does_not_return_success(responses):
    responses.respond(assistant_output("bad output"))
    native = provider(responses)
    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    try:
        async with timeout(20):
            with pytest.raises(OutputValidationError) as caught:
                await agent.run("first", error_handler=lambda _: RecoverRun({"other": 3}))
            assert caught.value.raw_output == ""
            assert len(responses.requests) == 1
    finally:
        await native.aclose()


@pytest.mark.parametrize("cancel_source", ["caller", "policy"])
async def test_error_recovery_cancellation_keeps_canonical_history(
    responses, monkeypatch, cancel_source
):
    responses.respond(assistant_output("bad output"))
    native = provider(responses)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    histories = []
    calls: list[RunFailure] = []
    original = Session._recover_one_shot

    async def observe_recovery(session, *args):
        before = await session.history_snapshot()
        try:
            return await original(session, *args)
        finally:
            after = await session.history_snapshot()
            histories.append((before, after))

    monkeypatch.setattr(Session, "_recover_one_shot", observe_recovery)

    async def recover(failure: RunFailure) -> RecoverRun[dict[str, int]]:
        calls.append(failure)
        entered.set()
        try:
            await release.wait()
            if cancel_source == "policy":
                raise asyncio.CancelledError("Policy cancelled recovery")
            return RecoverRun({"count": 73019})
        finally:
            finished.set()

    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    run = asyncio.create_task(agent.run("first", error_handler=recover))
    try:
        async with timeout(20):
            await entered.wait()
            if cancel_source == "caller":
                run.cancel()
            else:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await run
            assert finished.is_set() and run.cancelled()
            assert len(calls) == 1 and calls[0].kind == "invalid_output"
            assert len(histories) == 1
            before, after = histories[0]
            assert after.revision == before.revision
            assert after.items == before.items
            assert "73019" not in repr(after.items)
            assert len(responses.requests) == 1
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await native.aclose()


@pytest.mark.parametrize("persist", [True, False])
@pytest.mark.parametrize("change", ["root", "queued", "history", "profile"])
async def test_error_recovery_rejects_an_advanced_session(responses, persist, change):
    responses.respond(assistant_output("bad output"))
    if change in {"root", "queued"}:
        responses.respond(assistant_output('{"count":2}'))
    if change == "queued":
        responses.respond(assistant_output('{"count":3}'))
    native = provider(responses)
    entered = asyncio.Event()
    changed = asyncio.Event()
    polling = asyncio.Event()
    release_poll = asyncio.Event()
    captured = {}

    async def gate(event):
        item = event.get("item", {})
        if change == "queued" and item.get("content") == [
            {"type": "output_text", "text": '{"count":2}'}
        ]:
            polling.set()
            await release_poll.wait()

    responses.before_event = gate

    class ExposingAgent(Agent):
        @asynccontextmanager
        async def session(self, **options):
            async with super().session(**options) as session:
                captured["session"] = session
                try:
                    yield session
                finally:
                    captured["final_history"] = await session.history_snapshot()

    async def other_caller():
        await entered.wait()
        session = captured["session"]
        try:
            if change in {"root", "queued"}:
                other = await session.submit("intervening")
                if change == "queued":
                    await polling.wait()
                    queued = await session.submit("queued", if_busy="queue")
                    assert queued.turn_id is None
                    release_poll.set()
                await other.get_result()
                if change == "queued":
                    await queued.get_result()
            elif change == "history":
                before = await session.history_snapshot()
                await session.transact_history(
                    expected_revision=before.revision,
                    keep=before.total,
                    items=[{"type": "review_marker", "marker": "other caller"}],
                )
            else:
                await session.update(instructions="New native configuration.")
            captured["changed_history"] = await session.history_snapshot()
        finally:
            changed.set()

    async def recover(failure):
        entered.set()
        await changed.wait()
        await sibling
        return RecoverRun({"count": 73019}, include_in_history=persist)

    sibling = asyncio.create_task(other_caller())
    agent = ExposingAgent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    try:
        async with timeout(20):
            with pytest.raises(RuntimeError, match="Session.*changed"):
                await agent.run("first", error_handler=recover)
            await sibling
        before = captured["changed_history"]
        after = captured["final_history"]
        assert after.revision == before.revision
        assert after.items == before.items
        assert "73019" not in repr(after.items)
        assert len(responses.requests) == {"root": 2, "queued": 3}.get(change, 1)
    finally:
        release_poll.set()
        if not sibling.done():
            sibling.cancel()
        await asyncio.gather(sibling, return_exceptions=True)
        await native.aclose()

"""Prompted repair must retain the original completion policy and native owner."""

import asyncio
import json

import pytest

from agents import (
    Agent,
    ContinueTurn,
    DecisionResponse,
    OutputValidationError,
    PromptedOutput,
    StopTurn,
    TurnStoppedError,
)
from agents.observation import TurnFinished
from agents.usage import TokenUsage
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_prompted_repair import Report


class Decisions:
    def __init__(self):
        self.requests = []

    async def get_decision(self, request):
        self.requests.append(request)
        return DecisionResponse(
            value={"response": "corrected" not in request.input},
            usage=TokenUsage(7, 0, 0, 3, 0, 10),
            request_id=f"completion-policy-{len(self.requests)}",
            model="completion-policy-model",
        )


@pytest.mark.parametrize("action", ["accept", "continue", "stop", "error"])
async def test_repaired_candidate_uses_original_policy_owner_and_decision_provider(
    responses, action
):
    selected, unused = Decisions(), Decisions()
    context, calls = object(), []
    failure = LookupError("Application rejected correction")

    async def complete(request):
        # A previous candidate's helper must stay expired in the new callback.
        if calls:
            before = len(selected.requests)
            with pytest.raises(RuntimeError, match="exact live callback"):
                await calls[-1].decide("Late check", input="must not dispatch")
            assert len(selected.requests) == before
        calls.append(request)
        assert request.context is context
        accepted = await request.decide("Approve?", input=request.text or "")
        if accepted or action == "accept":
            return None
        if action == "continue":
            return ContinueTurn("Return an approved label using the original task and tools.")
        if action == "stop":
            return StopTurn("Rejected correction")
        raise failure

    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(assistant_output('{"label":"corrected"}'))
    if action == "continue":
        responses.respond(assistant_output('{"label":"approved"}'))
    agent = Agent(
        instructions="Return a report.",
        model="gpt-5.5",
        provider=provider(responses),
        decisions=unused,
        before_complete=complete,
    )
    async with timeout(20), agent.session(context=context, decisions=selected) as session:
        # Per-turn output selection must retain the original completion gate.
        turn = await session.submit("Report.", output_type=PromptedOutput(Report, 1))
        if action in ("accept", "continue"):
            result = await turn.get_result()
            assert result.turn is turn
            assert result.final_output == Report(
                label="approved" if action == "continue" else "corrected"
            )
            assert len(result.repair_attempts) == 1
            assert result.repair_attempts[0]["usage"]["total_tokens"] == 14
            assert result.thread_usage.total_token_usage.total_tokens == (
                58 if action == "continue" else 34
            )
        else:
            with pytest.raises(TurnStoppedError if action == "stop" else LookupError) as caught:
                await turn.get_result()
            if action == "error":
                assert caught.value is failure
        events = [event async for event in turn.events()]
        finished = [
            event
            for event in events
            if isinstance(event, TurnFinished) and event.session_id == session.id
        ]
        assert len(finished) == 1
        assert finished[0].status == (
            "cancelled" if action == "stop" else "failed" if action == "error" else "completed"
        )
        records = [record async for record in session.turns()]
        original = next(record for record in records if record["id"] == turn.turn_id)
        assert original["usage"]["total_tokens"] == (58 if action == "continue" else 34)
        assert [call.text for call in calls] == [
            '{"label":"x"}',
            '{"label":"corrected"}',
            *(['{"label":"approved"}'] if action == "continue" else []),
        ]
        assert {call.session_id for call in calls} == {session.id}
        assert {call.turn_id for call in calls} == {turn.turn_id}
        assert {call.model for call in calls} == {"gpt-5.5"}
        assert [call.continuation_count for call in calls] == (
            [0, 0, 1] if action == "continue" else [0, 0]
        )
    assert not unused.requests
    assert [request.input for request in selected.requests] == [call.text for call in calls]
    assert len(responses.requests) == (3 if action == "continue" else 2)
    if action == "continue":
        assert "Return an approved label" in json.dumps(responses.requests[2]["input"])
        assert "Correct the supplied JSON" not in json.dumps(responses.requests[2])


@pytest.mark.parametrize("continue_after_repair", [False, True])
async def test_repair_limit_is_shared_across_original_completion_proposals(
    responses, continue_after_repair
):
    calls = []

    def complete(request):
        calls.append(request.text)
        if request.text == '{"label":"corrected"}':
            return ContinueTurn("Try the original task again.")

    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(
        assistant_output('{"label":"corrected"}' if continue_after_repair else '{"label":"y"}')
    )
    if continue_after_repair:
        responses.respond(assistant_output('{"label":"z"}'))
    agent = Agent(
        instructions="Return a report.",
        provider=provider(responses),
        before_complete=complete,
        output_type=PromptedOutput(Report, 1),
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Report.")
        with pytest.raises(OutputValidationError):
            await turn.get_result()
        events = [event async for event in turn.events()]
        assert (
            len(
                [
                    event
                    for event in events
                    if isinstance(event, TurnFinished) and event.session_id == session.id
                ]
            )
            == 1
        )
    assert len(calls) == len(responses.requests) == (3 if continue_after_repair else 2)


@pytest.mark.parametrize("boundary", ["correction", "repaired_callback"])
@pytest.mark.parametrize("operation", ["cancel", "close"])
async def test_force_stop_joins_repair_and_original_callback_cleanup(
    responses, boundary, operation
):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    correction = assistant_output('{"label":"corrected"}')

    async def hold(event):
        if boundary == "correction" and event == correction:
            entered.set()
            await release.wait()

    async def complete(request):
        calls.append(request.text)
        if boundary == "repaired_callback" and request.text == '{"label":"corrected"}':
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                return ContinueTurn("Late feedback must not dispatch.")

    responses.before_event = hold
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(correction)
    agent = Agent(
        instructions="Return a report.",
        provider=provider(responses),
        before_complete=complete,
        output_type=PromptedOutput(Report, 1),
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Report.")
        await entered.wait()
        result = asyncio.create_task(turn.get_result())
        stopping = asyncio.create_task(turn.cancel() if operation == "cancel" else session.aclose())
        try:
            if boundary == "repaired_callback":
                await cleaning.wait()
                assert not stopping.done() and not result.done()
            else:
                # A received stop must join the correction request before returning.
                await stopping
                assert session._repair_session is None
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await result
        events = [event async for event in turn.events()]
        assert (
            len(
                [
                    event
                    for event in events
                    if isinstance(event, TurnFinished) and event.session_id == session.id
                ]
            )
            == 1
        )
        assert session._repair_session is None
    assert len(responses.requests) == 2


@pytest.mark.parametrize("action", ["accept", "continue", "invalid"])
@pytest.mark.parametrize("boundary", ["correction", "repaired_callback"])
async def test_graceful_stop_keeps_accepted_value_but_prevents_new_work(
    responses, action, boundary
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    corrected = '{"label":"y"}' if action == "invalid" else '{"label":"corrected"}'

    correction = assistant_output(corrected)

    async def hold(event):
        if boundary == "correction" and event == correction:
            entered.set()
            await release.wait()

    async def complete(request):
        calls.append(request.text)
        if request.text == corrected:
            if boundary == "repaired_callback":
                entered.set()
                await release.wait()
            if action == "continue":
                return ContinueTurn("Do not start another sample after the stop.")

    responses.before_event = hold
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(correction)
    agent = Agent(
        instructions="Return a report.",
        provider=provider(responses),
        before_complete=complete,
        output_type=PromptedOutput(Report, 2),
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Report.")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel(mode="graceful"))
        try:
            while not session._active.graceful_requested:
                await asyncio.sleep(0)
            assert not stopping.done()
        finally:
            release.set()
        await stopping
        if action == "accept":
            assert (await turn.get_result()).final_output == Report(label="corrected")
        else:
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
    assert calls == ['{"label":"x"}', corrected]
    assert len(responses.requests) == 2


async def test_accepted_repaired_none_is_not_confused_with_missing_validation(responses):
    calls = []
    responses.respond(assistant_output("not JSON"))
    responses.respond(assistant_output("null"))
    agent = Agent(
        instructions="Return an integer or null.",
        provider=provider(responses),
        before_complete=lambda request: calls.append(request.text),
        output_type=PromptedOutput(int | None, 1),
    )
    async with timeout(20):
        result = await agent.run("Answer.")
    assert result.final_output is None
    assert calls == ["not JSON", "null"]
    assert len(result.repair_attempts) == 1
    assert len(responses.requests) == 2


async def test_graceful_stop_during_correction_open_closes_without_admission(
    responses, monkeypatch
):
    from agents import Session

    entered, release = asyncio.Event(), asyncio.Event()
    repairs, calls = [], []
    original = Session._open.__func__

    async def opening(cls, declaration, **options):
        repair = await original(cls, declaration, **options)
        if declaration.before_complete is None:
            repairs.append(repair)
            entered.set()
            await release.wait()
        return repair

    responses.respond(assistant_output('{"label":"x"}'))
    agent = Agent(
        instructions="Return a report.",
        provider=provider(responses),
        output_type=PromptedOutput(Report, 1),
        before_complete=lambda request: calls.append(request.text),
    )
    monkeypatch.setattr(Session, "_open", classmethod(opening))
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Report.")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel(mode="graceful"))
        try:
            while not session._active.graceful_requested:
                await asyncio.sleep(0)
            assert not stopping.done()
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert len(repairs) == 1 and repairs[0]._closed
        assert session._repair_session is None
        assert not session._repair_turns
    assert calls == ['{"label":"x"}']
    assert len(responses.requests) == 1


async def test_cancel_repaired_decide_joins_cleanup_and_keeps_late_usage_on_original(responses):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    completed_callbacks = []

    class SlowDecisions:
        async def get_decision(self, request):
            requests.append(request.input)
            if "corrected" in request.input:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleaning.set()
                    await release.wait()
            return DecisionResponse(value={"response": True}, usage=TokenUsage(7, 0, 0, 3, 0, 10))

    async def complete(request):
        if request.text != '{"label":"successor"}':
            assert await request.decide("Approve?", input=request.text or "")
            completed_callbacks.append(request.text)

    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(assistant_output('{"label":"corrected"}'))
    responses.respond(assistant_output('{"label":"successor"}'))
    agent = Agent(
        instructions="Return a report.",
        provider=provider(responses),
        output_type=PromptedOutput(Report, 1),
        decisions=SlowDecisions(),
        before_complete=complete,
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Report.")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not stopping.done()
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        root_usage = [record async for record in session.turns()][0]["usage"]["total_tokens"]
        assert root_usage == 34
        assert len(session._repair_turns) == 1
        assert session._repair_turns[0]["usage"]["total_tokens"] == 14
        assert session._repair_session is None
        successor = await (await session.submit("Next.")).get_result()
        assert successor.final_output == Report(label="successor")
        assert successor.thread_usage.total_token_usage.total_tokens == 48
        records = [record async for record in session.turns()]
        roots = [record for record in records if "parent_turn_id" not in record]
        assert [record["usage"]["total_tokens"] for record in roots] == [34, 14]
        repairs = [record for record in records if record.get("parent_turn_id") == turn.turn_id]
        assert [record["usage"]["total_tokens"] for record in repairs] == [14]
    assert completed_callbacks == ['{"label":"x"}']
    assert requests == ['{"label":"x"}', '{"label":"corrected"}']
    assert len(responses.requests) == 3

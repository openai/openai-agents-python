"""Logical acceptance observations carry identity without result content."""

import asyncio
from dataclasses import asdict

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    ContinueTurn,
    OutputValidationError,
    PromptedOutput,
    StopTurn,
    TurnStoppedError,
)
from agents.observation import OutputAccepted, TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider


class Count(BaseModel):
    count: int


async def test_event_only_consumption_and_multiple_typed_waiters(responses):
    responses.respond(assistant_output('{"count":2}'))
    agent = Agent(
        instructions="Count.", model="gpt-5.5", provider=provider(responses), output_type=Count
    )
    async with timeout(20), agent.session(include_output_accepted=True) as session:
        turn = await session.submit("go")
        events = [event async for event in turn.events()]
        accepted = [event for event in events if isinstance(event, OutputAccepted)]
        assert len(accepted) == 1
        assert asdict(accepted[0]) == {
            "type": "output_accepted",
            "session_id": session.id,
            "submission_id": turn.submission_id,
            "turn_id": turn.turn_id,
            "sequence": accepted[0].sequence,
            "parent_submission_id": None,
        }
        assert (
            next(event.sequence for event in events if isinstance(event, TurnFinished))
            < accepted[0].sequence
        )
        first, second = await asyncio.gather(turn.get_result(), turn.get_result())
        assert first is second
        assert first.final_output == Count(count=2)
        assert [
            event async for event in turn.events() if isinstance(event, OutputAccepted)
        ] == accepted


@pytest.mark.parametrize("mode", ["continue", "repair"])
async def test_only_final_logical_result_is_accepted(responses, mode):
    responses.respond(assistant_output("invalid" if mode == "repair" else "private draft"))
    responses.respond(assistant_output('{"count":2}' if mode == "repair" else "accepted"))

    def complete(request):
        return ContinueTurn("Revise.") if request.text == "private draft" else None

    agent = Agent(
        instructions="Answer.",
        model="gpt-5.5",
        provider=provider(responses),
        before_complete=complete if mode == "continue" else None,
    )
    async with timeout(20), agent.session(include_output_accepted=True) as session:
        turn = (
            await session.submit("go", output_type=PromptedOutput(Count, 1))
            if mode == "repair"
            else await session.submit("go")
        )
        events = [event async for event in turn.events()]
        accepted = [event for event in events if isinstance(event, OutputAccepted)]
        assert len(accepted) == 1
        assert accepted[0].session_id == session.id
        assert accepted[0].submission_id == turn.submission_id
        assert "private draft" not in repr(accepted)
        assert (await turn.get_result()).final_output == (
            Count(count=2) if mode == "repair" else "accepted"
        )
        assert len(responses.requests) == 2


@pytest.mark.parametrize("mode", ["reject", "cancel"])
async def test_rejected_and_cancelled_results_have_no_acceptance(responses, mode):
    entered = asyncio.Event()
    responses.respond(assistant_output("private candidate"))

    async def complete(request):
        if mode == "reject":
            return StopTurn("Rejected.")
        entered.set()
        await asyncio.Event().wait()

    agent = Agent(
        instructions="Answer.",
        model="gpt-5.5",
        provider=provider(responses),
        before_complete=complete,
    )
    async with timeout(20), agent.session(include_output_accepted=True) as session:
        turn = await session.submit("go")

        async def collect():
            return [event async for event in turn.events()]

        events = asyncio.create_task(collect())
        if mode == "cancel":
            await entered.wait()
            await turn.cancel()
        assert not any(isinstance(event, OutputAccepted) for event in await events)
        with pytest.raises(TurnStoppedError):
            await turn.get_result()


async def test_session_reader_receives_acceptance_without_a_result_waiter(responses):
    responses.respond(assistant_output("private answer"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session(
            include_output_accepted=True
        ) as session,
    ):
        stream = session.events()
        turn = await session.submit("go")
        async for event in stream:
            if isinstance(event, OutputAccepted):
                assert event.submission_id == turn.submission_id
                assert event.turn_id == turn.turn_id
                assert "private answer" not in repr(event)
                break
        else:
            pytest.fail("No accepted output observation")
        await stream.aclose()


async def test_default_stream_keeps_its_existing_completion_contract(responses):
    responses.respond(assistant_output("answer"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("go")
        events = [event async for event in turn.events()]
        assert not any(isinstance(event, OutputAccepted) for event in events)
        assert (await turn.get_result()).final_output == "answer"
        assert not [event async for event in turn.events() if isinstance(event, OutputAccepted)]


async def test_steering_successor_uses_final_turn_and_original_submission(responses, monkeypatch):
    from agents.observation import TurnStarted
    from tests.test_review_ii_steering_tail import ProjectContinuation

    responses.respond(assistant_output("initial"))
    responses.respond(assistant_output("final successor"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session(
            include_output_accepted=True, include_request_attempts=True
        ) as session,
    ):
        monkeypatch.setattr(session, "_native", ProjectContinuation(session._native))
        turn = await session.submit("go")
        events = [event async for event in turn.events()]
        started = [event for event in events if isinstance(event, TurnStarted)]
        accepted = [event for event in events if isinstance(event, OutputAccepted)]
        assert len(started) == 2
        assert len(accepted) == 1
        assert accepted[0].submission_id == turn.submission_id
        assert accepted[0].turn_id == started[-1].turn_id != turn.turn_id
        assert (await turn.get_result()).final_output == "final successor"
        assert len(responses.requests) == 2
        report = turn.request_attempts
        assert report is not None
        assert report.availability == "complete"
        assert sum(row.count for row in report.counts) == 1


async def test_invalid_typed_output_has_no_acceptance(responses):
    responses.respond(assistant_output('{"count":"private invalid value"}'))
    agent = Agent(instructions="Count.", provider=provider(responses))
    async with timeout(20), agent.session(include_output_accepted=True) as session:
        turn = await session.submit("go", output_type=PromptedOutput(Count))
        events = [event async for event in turn.events()]
        assert not any(isinstance(event, OutputAccepted) for event in events)
        with pytest.raises(OutputValidationError):
            await turn.get_result()

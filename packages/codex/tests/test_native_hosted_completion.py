"""Hosted-only native completions preserve their results through application acceptance."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, ContinueTurn, TurnStoppedError
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_hosted_tools import CASES, completed, provider


@pytest.mark.parametrize("tool,wire,raw,_fields,_include", CASES)
async def test_hosted_only_completion_continues_same_turn(
    responses, tool, wire, raw, _fields, _include
):
    calls = []

    def complete(request):
        calls.append(request)
        if request.continuation_count == 0:
            assert request.text is None
            return ContinueTurn("Explain the completed hosted result.")
        assert request.continuation_count == 1
        assert request.text == "Hosted result accepted."
        return None

    responses.respond(completed(raw))
    responses.respond(assistant_output("Hosted result accepted."))
    borrowed = provider(responses)
    agent = Agent(
        instructions="Use the hosted tool.",
        model="gpt-5.2",
        provider=borrowed,
        tools=[tool],
        before_complete=complete,
    )
    async with timeout(30), borrowed, agent.session() as session:
        turn = await session.submit("Perform the hosted task.")
        result = await turn.get_result()
        events = [event async for event in turn.events()]
        history = [item async for item in session.items() if item["type"] == raw["type"]]

    assert result.final_output == "Hosted result accepted."
    assert result.turn is turn
    assert [call.continuation_count for call in calls] == [0, 1]
    assert {call.turn_id for call in calls} == {turn.turn_id}
    assert len(responses.requests) == 2
    for request in responses.requests:
        assert next(item for item in request["tools"] if item["type"] == wire["type"]) == wire
    assert [item for item in responses.requests[1]["input"] if item.get("type") == raw["type"]] == [
        raw
    ]
    assert len(history) == 1
    assert history[0]["id"] == raw["id"]
    assert history[0]["turn_id"] == turn.turn_id
    assert history[0]["provider_data"] == raw
    assert [event.status for event in events if isinstance(event, TurnFinished)] == ["completed"]


@pytest.mark.parametrize("tool,_wire,raw,_fields,_include", CASES[-1:])
async def test_cancel_image_completion_joins_cleanup_and_discards_late_continuation(
    responses, tool, _wire, raw, _fields, _include
):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def complete(request):
        calls.append(request)
        assert request.text is None
        assert request.continuation_count == 0
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            return ContinueTurn("Late hosted feedback must be discarded.")

    assert raw["type"] == "image_generation_call"
    responses.respond(completed(raw))
    borrowed = provider(responses)
    agent = Agent(
        instructions="Generate the image.",
        model="gpt-5.2",
        provider=borrowed,
        tools=[tool],
        before_complete=complete,
    )
    async with timeout(30), borrowed, agent.session() as session:
        turn = await session.submit("Generate the image without assistant text.")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not stopping.done()
            assert len(responses.requests) == 1
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        events = [event async for event in turn.events()]
        images = [item async for item in session.items() if item["type"] == "image_generation_call"]

    assert len(calls) == 1
    assert calls[0].turn_id == turn.turn_id
    assert len(responses.requests) == 1
    assert [event.status for event in events if isinstance(event, TurnFinished)] == ["cancelled"]
    assert len(images) == 1
    assert images[0]["id"] == raw["id"]
    assert images[0]["turn_id"] == turn.turn_id
    assert images[0]["result"] == raw["result"]
    assert images[0]["provider_data"] == raw

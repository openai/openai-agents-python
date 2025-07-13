import json

import pytest

from agents import Agent, Runner

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_simple_streaming_with_cancel():
    model = FakeModel()
    agent = Agent(name="Joker", model=model)

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    num_events = 0
    stop_after = 1  # There are two that the model gives back.

    async for _event in result.stream_events():
        num_events += 1
        if num_events == stop_after:
            result.cancel()

    assert num_events == 1, f"Expected {stop_after} visible events, but got {num_events}"


@pytest.mark.asyncio
async def test_multiple_events_streaming_with_cancel():
    model = FakeModel()
    agent = Agent(
        name="Joker",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [
                get_text_message("a_message"),
                get_function_tool_call("foo", json.dumps({"a": "b"})),
            ],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    num_events = 0
    stop_after = 2

    async for _ in result.stream_events():
        num_events += 1
        if num_events == stop_after:
            result.cancel()

    assert num_events == stop_after, f"Expected {stop_after} visible events, but got {num_events}"


@pytest.mark.asyncio
async def test_cancel_prevents_further_events():
    model = FakeModel()
    agent = Agent(name="Joker", model=model)
    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    events = []
    async for event in result.stream_events():
        events.append(event)
        result.cancel()
        break  # Cancel after first event
    # Try to get more events after cancel
    more_events = [e async for e in result.stream_events()]
    assert len(events) == 1
    assert more_events == [], "No events should be yielded after cancel()"


@pytest.mark.asyncio
async def test_cancel_is_idempotent():
    model = FakeModel()
    agent = Agent(name="Joker", model=model)
    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    events = []
    async for event in result.stream_events():
        events.append(event)
        result.cancel()
        result.cancel()  # Call cancel again
        break
    # Should not raise or misbehave
    assert len(events) == 1


@pytest.mark.asyncio
async def test_cancel_before_streaming():
    model = FakeModel()
    agent = Agent(name="Joker", model=model)
    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    result.cancel()  # Cancel before streaming
    events = [e async for e in result.stream_events()]
    assert events == [], "No events should be yielded if cancel() is called before streaming."


@pytest.mark.asyncio
async def test_cancel_cleans_up_resources():
    model = FakeModel()
    agent = Agent(name="Joker", model=model)
    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    # Start streaming, then cancel
    async for _ in result.stream_events():
        result.cancel()
        break
    # After cancel, queues should be empty and is_complete True
    assert result.is_complete, "Result should be marked complete after cancel."
    assert result._event_queue.empty(), "Event queue should be empty after cancel."
    assert result._input_guardrail_queue.empty(), (
        "Input guardrail queue should be empty after cancel."
    )

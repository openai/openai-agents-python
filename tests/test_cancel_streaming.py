import asyncio
import json
import time

import pytest
from openai.types.responses import ResponseCompletedEvent

from agents import Agent, Runner
from agents.stream_events import RawResponsesStreamEvent

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


class SlowCompleteFakeModel(FakeModel):
    """A FakeModel that delays before emitting the completed event in streaming."""

    def __init__(self, delay_seconds: float):
        super().__init__()
        self._delay_seconds = delay_seconds

    async def stream_response(self, *args, **kwargs):
        async for ev in super().stream_response(*args, **kwargs):
            if isinstance(ev, ResponseCompletedEvent) and self._delay_seconds > 0:
                await asyncio.sleep(self._delay_seconds)
            yield ev


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


@pytest.mark.asyncio
async def test_cancel_immediate_mode_explicit():
    """Test explicit immediate mode behaves same as default."""
    model = FakeModel()
    agent = Agent(name="Joker", model=model)

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")

    async for _ in result.stream_events():
        result.cancel(mode="immediate")
        break

    assert result.is_complete
    assert result._event_queue.empty()
    assert result._cancel_mode == "immediate"


@pytest.mark.asyncio
async def test_stream_events_respects_asyncio_timeout_cancellation():
    model = SlowCompleteFakeModel(delay_seconds=0.5)
    model.set_next_output([get_text_message("Final response")])
    agent = Agent(name="TimeoutTester", model=model)

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    event_iter = result.stream_events().__aiter__()

    # Consume events until the output item is done so the next event is delayed.
    while True:
        event = await asyncio.wait_for(event_iter.__anext__(), timeout=1.0)
        if (
            isinstance(event, RawResponsesStreamEvent)
            and event.data.type == "response.output_item.done"
        ):
            break

    start = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(event_iter.__anext__(), timeout=0.1)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.3, "Cancellation should propagate promptly when waiting for events."
    result.cancel()


@pytest.mark.asyncio
async def test_cancel_immediate_unblocks_waiting_stream_consumer():
    block_event = asyncio.Event()

    class BlockingFakeModel(FakeModel):
        async def stream_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            await block_event.wait()
            async for event in super().stream_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            ):
                yield event

    model = BlockingFakeModel()
    agent = Agent(name="Joker", model=model)

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")

    async def consume_events():
        return [event async for event in result.stream_events()]

    consumer_task = asyncio.create_task(consume_events())
    await asyncio.sleep(0)

    result.cancel(mode="immediate")

    events = await asyncio.wait_for(consumer_task, timeout=1)

    assert len(events) <= 1
    assert not block_event.is_set()
    assert result.is_complete

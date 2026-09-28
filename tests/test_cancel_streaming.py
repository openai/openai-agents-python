import asyncio
import gc
import json
import time

import pytest
from openai.types.responses import ResponseCompletedEvent

from agents import Agent, ComputerProvider, ComputerTool, GuardrailFunctionOutput, Runner
from agents.decorators import tool
from agents.guardrail import input_guardrail
from agents.models.multi_provider import MultiProvider
from agents.result import RunResultStreaming
from agents.stream_events import RawResponsesStreamEvent
from agents.testing import ScriptedModel

from .test_computer_tool_lifecycle import FakeComputer
from .test_responses import get_function_tool, get_function_tool_call, get_text_message
from .testing_processor import fetch_events


class SlowCompleteScriptedModel(ScriptedModel):
    """A ScriptedModel that delays before emitting the completed event in streaming."""

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
    model = ScriptedModel()
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
    model = ScriptedModel()
    agent = Agent(
        name="Joker",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    model.extend(
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
    model = ScriptedModel()
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
    model = ScriptedModel()
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
async def test_cancel_before_streaming(
    monkeypatch: pytest.MonkeyPatch,
    recwarn: pytest.WarningsRecorder,
) -> None:
    closed: list[MultiProvider] = []

    async def record_close(provider: MultiProvider) -> None:
        closed.append(provider)

    monkeypatch.setattr(MultiProvider, "aclose", record_close)
    model = ScriptedModel()
    agent = Agent(name="Joker", model=model)
    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    result.cancel()  # Cancel before streaming
    events = [e async for e in result.stream_events()]
    gc.collect()

    assert events == [], "No events should be yielded if cancel() is called before streaming."
    assert len(closed) == 1
    assert not any(
        warning.category is RuntimeWarning and "was never awaited" in str(warning.message)
        for warning in recwarn
    )


@pytest.mark.asyncio
async def test_cancel_cleans_up_resources():
    model = ScriptedModel()
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
    model = ScriptedModel()
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
    model = SlowCompleteScriptedModel(delay_seconds=0.5)
    model.enqueue([get_text_message("Final response")])
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

    class BlockingScriptedModel(ScriptedModel):
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

    model = BlockingScriptedModel()
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


@pytest.mark.asyncio
async def test_run_loop_exception_property_is_none_on_success():
    """run_loop_exception is None when the stream completes without error."""
    model = ScriptedModel()
    model.enqueue([get_text_message("hello")])
    agent = Agent(name="A", model=model)

    result = Runner.run_streamed(agent, input="hi")
    async for _ in result.stream_events():
        pass

    assert result.run_loop_exception is None


@pytest.mark.asyncio
async def test_run_loop_exception_surfaced_after_stream():
    """run_loop_exception is set when the run loop raises before yielding events."""

    class BoomModel(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            raise RuntimeError("run loop boom")

        async def stream_response(self, *args, **kwargs):
            raise RuntimeError("run loop boom")
            yield  # make this an async generator

    agent = Agent(name="A", model=BoomModel())

    result = Runner.run_streamed(agent, input="hi")
    with pytest.raises(RuntimeError, match="run loop boom"):
        async for _ in result.stream_events():
            pass

    # Property must also expose the exception for callers who want to inspect it directly.
    assert result.run_loop_exception is not None
    assert isinstance(result.run_loop_exception, RuntimeError)
    assert "run loop boom" in str(result.run_loop_exception)


@pytest.mark.asyncio
async def test_falsy_run_loop_exception_is_surfaced_after_stream() -> None:
    class FalsyRuntimeError(RuntimeError):
        def __bool__(self) -> bool:
            return False

    class BoomModel(ScriptedModel):
        async def stream_response(self, *args, **kwargs):
            raise FalsyRuntimeError("falsy run loop boom")
            yield

    result = Runner.run_streamed(Agent(name="A", model=BoomModel()), input="hi")

    with pytest.raises(FalsyRuntimeError, match="falsy run loop boom"):
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_falsy_input_guardrail_exception_is_surfaced_after_stream() -> None:
    class FalsyRuntimeError(RuntimeError):
        def __bool__(self) -> bool:
            return False

    @input_guardrail
    async def raising_guardrail(context, agent, input):
        raise FalsyRuntimeError("falsy guardrail boom")

    model = ScriptedModel()
    model.enqueue([get_text_message("done")])
    result = Runner.run_streamed(
        Agent(name="A", model=model, input_guardrails=[raising_guardrail]),
        input="hi",
    )

    with pytest.raises(FalsyRuntimeError, match="falsy guardrail boom"):
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_cancel_with_pending_parallel_input_guardrail_finishes_cleanup() -> None:
    guardrail_started = asyncio.Event()
    tool_started = asyncio.Event()
    disposed: list[FakeComputer] = []

    @input_guardrail
    async def slow_guardrail(context, agent, input):
        guardrail_started.set()
        await asyncio.Event().wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    @tool
    async def slow_tool() -> str:
        await guardrail_started.wait()
        tool_started.set()
        await asyncio.Event().wait()
        return "unreachable"

    computer_tool = ComputerTool(
        computer=ComputerProvider[FakeComputer](
            create=lambda *, run_context: FakeComputer(),
            dispose=lambda *, run_context, computer: disposed.append(computer),
        )
    )
    agent = Agent(
        name="A",
        model=ScriptedModel([[get_function_tool_call("slow_tool", "{}", "call_1")]]),
        tools=[slow_tool, computer_tool],
        input_guardrails=[slow_guardrail],
    )
    result = Runner.run_streamed(agent, input="hi")

    async def consume() -> None:
        async for _ in result.stream_events():
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=2)
        result.cancel()
        await asyncio.wait_for(consumer, timeout=2)
        assert len(disposed) == 1
        events = fetch_events()
        assert events.count("trace_start") == events.count("trace_end") == 1
        assert events.count("span_start") == events.count("span_end")
    finally:
        result.cancel()
        assert result.run_loop_task is not None
        await asyncio.gather(consumer, result.run_loop_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_target", ["guardrail", "run_loop"])
async def test_consumer_cancellation_at_terminal_wait_propagates(
    monkeypatch: pytest.MonkeyPatch, wait_target: str
) -> None:
    wait_started = asyncio.Event()
    guardrail_started = asyncio.Event()
    disposal_started = asyncio.Event()
    child_cancelled = asyncio.Event()

    @input_guardrail
    async def slow_guardrail(context, agent, input):
        guardrail_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            child_cancelled.set()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    async def dispose(**kwargs) -> None:
        disposal_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            child_cancelled.set()

    computer_tool = ComputerTool(
        computer=ComputerProvider[FakeComputer](
            create=lambda *, run_context: FakeComputer(), dispose=dispose
        )
    )
    result = Runner.run_streamed(
        Agent(
            name="A",
            model=ScriptedModel([[get_text_message("done")]]),
            input_guardrails=[slow_guardrail] if wait_target == "guardrail" else [],
            tools=[computer_tool] if wait_target == "run_loop" else [],
        ),
        input="hi",
    )
    original_wait = RunResultStreaming._await_task_safely

    async def observe_wait(self, task) -> None:
        target = self._input_guardrails_task if wait_target == "guardrail" else self.run_loop_task
        if self is result and task is target and task is not None and not task.done():
            wait_started.set()
        await original_wait(self, task)

    # Observe entry to the terminal wait without changing its behavior. Public events do not
    # expose this boundary, and the consumer must already be suspended here before cancellation.
    monkeypatch.setattr(RunResultStreaming, "_await_task_safely", observe_wait)

    async def consume() -> None:
        async for _ in result.stream_events():
            pass

    consumer = asyncio.create_task(consume())
    try:
        child_started = guardrail_started if wait_target == "guardrail" else disposal_started
        await asyncio.wait_for(child_started.wait(), timeout=2)
        await asyncio.wait_for(wait_started.wait(), timeout=2)
        assert result.final_output == "done"
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, timeout=2)
        await asyncio.wait_for(child_cancelled.wait(), timeout=2)
        if wait_target == "guardrail":
            events = fetch_events()
            assert events.count("trace_start") == events.count("trace_end") == 1
            assert events.count("span_start") == events.count("span_end")
    finally:
        result.cancel()
        assert result.run_loop_task is not None
        await asyncio.gather(consumer, result.run_loop_task, return_exceptions=True)

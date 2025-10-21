from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from openai.types.responses import ResponseCompletedEvent

from agents import Agent, GuardrailFunctionOutput, InputGuardrail, RunContextWrapper, Runner
from agents.exceptions import InputGuardrailTripwireTriggered
from agents.items import TResponseInputItem
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message
from tests.testing_processor import fetch_events, fetch_ordered_spans


def make_input_guardrail(delay_seconds: float, *, trip: bool) -> InputGuardrail[Any]:
    async def guardrail(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        # Simulate variable guardrail completion timing.
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return GuardrailFunctionOutput(
            output_info={"delay": delay_seconds}, tripwire_triggered=trip
        )

    name = "tripping_input_guardrail" if trip else "delayed_input_guardrail"
    return InputGuardrail(guardrail_function=guardrail, name=name)


@pytest.mark.asyncio
@pytest.mark.parametrize("guardrail_delay", [0.0, 0.2])
async def test_run_streamed_input_guardrail_timing_is_consistent(guardrail_delay: float):
    """Ensure streaming behavior matches when input guardrail finishes before and after LLM stream.

    We verify that:
    - The sequence of streamed event types is identical.
    - Final output matches.
    - Exactly one input guardrail result is recorded and does not trigger.
    """

    # Arrange: Agent with a single text output and a delayed input guardrail
    model = FakeModel()
    model.set_next_output([get_text_message("Final response")])

    agent = Agent(
        name="TimingAgent",
        model=model,
        input_guardrails=[make_input_guardrail(guardrail_delay, trip=False)],
    )

    # Act: Run streamed and collect event types
    result = Runner.run_streamed(agent, input="Hello")
    event_types: list[str] = []

    async for event in result.stream_events():
        event_types.append(event.type)

    # Assert: Guardrail results populated and identical behavioral outcome
    assert len(result.input_guardrail_results) == 1, "Expected exactly one input guardrail result"
    assert result.input_guardrail_results[0].guardrail.get_name() == "delayed_input_guardrail", (
        "Guardrail name mismatch"
    )
    assert result.input_guardrail_results[0].output.tripwire_triggered is False, (
        "Guardrail should not trigger in this test"
    )

    # Final output should be the text from the model's single message
    assert result.final_output == "Final response"

    # Minimal invariants on event sequence to ensure stability across timing
    # Must start with agent update and include raw response events
    assert len(event_types) >= 3, f"Unexpectedly few events: {event_types}"
    assert event_types[0] == "agent_updated_stream_event"
    # Ensure we observed raw response events in the stream irrespective of guardrail timing
    assert any(t == "raw_response_event" for t in event_types)


@pytest.mark.asyncio
async def test_run_streamed_input_guardrail_sequences_match_between_fast_and_slow():
    """Run twice with fast vs slow input guardrail and compare event sequences exactly."""

    async def run_once(delay: float) -> list[str]:
        model = FakeModel()
        model.set_next_output([get_text_message("Final response")])
        agent = Agent(
            name="TimingAgent",
            model=model,
            input_guardrails=[make_input_guardrail(delay, trip=False)],
        )
        result = Runner.run_streamed(agent, input="Hello")
        events: list[str] = []
        async for ev in result.stream_events():
            events.append(ev.type)
        return events

    events_fast = await run_once(0.0)
    events_slow = await run_once(0.2)

    assert events_fast == events_slow, (
        f"Event sequences differ between guardrail timings:\nfast={events_fast}\nslow={events_slow}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("guardrail_delay", [0.0, 0.2])
async def test_run_streamed_input_guardrail_tripwire_raises(guardrail_delay: float):
    """Guardrail tripwire must raise from stream_events regardless of timing."""

    model = FakeModel()
    model.set_next_output([get_text_message("Final response")])

    agent = Agent(
        name="TimingAgentTrip",
        model=model,
        input_guardrails=[make_input_guardrail(guardrail_delay, trip=True)],
    )

    result = Runner.run_streamed(agent, input="Hello")

    with pytest.raises(InputGuardrailTripwireTriggered) as excinfo:
        async for _ in result.stream_events():
            pass

    # Exception contains the guardrail result and run data
    exc = excinfo.value
    assert exc.guardrail_result.output.tripwire_triggered is True
    assert exc.run_data is not None
    assert len(exc.run_data.input_guardrail_results) == 1
    assert (
        exc.run_data.input_guardrail_results[0].guardrail.get_name() == "tripping_input_guardrail"
    )


class SlowCompleteFakeModel(FakeModel):
    """A FakeModel that delays just before emitting ResponseCompletedEvent in streaming."""

    def __init__(self, delay_seconds: float, tracing_enabled: bool = True):
        super().__init__(tracing_enabled=tracing_enabled)
        self._delay_seconds = delay_seconds

    async def stream_response(self, *args, **kwargs):
        async for ev in super().stream_response(*args, **kwargs):
            if isinstance(ev, ResponseCompletedEvent) and self._delay_seconds > 0:
                await asyncio.sleep(self._delay_seconds)
            yield ev


def _get_span_by_type(spans, span_type: str):
    for s in spans:
        exported = s.export()
        if not exported:
            continue
        if exported.get("span_data", {}).get("type") == span_type:
            return s
    return None


def _iso(s: str | None) -> datetime:
    assert s is not None
    return datetime.fromisoformat(s)


@pytest.mark.asyncio
async def test_parent_span_and_trace_finish_after_slow_input_guardrail():
    """Agent span and trace finish after guardrail when guardrail completes last."""

    model = FakeModel(tracing_enabled=True)
    model.set_next_output([get_text_message("Final response")])
    agent = Agent(
        name="TimingAgentTrace",
        model=model,
        input_guardrails=[make_input_guardrail(0.2, trip=False)],  # guardrail slower than model
    )

    result = Runner.run_streamed(agent, input="Hello")
    async for _ in result.stream_events():
        pass

    spans = fetch_ordered_spans()
    agent_span = _get_span_by_type(spans, "agent")
    guardrail_span = _get_span_by_type(spans, "guardrail")
    generation_span = _get_span_by_type(spans, "generation")

    assert agent_span and guardrail_span and generation_span, (
        "Expected agent, guardrail, generation spans"
    )

    # Agent span must finish last
    assert _iso(agent_span.ended_at) >= _iso(guardrail_span.ended_at)
    assert _iso(agent_span.ended_at) >= _iso(generation_span.ended_at)

    # Trace should end after all spans end
    events = fetch_events()
    assert events[-1] == "trace_end"


@pytest.mark.asyncio
async def test_parent_span_and_trace_finish_after_slow_model():
    """Agent span and trace finish after model when model completes last."""

    model = SlowCompleteFakeModel(delay_seconds=0.2, tracing_enabled=True)
    model.set_next_output([get_text_message("Final response")])
    agent = Agent(
        name="TimingAgentTrace",
        model=model,
        input_guardrails=[make_input_guardrail(0.0, trip=False)],  # guardrail faster than model
    )

    result = Runner.run_streamed(agent, input="Hello")
    async for _ in result.stream_events():
        pass

    spans = fetch_ordered_spans()
    agent_span = _get_span_by_type(spans, "agent")
    guardrail_span = _get_span_by_type(spans, "guardrail")
    generation_span = _get_span_by_type(spans, "generation")

    assert agent_span and guardrail_span and generation_span, (
        "Expected agent, guardrail, generation spans"
    )

    # Agent span must finish last
    assert _iso(agent_span.ended_at) >= _iso(guardrail_span.ended_at)
    assert _iso(agent_span.ended_at) >= _iso(generation_span.ended_at)

    events = fetch_events()
    assert events[-1] == "trace_end"

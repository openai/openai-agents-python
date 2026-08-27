from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest
from openai.types.responses import ResponseCompletedEvent

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    MaxTurnsExceeded,
    RunContextWrapper,
    Runner,
)
from agents.exceptions import InputGuardrailTripwireTriggered
from agents.items import TResponseInputItem
from agents.testing import ScriptedModel
from tests.test_responses import get_function_tool, get_function_tool_call, get_text_message
from tests.testing_processor import fetch_events, fetch_ordered_spans

FAST_GUARDRAIL_DELAY = 0.005
SLOW_GUARDRAIL_DELAY = 0.02


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
async def test_input_guardrail_results_follow_completion_order():
    async def fast_guardrail(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(0)
        return GuardrailFunctionOutput(output_info={"delay": 0.0}, tripwire_triggered=False)

    async def slow_guardrail(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(FAST_GUARDRAIL_DELAY)
        return GuardrailFunctionOutput(
            output_info={"delay": FAST_GUARDRAIL_DELAY}, tripwire_triggered=False
        )

    model = ScriptedModel()
    model.enqueue([get_text_message("Final response")])

    agent = Agent(
        name="TimingAgentOrder",
        model=model,
        input_guardrails=[
            InputGuardrail(guardrail_function=slow_guardrail, name="slow_guardrail"),
            InputGuardrail(guardrail_function=fast_guardrail, name="fast_guardrail"),
        ],
    )

    result = Runner.run_streamed(agent, input="Hello")
    async for _ in result.stream_events():
        pass

    delays = [res.output.output_info["delay"] for res in result.input_guardrail_results]
    assert delays == [0.0, FAST_GUARDRAIL_DELAY]


@pytest.mark.asyncio
@pytest.mark.parametrize("guardrail_delay", [0.0, SLOW_GUARDRAIL_DELAY])
async def test_run_streamed_input_guardrail_timing_is_consistent(guardrail_delay: float):
    """Ensure streaming behavior matches when input guardrail finishes before and after LLM stream.

    We verify that:
    - The sequence of streamed event types is identical.
    - Final output matches.
    - Exactly one input guardrail result is recorded and does not trigger.
    """

    # Arrange: Agent with a single text output and a delayed input guardrail
    model = ScriptedModel()
    model.enqueue([get_text_message("Final response")])

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
        model = ScriptedModel()
        model.enqueue([get_text_message("Final response")])
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
    events_slow = await run_once(SLOW_GUARDRAIL_DELAY)

    assert events_fast == events_slow, (
        f"Event sequences differ between guardrail timings:\nfast={events_fast}\nslow={events_slow}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("guardrail_delay", [0.0, SLOW_GUARDRAIL_DELAY])
async def test_run_streamed_input_guardrail_tripwire_raises(guardrail_delay: float):
    """Guardrail tripwire must raise from stream_events regardless of timing."""

    model = ScriptedModel()
    model.enqueue([get_text_message("Final response")])

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


@pytest.mark.asyncio
async def test_max_turns_does_not_clobber_input_guardrail_tripwire():
    """A guardrail tripwire recorded before max_turns fires must win over MaxTurnsExceeded.

    Regression test: RunResultStreaming._check_errors() re-creates a fresh
    MaxTurnsExceeded and overwrites self._stored_exception on *every* call once
    current_turn > max_turns, because self._max_turns_handled is only ever set
    True by the max_turns error-handler path -- never in the default (no
    handler) path. stream_events() calls _check_errors() again unconditionally
    in its `finally` block, so a guardrail trip that was already captured as
    InputGuardrailTripwireTriggered got silently replaced with MaxTurnsExceeded
    by that final call. Callers using the documented
    `except InputGuardrailTripwireTriggered` pattern never saw the tripwire.

    This race must not be ordered with a real-time sleep: a fixed delay only
    approximates "the guardrail finishes after max_turns is exceeded", and
    under CI load, tracing overhead, or slower model instrumentation the
    guardrail can instead finish *before* current_turn > max_turns is ever
    reached, in which case the test would observe the correct exception even
    against the unpatched (buggy) implementation and silently stop being a
    regression test. Instead, an `error_handlers={"max_turns": ...}` hook
    (returning None, so it falls through to the exact same default raise path
    as if no handler were registered) sets an `asyncio.Event` at the precise
    moment the run loop establishes current_turn > max_turns. The guardrail
    awaits that event before returning its tripwire, so it can only ever
    resolve *after* the max-turns condition genuinely holds.
    """

    max_turns_reached = asyncio.Event()

    async def tripping_guardrail(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        # Wait for the run loop to have actually established current_turn >
        # max_turns, rather than guessing at a delay long enough to outlast it.
        await max_turns_reached.wait()
        return GuardrailFunctionOutput(output_info={"reason": "blocked"}, tripwire_triggered=True)

    model = ScriptedModel()
    func_output = json.dumps({"a": "b"})
    model.extend(
        [
            [
                get_text_message(str(i)),
                get_function_tool_call("some_function", func_output, str(i)),
            ]
            for i in range(1, 10)
        ]
    )

    agent = Agent(
        name="MaxTurnsGuardrailAgent",
        model=model,
        tools=[get_function_tool("some_function", "result")],
        # run_in_parallel defaults to True -- this is the default configuration,
        # not an opt-in one.
        input_guardrails=[InputGuardrail(guardrail_function=tripping_guardrail, name="trip")],
    )

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=1,
        # Declining (returning None) preserves the exact default max_turns
        # behavior; the handler exists purely to signal, deterministically,
        # the moment current_turn > max_turns is established.
        error_handlers={"max_turns": lambda data: max_turns_reached.set()},
    )

    raised: BaseException | None = None
    try:
        async for _ in result.stream_events():
            pass
    except BaseException as exc:  # noqa: BLE001 - we need to inspect the exact type raised
        raised = exc

    assert isinstance(raised, InputGuardrailTripwireTriggered), (
        f"Expected InputGuardrailTripwireTriggered, got "
        f"{type(raised).__name__ if raised else None}. The tripped guardrail "
        "result was silently clobbered by a freshly-minted MaxTurnsExceeded."
    )
    assert not isinstance(raised, MaxTurnsExceeded)


class SlowCompleteScriptedModel(ScriptedModel):
    """A ScriptedModel that delays just before emitting ResponseCompletedEvent in streaming."""

    def __init__(self, delay_seconds: float, emit_traces: bool = True):
        super().__init__(emit_traces=emit_traces)
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

    model = ScriptedModel(emit_traces=True)
    model.enqueue([get_text_message("Final response")])
    agent = Agent(
        name="TimingAgentTrace",
        model=model,
        input_guardrails=[make_input_guardrail(SLOW_GUARDRAIL_DELAY, trip=False)],
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

    model = SlowCompleteScriptedModel(delay_seconds=SLOW_GUARDRAIL_DELAY, emit_traces=True)
    model.enqueue([get_text_message("Final response")])
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

"""Tests for ReasoningDeltaEvent stream event (issue #825)."""

from __future__ import annotations

import pytest
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary

from agents import Agent, Runner
from agents.stream_events import RawResponsesStreamEvent, ReasoningDeltaEvent

from .fake_model import FakeModel
from .test_responses import get_text_message


def _make_reasoning_item(text: str) -> ResponseReasoningItem:
    return ResponseReasoningItem(
        id="rs_test",
        type="reasoning",
        summary=[Summary(text=text, type="summary_text")],
    )


@pytest.mark.asyncio
async def test_reasoning_delta_event_emitted_during_streaming() -> None:
    """ReasoningDeltaEvent is emitted when the model streams a reasoning summary delta."""
    model = FakeModel()
    model.set_next_output(
        [
            _make_reasoning_item("Let me think..."),
            get_text_message("Answer"),
        ]
    )

    agent = Agent(name="A", model=model)
    result = Runner.run_streamed(agent, input="hi")

    reasoning_deltas: list[ReasoningDeltaEvent] = []
    async for event in result.stream_events():
        if isinstance(event, ReasoningDeltaEvent):
            reasoning_deltas.append(event)

    assert len(reasoning_deltas) >= 1
    assert all(isinstance(e.delta, str) for e in reasoning_deltas)
    assert all(isinstance(e.snapshot, str) for e in reasoning_deltas)
    assert all(e.type == "reasoning_delta" for e in reasoning_deltas)


@pytest.mark.asyncio
async def test_reasoning_delta_snapshot_accumulates() -> None:
    """The snapshot field grows monotonically across delta events."""
    model = FakeModel()
    model.set_next_output(
        [
            _make_reasoning_item("Hello world"),
            get_text_message("done"),
        ]
    )

    agent = Agent(name="A", model=model)
    result = Runner.run_streamed(agent, input="hi")

    snapshots: list[str] = []
    async for event in result.stream_events():
        if isinstance(event, ReasoningDeltaEvent):
            snapshots.append(event.snapshot)

    # The stream must have produced at least one snapshot, otherwise the
    # subsequent monotonic and content checks would silently pass on a
    # broken implementation that never emits ReasoningDeltaEvent at all.
    assert snapshots, "expected at least one ReasoningDeltaEvent snapshot"

    # Each snapshot must be at least as long as the previous one
    for i in range(1, len(snapshots)):
        assert len(snapshots[i]) >= len(snapshots[i - 1])

    # Last snapshot must contain the full reasoning text
    assert "Hello world" in snapshots[-1]


@pytest.mark.asyncio
async def test_no_reasoning_delta_event_without_reasoning() -> None:
    """ReasoningDeltaEvent is not emitted when there is no reasoning in the response."""
    model = FakeModel()
    model.set_next_output([get_text_message("plain text answer")])

    agent = Agent(name="A", model=model)
    result = Runner.run_streamed(agent, input="hi")

    event_count = 0
    async for event in result.stream_events():
        event_count += 1
        assert not isinstance(event, ReasoningDeltaEvent), (
            "Got unexpected ReasoningDeltaEvent for a plain text response"
        )

    # Sanity: ensure the run actually streamed something so the
    # negative assertion above isn't passing on an empty event stream.
    assert event_count > 0, "expected the stream to yield at least one event"


@pytest.mark.asyncio
async def test_reasoning_delta_event_type_field() -> None:
    """ReasoningDeltaEvent.type is always 'reasoning_delta'."""
    model = FakeModel()
    model.set_next_output(
        [
            _make_reasoning_item("some reasoning"),
            get_text_message("answer"),
        ]
    )

    agent = Agent(name="A", model=model)
    result = Runner.run_streamed(agent, input="hi")

    found = False
    async for event in result.stream_events():
        if isinstance(event, ReasoningDeltaEvent):
            assert event.type == "reasoning_delta"
            found = True
            break
    assert found, "Expected at least one ReasoningDeltaEvent but none were emitted"


@pytest.mark.asyncio
async def test_raw_response_events_still_emitted_alongside_reasoning_delta() -> None:
    """RawResponsesStreamEvent is still emitted even when ReasoningDeltaEvent is also emitted."""
    model = FakeModel()
    model.set_next_output(
        [
            _make_reasoning_item("thinking"),
            get_text_message("result"),
        ]
    )

    agent = Agent(name="A", model=model)
    result = Runner.run_streamed(agent, input="hi")

    raw_events: list[RawResponsesStreamEvent] = []
    reasoning_events: list[ReasoningDeltaEvent] = []

    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent):
            raw_events.append(event)
        elif isinstance(event, ReasoningDeltaEvent):
            reasoning_events.append(event)

    # Both types should be present
    assert len(raw_events) > 0
    assert len(reasoning_events) > 0


@pytest.mark.asyncio
async def test_reasoning_delta_event_importable_from_agents() -> None:
    """ReasoningDeltaEvent can be imported directly from the agents package."""
    from agents import ReasoningDeltaEvent as RDE

    assert RDE is ReasoningDeltaEvent


def test_reasoning_delta_event_dataclass() -> None:
    """ReasoningDeltaEvent is a proper dataclass with expected fields."""
    event = ReasoningDeltaEvent(delta="chunk", snapshot="full chunk")
    assert event.delta == "chunk"
    assert event.snapshot == "full chunk"
    assert event.type == "reasoning_delta"

"""Tests that RealtimeSession creates agent spans for SDK-level tracing."""

from __future__ import annotations

from typing import Any

import pytest

from agents.realtime.agent import RealtimeAgent
from agents.realtime.model import RealtimeModel, RealtimeModelConfig, RealtimeModelListener
from agents.realtime.model_events import RealtimeModelEvent
from agents.realtime.session import RealtimeSession
from agents.tracing import trace
from agents.tracing.span_data import AgentSpanData
from tests.testing_processor import SPAN_PROCESSOR_TESTING


class _FakeRealtimeModel(RealtimeModel):
    """Minimal fake that never sends events and succeeds immediately."""

    def __init__(self) -> None:
        self._listeners: list[RealtimeModelListener] = []

    def add_listener(self, listener: RealtimeModelListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: RealtimeModelListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def connect(self, options: RealtimeModelConfig) -> None:
        pass

    async def close(self) -> None:
        pass

    async def send_event(self, event: Any) -> None:
        pass

    async def send_message(
        self, message: Any, other_event_data: dict[str, Any] | None = None
    ) -> None:
        pass

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        pass

    async def send_tool_output(self, tool_call: Any, output: str, start_response: bool) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    async def dispatch(self, event: RealtimeModelEvent) -> None:
        """Send an event to all listeners (test helper)."""
        for listener in self._listeners:
            await listener.on_event(event)


def _make_session(
    agent: RealtimeAgent,
    model: _FakeRealtimeModel | None = None,
    *,
    tracing_disabled: bool = False,
) -> RealtimeSession:
    return RealtimeSession(
        model=model or _FakeRealtimeModel(),
        agent=agent,
        context=None,
        run_config={"tracing_disabled": tracing_disabled} if tracing_disabled else {},
    )


@pytest.mark.asyncio
async def test_session_creates_agent_span_on_enter():
    """Entering a RealtimeSession context must create an agent span."""
    agent = RealtimeAgent(name="greeter")
    session = _make_session(agent)

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert len(agent_spans) == 1, f"Expected 1 agent span, got {len(agent_spans)}"


@pytest.mark.asyncio
async def test_session_agent_span_has_correct_name():
    """The agent span name must match the RealtimeAgent name."""
    agent = RealtimeAgent(name="support_bot")
    session = _make_session(agent)

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert agent_spans[0].span_data.name == "support_bot"


@pytest.mark.asyncio
async def test_session_agent_span_finished_after_close():
    """The agent span must be finished (exported) once the session closes."""
    agent = RealtimeAgent(name="closer")
    session = _make_session(agent)

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert agent_spans[0].ended_at is not None


@pytest.mark.asyncio
async def test_session_span_includes_tool_names():
    """The agent span records the names of tools available to the agent."""
    from agents.tool import function_tool

    @function_tool
    def my_tool() -> str:
        """A test tool."""
        return "ok"

    agent = RealtimeAgent(name="tool_agent", tools=[my_tool])
    session = _make_session(agent)

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert agent_spans[0].span_data.tools == ["my_tool"]


@pytest.mark.asyncio
async def test_session_span_includes_handoff_names():
    """The agent span records the names of handoff targets."""
    child = RealtimeAgent(name="specialist")
    agent = RealtimeAgent(name="router", handoffs=[child])
    session = _make_session(agent)

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert agent_spans[0].span_data.handoffs == ["specialist"]


@pytest.mark.asyncio
async def test_tracing_disabled_creates_no_agent_spans():
    """When tracing_disabled=True, no agent spans should be emitted."""
    agent = RealtimeAgent(name="silent")
    session = _make_session(agent, tracing_disabled=True)

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert len(agent_spans) == 0, f"Expected 0 agent spans, got {len(agent_spans)}"

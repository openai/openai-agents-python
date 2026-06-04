"""Tests that RealtimeSession creates agent spans for SDK-level tracing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents.realtime.agent import RealtimeAgent
from agents.realtime.model import RealtimeModel, RealtimeModelConfig, RealtimeModelListener
from agents.realtime.model_events import RealtimeModelEvent, RealtimeModelToolCallEvent
from agents.realtime.session import RealtimeSession
from agents.tracing import trace
from agents.tracing.scope import Scope
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


@pytest.mark.asyncio
async def test_no_active_trace_does_not_poison_span_context():
    """Without an outer trace(), the session must not install a NoOpSpan as current.

    Convention: provider returns NoOpSpan when no active trace exists. Installing
    a NoOpSpan as current would make every span created afterward also a NoOpSpan
    (provider._is_noop_span check). The session must skip Scope.set_current_span()
    for NoOpSpans so ambient context is unchanged after the session closes.
    """
    span_before = Scope.get_current_span()
    agent = RealtimeAgent(name="agent")
    session = _make_session(agent)

    # Enter/exit WITHOUT any enclosing trace — span will be a NoOpSpan.
    async with session:
        pass

    span_after = Scope.get_current_span()
    assert span_before is span_after, (
        "Session must not permanently alter the current span context when no active trace exists."
    )


@pytest.mark.asyncio
async def test_disabled_handoff_excluded_from_span_metadata():
    """Handoffs with is_enabled=False must not appear in span handoff metadata.

    Convention: span metadata must reflect what was actually sent to the model.
    _get_handoffs() filters by is_enabled; raw agent.handoffs must not be used.
    """
    from agents.realtime.handoffs import realtime_handoff

    specialist = RealtimeAgent(name="specialist")
    disabled_handoff = realtime_handoff(specialist, is_enabled=False)
    agent = RealtimeAgent(name="router", handoffs=[disabled_handoff])
    session = _make_session(agent)

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert agent_spans[0].span_data.handoffs is None, (
        f"Disabled handoff should not appear in span metadata, "
        f"got: {agent_spans[0].span_data.handoffs}"
    )


@pytest.mark.asyncio
async def test_cleanup_from_different_task_does_not_raise():
    """_cleanup called from a task other than __aenter__'s task must not raise ValueError.

    close() is public and __aiter__ also calls _cleanup when _stored_exception is set.
    Both can run in a different asyncio task than __aenter__. Resetting a contextvars
    token from a different task raises ValueError — this must be caught gracefully.
    """
    agent = RealtimeAgent(name="agent")
    session = _make_session(agent)

    with trace("test"):
        await session.enter()  # open the session in this (main) task

        # Call _cleanup from a background task — it gets a copied context, so the
        # token stored by __aenter__ in the main task cannot be reset here; must not raise.
        async def close_from_other_task() -> None:
            await session._cleanup()

        await asyncio.create_task(close_from_other_task())

    assert session._closed is True


@pytest.mark.asyncio
async def test_span_context_clean_after_close_called_directly():
    """Span context must be reset even when close() is called directly (no async with).

    Method: enter via session.enter(), call close() directly, verify Scope is clean.
    """
    span_before = Scope.get_current_span()
    agent = RealtimeAgent(name="agent")
    session = _make_session(agent)

    with trace("test"):
        await session.enter()
        await session.close()

    span_after = Scope.get_current_span()
    assert span_before is span_after, "Calling close() directly must still reset the span context."


@pytest.mark.asyncio
async def test_handoff_span_is_sibling_not_child_of_initial_span():
    """After a handoff the new agent span must be a sibling of the first, not its child.

    Convention: the incoming agent span's parent_id must not equal the outgoing agent
    span's span_id. Both should be direct children of the trace root (parent_id=None).
    """
    specialist = RealtimeAgent(name="specialist")
    router = RealtimeAgent(name="router", handoffs=[specialist])
    model = _FakeRealtimeModel()
    session = _make_session(router, model)

    with trace("test"):
        async with session:
            # Fire the handoff tool call that the model would send.
            await model.dispatch(
                RealtimeModelToolCallEvent(
                    name="transfer_to_specialist",
                    call_id="call_001",
                    arguments="{}",
                )
            )
            # Let the background task spawned by async_tool_calls complete.
            await asyncio.sleep(0.05)

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert len(agent_spans) == 2, (
        f"Expected 2 agent spans (router + specialist), got {len(agent_spans)}"
    )

    router_span = next(s for s in agent_spans if s.span_data.name == "router")
    specialist_span = next(s for s in agent_spans if s.span_data.name == "specialist")

    assert specialist_span.parent_id != router_span.span_id, (
        "Specialist span must not be a child of the router span. "
        f"specialist.parent_id={specialist_span.parent_id}, router.span_id={router_span.span_id}"
    )

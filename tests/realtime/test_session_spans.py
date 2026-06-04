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
    """Without an outer trace(), the session must not alter the ambient span context.

    Convention: RealtimeSession never installs agent spans as the ContextVar current span,
    so the context is always unchanged before and after the session regardless of whether
    a real trace exists.
    """
    span_before = Scope.get_current_span()
    agent = RealtimeAgent(name="agent")
    session = _make_session(agent)

    # Enter/exit WITHOUT any enclosing trace.
    async with session:
        pass

    span_after = Scope.get_current_span()
    assert span_before is span_after, "Session must not permanently alter the current span context."


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
    """_cleanup called from a different asyncio task must not raise and must close the session.

    close() is public and __aiter__ also calls _cleanup when _stored_exception is set.
    Both can run in a different asyncio task than __aenter__.
    """
    agent = RealtimeAgent(name="agent")
    session = _make_session(agent)

    with trace("test"):
        await session.enter()

        async def close_from_other_task() -> None:
            await session._cleanup()

        await asyncio.create_task(close_from_other_task())

    assert session._closed is True


@pytest.mark.asyncio
async def test_span_context_unchanged_after_close_called_directly():
    """Ambient span context must be unchanged whether exited via async with or close().

    Convention: RealtimeSession never installs agent spans as the ContextVar current span,
    so close() has no context cleanup to perform; state before and after is identical.
    """
    span_before = Scope.get_current_span()
    agent = RealtimeAgent(name="agent")
    session = _make_session(agent)

    with trace("test"):
        await session.enter()
        await session.close()

    span_after = Scope.get_current_span()
    assert span_before is span_after, "Calling close() directly must not alter the span context."


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


@pytest.mark.asyncio
async def test_aenter_failure_finishes_span():
    """If __aenter__ raises after the span is started, the span must still be finished.

    Python does not call __aexit__ when __aenter__ raises, so the except BaseException
    block in __aenter__ is the only cleanup path. Verify no unfinished span is leaked.
    """

    class _FailingConnectModel(_FakeRealtimeModel):
        async def connect(self, options: Any) -> None:
            raise RuntimeError("simulated connection failure")

    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(
        model=_FailingConnectModel(),
        agent=agent,
        context=None,
        run_config={},
    )

    with trace("test"):
        with pytest.raises(RuntimeError):
            async with session:
                pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert len(agent_spans) == 1, f"Expected 1 agent span, got {len(agent_spans)}"
    assert agent_spans[0].ended_at is not None, (
        "Agent span must be finished (not leaked) when __aenter__ raises."
    )


@pytest.mark.asyncio
async def test_span_tool_metadata_reflects_model_config_override():
    """model_config.initial_model_settings tool override must be reflected in span metadata.

    Convention: span metadata must match what was actually sent to the model. When
    initial_model_settings overrides tools (e.g. to empty), the span must show the
    override — not the agent's default tool list.
    """
    from agents.tool import function_tool

    @function_tool
    def my_tool() -> str:
        """A test tool."""
        return "ok"

    agent = RealtimeAgent(name="tool_agent", tools=[my_tool])
    # model_config overrides tools with an empty list, wiping the agent's tool.
    session = RealtimeSession(
        model=_FakeRealtimeModel(),
        agent=agent,
        context=None,
        model_config={"initial_model_settings": {"tools": []}},
        run_config={},
    )

    with trace("test"):
        async with session:
            pass

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert agent_spans[0].span_data.tools is None, (
        f"model_config tool override must clear tools from span, "
        f"got: {agent_spans[0].span_data.tools}"
    )


@pytest.mark.asyncio
async def test_update_agent_finishes_old_span_and_starts_new_one():
    """update_agent() must finish the outgoing span and emit a new span for the incoming agent.

    Convention: update_agent() is the public API equivalent of a handoff. It must mirror
    the handoff path: finish the current agent span, then create and start a new one for
    the incoming agent. Without this, activity after the switch is attributed to the wrong
    agent and no span is emitted for the new agent.
    """
    original = RealtimeAgent(name="original_agent")
    replacement = RealtimeAgent(name="replacement_agent")
    model = _FakeRealtimeModel()
    session = _make_session(original, model)

    with trace("test"):
        async with session:
            await session.update_agent(replacement)

    spans = SPAN_PROCESSOR_TESTING.get_ordered_spans()
    agent_spans = [s for s in spans if isinstance(s.span_data, AgentSpanData)]
    assert len(agent_spans) == 2, (
        f"Expected 2 agent spans (original + replacement), got {len(agent_spans)}"
    )

    names = {s.span_data.name for s in agent_spans}
    assert names == {"original_agent", "replacement_agent"}, (
        f"Expected spans for both agents, got: {names}"
    )

    original_span = next(s for s in agent_spans if s.span_data.name == "original_agent")
    assert original_span.ended_at is not None, (
        "Original agent span must be finished after update_agent()"
    )

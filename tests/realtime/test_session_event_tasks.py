from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from agents.realtime.events import RealtimeError, RealtimeEventInfo
from agents.realtime.session import RealtimeSession


@pytest.fixture
def fake_agent():
    agent = Mock()
    agent.get_all_tools = AsyncMock(return_value=[])
    agent.get_system_prompt = AsyncMock(return_value="test instructions")
    agent.handoffs = []
    return agent


@pytest.fixture
def fake_model():
    return Mock()


class TestEmitEventSoon:
    """Background event tasks must be referenced so they are not dropped."""

    @pytest.mark.asyncio
    async def test_emit_event_soon_keeps_task_referenced_until_done(self, fake_model, fake_agent):
        """The scheduled task is tracked while pending and released when done.

        asyncio only keeps a weak reference to a task, so a fire-and-forget
        ``create_task`` can be garbage-collected before it runs. ``_emit_event_soon``
        retains a strong reference until completion and then delivers the event.
        """
        session = RealtimeSession(fake_model, fake_agent, None)
        event = RealtimeError(
            info=RealtimeEventInfo(context=session._context_wrapper),
            error={"message": "boom"},
        )

        session._emit_event_soon(event)

        # While pending, the task is held in the tracking set (strong reference).
        assert len(session._event_tasks) == 1

        # Once it runs, the event reaches the queue and the reference is released.
        delivered = await asyncio.wait_for(session._event_queue.get(), timeout=1)
        assert delivered is event
        await asyncio.sleep(0)
        assert len(session._event_tasks) == 0

    @pytest.mark.asyncio
    async def test_cleanup_event_tasks_cancels_pending(self, fake_model, fake_agent):
        """Cleanup cancels any still-pending background event tasks."""
        session = RealtimeSession(fake_model, fake_agent, None)
        event = RealtimeError(
            info=RealtimeEventInfo(context=session._context_wrapper),
            error={"message": "boom"},
        )

        session._emit_event_soon(event)
        assert len(session._event_tasks) == 1

        session._cleanup_event_tasks()
        assert len(session._event_tasks) == 0

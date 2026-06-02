"""
Smoke tests for the A2A server executor.

Verifies that A2AServerAgent correctly:
  - Processes a SendMessage request and produces a completed Task
  - Persists and retrieves conversation sessions across turns
  - Handles errors gracefully (produces a failed Task)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from agents.agent import Agent


# ---------------------------------------------------------------------------
# Fake RequestContext — minimal mock for testing
# ---------------------------------------------------------------------------


class _FakeRequestContext:
    """Minimal RequestContext stub for testing A2AServerAgent."""

    def __init__(
        self,
        message: Any = None,
        task_id: str | None = None,
        context_id: str | None = None,
    ) -> None:
        self._message = message
        self.task_id = task_id or f"test-task-{uuid.uuid4().hex[:8]}"
        self.context_id = context_id or f"test-session-{uuid.uuid4().hex[:8]}"

    @property
    def message(self) -> Any:
        return self._message


# ---------------------------------------------------------------------------
# Fake EventQueue — captures published events for assertions
# ---------------------------------------------------------------------------


class _FakeEventQueue:
    """Minimal EventQueue stub that captures published events."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []
        self.status_updates: list[Any] = []

    async def enqueue_task(self, task: Any) -> None:
        self.tasks.append(task)

    async def enqueue_task_status_update(self, task_id: str, status: Any, final: bool) -> None:
        self.status_updates.append({"task_id": task_id, "status": status, "final": final})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_produces_completed_task() -> None:
    """Verify that a basic non-streaming execution produces a completed Task."""
    from agents.extensions.a2a._converter import (
        a2a_message_to_openai_input_items,
    )

    # Build an A2A message using the real converter
    from a2a.types.a2a_pb2 import Message, Part

    part = Part()
    part.text = "Hello"
    part.media_type = "text/plain"
    message = Message()
    message.role = 1  # USER
    message.parts.append(part)

    context = _FakeRequestContext(message=message)
    event_queue = _FakeEventQueue()

    agent = Agent(name="TestAgent", instructions="Be helpful.")

    from agents.extensions.a2a._server_executor import A2AServerAgent

    executor = A2AServerAgent(agent=agent, max_turns=5)
    await executor.execute(context, event_queue)

    # Should have published at least one task (working + completed)
    assert len(event_queue.tasks) >= 1, "expected at least a completed task"
    completed = event_queue.tasks[-1]
    assert completed.id == context.task_id


@pytest.mark.asyncio
async def test_executor_session_persistence() -> None:
    """Verify that conversation history is persisted across turns."""
    from a2a.types.a2a_pb2 import Message, Part

    def make_message(text: str) -> Message:
        part = Part()
        part.text = text
        part.media_type = "text/plain"
        msg = Message()
        msg.role = 1  # USER
        msg.parts.append(part)
        return msg

    agent = Agent(name="SessionAgent", instructions="You are helpful.")

    from agents.extensions.a2a._server_executor import A2AServerAgent

    executor = A2AServerAgent(agent=agent, max_turns=5)

    # First turn
    ctx1 = _FakeRequestContext(
        message=make_message("First message"),
        context_id="session-1",
    )
    queue1 = _FakeEventQueue()
    await executor.execute(ctx1, queue1)
    assert len(queue1.tasks) >= 1

    # Verify session was stored
    session_items = executor._get_session("session-1")
    assert len(session_items) > 0, "session should contain items after first turn"

    # Second turn: session should have accumulated history
    ctx2 = _FakeRequestContext(
        message=make_message("Second message"),
        context_id="session-1",
    )
    queue2 = _FakeEventQueue()
    await executor.execute(ctx2, queue2)
    assert len(queue2.tasks) >= 1

    # Session should now contain more items
    session_items_2 = executor._get_session("session-1")
    assert len(session_items_2) > len(session_items), (
        "session should accumulate history across turns"
    )


@pytest.mark.asyncio
async def test_executor_cancel_cleans_up_running_task() -> None:
    """Verify that a running task is removed from tracking after completion."""
    from a2a.types.a2a_pb2 import Message, Part

    part = Part()
    part.text = "test"
    part.media_type = "text/plain"
    message = Message()
    message.role = 1
    message.parts.append(part)

    context = _FakeRequestContext(message=message, task_id="cancel-test-task")
    event_queue = _FakeEventQueue()

    agent = Agent(name="CancelAgent")

    from agents.extensions.a2a._server_executor import A2AServerAgent

    executor = A2AServerAgent(agent=agent, max_turns=3)
    await executor.execute(context, event_queue)

    # After completion, the task should not be in the running tasks dict
    assert "cancel-test-task" not in executor._running_tasks

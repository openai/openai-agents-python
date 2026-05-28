"""
Regression tests for openai/openai-agents-python#3334.

RealtimeSession._cleanup() must await cancelled background tasks so that
their finally-blocks fully run before _cleanup() returns. The old code
called task.cancel() and then immediately cleared the tracking sets without
ever awaiting the cancelled coroutines.

Note on task lifecycle: asyncio tasks need at least one event-loop iteration
to start running. In real usage, background tasks run for some time before
_cleanup() is called, so they have already reached their first await. The
tests below use `await asyncio.sleep(0)` to simulate that running state
before triggering cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.realtime.agent import RealtimeAgent
from agents.realtime.session import RealtimeSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_model() -> Any:
    model = MagicMock()
    model.add_listener = MagicMock()
    model.remove_listener = MagicMock()
    model.close = AsyncMock()
    return model


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_awaits_guardrail_task_finally_block() -> None:
    """_cleanup() must not return until a cancelled guardrail task's finally block runs."""
    model = _make_fake_model()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)

    finally_ran = asyncio.Event()

    async def long_guardrail() -> None:
        try:
            await asyncio.Event().wait()  # block until cancelled
        finally:
            finally_ran.set()

    task = asyncio.create_task(long_guardrail())
    session._guardrail_tasks.add(task)

    # Let the task reach its first await before we trigger cleanup
    await asyncio.sleep(0)

    await session._cleanup()

    assert finally_ran.is_set(), (
        "_cleanup() returned before the cancelled guardrail task's finally-block ran"
    )


@pytest.mark.asyncio
async def test_cleanup_awaits_tool_call_task_finally_block() -> None:
    """_cleanup() must not return until a cancelled tool-call task's finally block runs."""
    model = _make_fake_model()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)

    finally_ran = asyncio.Event()

    async def long_tool_call() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            finally_ran.set()

    task = asyncio.create_task(long_tool_call())
    session._tool_call_tasks.add(task)

    # Let the task reach its first await
    await asyncio.sleep(0)

    await session._cleanup()

    assert finally_ran.is_set(), (
        "_cleanup() returned before the cancelled tool-call task's finally-block ran"
    )


@pytest.mark.asyncio
async def test_cleanup_awaits_both_task_types_before_model_close() -> None:
    """Both guardrail and tool-call finally-blocks must complete before model.close()."""
    model = _make_fake_model()
    order: list[str] = []

    async def close_side_effect() -> None:
        order.append("model_closed")

    model.close.side_effect = close_side_effect

    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)

    async def make_task(label: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            order.append(label)

    gtask = asyncio.create_task(make_task("guardrail_finally"))
    ttask = asyncio.create_task(make_task("tool_call_finally"))
    session._guardrail_tasks.add(gtask)
    session._tool_call_tasks.add(ttask)

    # Let both tasks reach their first await
    await asyncio.sleep(0)

    await session._cleanup()

    assert "guardrail_finally" in order
    assert "tool_call_finally" in order
    # Both finally-blocks must appear before model.close()
    assert order.index("guardrail_finally") < order.index("model_closed"), (
        "guardrail finally-block ran after model.close()"
    )
    assert order.index("tool_call_finally") < order.index("model_closed"), (
        "tool-call finally-block ran after model.close()"
    )


@pytest.mark.asyncio
async def test_cleanup_idempotent_when_no_pending_tasks() -> None:
    """_cleanup() must succeed silently when there are no background tasks."""
    model = _make_fake_model()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)

    await session._cleanup()  # must not raise
    assert session._closed

"""Normal closure is observable without delaying or draining failed media."""

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeEvent, RealtimeSession
from agents import Session
from agents.realtime import RealtimeLifecycle, RealtimeText
from tests._asyncio import timeout


def owner_for() -> RealtimeSession:
    session = cast(
        Session[Any, Any],
        SimpleNamespace(_check_callback_operation=lambda _: None, _realtime=None),
    )
    owner = RealtimeSession(session, config=RealtimeConfig())
    session._realtime = owner
    owner._live = True
    return owner


async def test_requested_close_yields_one_closed_then_ends() -> None:
    owner = owner_for()
    async with timeout(5):
        await owner.aclose()
        # The native notification arrives after local retirement. Repeated
        # notification and idempotent close must not duplicate public closure.
        closed = {
            "id": owner._generation,
            "msg": {"type": "realtime_conversation_closed"},
        }
        assert owner._ingest(closed)
        assert owner._ingest(closed)
        await owner.aclose()
        events = cast(AsyncGenerator[RealtimeEvent, None], owner.events())
        try:
            assert await anext(events) == RealtimeLifecycle("closed")
            with pytest.raises(StopAsyncIteration):
                await anext(events)
        finally:
            await events.aclose()


async def test_unexpected_close_discards_buffered_media_and_raises_original_error() -> None:
    owner = owner_for()
    owner._emit(RealtimeText("assistant", "stale buffered media", False))
    assert owner._queue_bytes > 0
    assert owner._ingest(
        {
            "id": owner._generation,
            "msg": {"type": "realtime_conversation_closed", "reason": "peer disconnected"},
        }
    )
    error = owner._terminal.result()
    assert isinstance(error, RuntimeError)
    assert "peer disconnected" in str(error)
    assert owner._queue.empty()
    assert owner._queue_bytes == 0
    async with timeout(5):
        events = cast(AsyncGenerator[RealtimeEvent, None], owner.events())
        try:
            with pytest.raises(RuntimeError) as failure:
                await anext(events)
            assert failure.value is error
        finally:
            await events.aclose()
            await owner.aclose()


async def test_overflow_keeps_immediate_error_and_discards_buffered_media() -> None:
    owner = owner_for()
    owner._queue = asyncio.Queue(1)
    owner._emit(RealtimeText("assistant", "first buffered frame", False))
    owner._emit(RealtimeText("assistant", "overflowing frame", False))
    error = owner._terminal.result()
    assert isinstance(error, RuntimeError)
    assert "overflow" in str(error)
    assert owner._queue.empty()
    assert owner._queue_bytes == 0
    async with timeout(5):
        events = cast(AsyncGenerator[RealtimeEvent, None], owner.events())
        try:
            with pytest.raises(RuntimeError) as failure:
                await anext(events)
            assert failure.value is error
        finally:
            await events.aclose()
            await owner.aclose()

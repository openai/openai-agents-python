from __future__ import annotations

from typing import Any

import pytest

from agents.items import TResponseInputItem
from agents.memory import (
    OpenAIResponsesCompactionSession,
    SessionSettings,
    slice_items_by_turn,
)
from agents.memory.session_settings import resolve_session_turn_limit
from tests.utils.openai_responses_session_helpers import (
    ALL_TURNS,
    TURN_THREE,
    TURN_TWO,
    build_session,
)


def test_resolve_session_turn_limit_explicit_turn_limit_wins() -> None:
    assert resolve_session_turn_limit(None, 2, SessionSettings(turn_limit=5)) == 2
    assert resolve_session_turn_limit(3, 2, SessionSettings(turn_limit=5)) == 2


def test_resolve_session_turn_limit_explicit_limit_suppresses_settings_turn_limit() -> None:
    # An explicit item limit is a deliberate window request and must not be reshaped
    # by a configured turn limit.
    assert resolve_session_turn_limit(3, None, SessionSettings(turn_limit=5)) is None


def test_resolve_session_turn_limit_settings_turn_limit_applies_when_only_settings() -> None:
    assert resolve_session_turn_limit(None, None, SessionSettings(turn_limit=5)) == 5


def test_resolve_session_turn_limit_no_args() -> None:
    assert resolve_session_turn_limit(None, None, None) is None


def test_slice_items_by_turn_returns_latest_turns() -> None:
    assert slice_items_by_turn(ALL_TURNS, 1) == TURN_THREE
    assert slice_items_by_turn(ALL_TURNS, 2) == [*TURN_TWO, *TURN_THREE]
    assert slice_items_by_turn(ALL_TURNS, 3) == ALL_TURNS


def test_slice_items_by_turn_non_positive_returns_empty() -> None:
    assert slice_items_by_turn(ALL_TURNS, 0) == []
    assert slice_items_by_turn(ALL_TURNS, -1) == []


@pytest.mark.asyncio
async def test_get_items_turn_limit(tmp_path: Any) -> None:
    session = await build_session(tmp_path)
    assert await session.get_items(turn_limit=1) == TURN_THREE
    assert await session.get_items(turn_limit=2) == [*TURN_TWO, *TURN_THREE]
    assert await session.get_items(turn_limit=3) == ALL_TURNS


@pytest.mark.asyncio
async def test_get_items_limit_returns_latest_items(tmp_path: Any) -> None:
    session = await build_session(tmp_path)
    assert await session.get_items(limit=2) == TURN_THREE
    assert await session.get_items(limit=6) == ALL_TURNS


@pytest.mark.asyncio
async def test_get_items_explicit_limit_suppresses_settings_turn_limit(tmp_path: Any) -> None:
    session = await build_session(tmp_path)
    session.session_settings = SessionSettings(turn_limit=3)
    assert await session.get_items(limit=2) == TURN_THREE


@pytest.mark.asyncio
async def test_get_items_explicit_turn_limit_overrides_settings_limit(tmp_path: Any) -> None:
    session = await build_session(tmp_path)
    session.session_settings = SessionSettings(limit=1)
    assert await session.get_items(turn_limit=2) == [
        *TURN_TWO,
        *TURN_THREE,
    ]


@pytest.mark.asyncio
async def test_get_items_settings_only_turn_limit(tmp_path: Any) -> None:
    session = await build_session(tmp_path)
    session.session_settings = SessionSettings(turn_limit=2)
    assert await session.get_items() == [
        *TURN_TWO,
        *TURN_THREE,
    ]


@pytest.mark.asyncio
async def test_get_items_non_positive_turn_limit_returns_empty(tmp_path: Any) -> None:
    session = await build_session(tmp_path)
    assert await session.get_items(turn_limit=0) == []
    assert await session.get_items(turn_limit=-1) == []


class _OldSignatureSession:
    """A third-party session using only the released ``get_items(limit)`` signature."""

    session_id = "old-signature"
    session_settings: SessionSettings | None = None

    def __init__(self, history: list[TResponseInputItem]) -> None:
        self._items = list(history)

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is None:
            return list(self._items)
        return self._items[-limit:]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items = []


@pytest.mark.asyncio
async def test_compaction_wrapper_preserves_old_signature_sessions() -> None:
    """Regression: the compaction wrapper must not forward ``turn_limit`` to sessions
    that only implement the released ``get_items(limit)`` signature."""
    underlying = _OldSignatureSession(ALL_TURNS)
    wrapper = OpenAIResponsesCompactionSession(session_id="wrapped", underlying_session=underlying)

    # Bare retrieval and item-window retrieval must work on old-signature sessions.
    assert await wrapper.get_items() == ALL_TURNS
    assert await wrapper.get_items(limit=2) == TURN_THREE


@pytest.mark.asyncio
async def test_compaction_wrapper_forwards_turn_limit_to_new_signature_sessions(
    tmp_path: Any,
) -> None:
    underlying = await build_session(tmp_path)
    wrapper = OpenAIResponsesCompactionSession(
        session_id="wrapped-new", underlying_session=underlying
    )
    assert await wrapper.get_items(turn_limit=1) == TURN_THREE
    assert await wrapper.get_items(limit=2, turn_limit=1) == TURN_THREE


def test_slice_items_by_turn_is_public_api() -> None:
    import agents.memory as memory_module
    from agents.memory import slice_items_by_turn as exported

    assert "slice_items_by_turn" in memory_module.__all__
    from agents.memory.session import slice_items_by_turn as source

    assert exported is source

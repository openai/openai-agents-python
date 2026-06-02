from __future__ import annotations

from typing import cast

import pytest

from agents.items import TResponseInputItem
from tests.utils.simple_session import CountingSession, IdStrippingSession, SimpleListSession


@pytest.mark.asyncio
async def test_simple_list_session_preserves_history_and_saved_items() -> None:
    history: list[TResponseInputItem] = [
        cast(TResponseInputItem, {"id": "msg1", "content": "hi", "role": "user"}),
        cast(TResponseInputItem, {"id": "msg2", "content": "hello", "role": "assistant"}),
    ]
    session = SimpleListSession(history=history)

    items = await session.get_items()
    # get_items should return a copy, not the original list.
    assert items == history
    assert items is not history
    # saved_items should mirror the stored list.
    assert session.saved_items == history


@pytest.mark.asyncio
async def test_counting_session_tracks_pop_calls() -> None:
    session = CountingSession(
        history=[cast(TResponseInputItem, {"id": "x", "content": "hi", "role": "user"})]
    )

    assert session.pop_calls == 0
    await session.pop_item()
    assert session.pop_calls == 1
    await session.pop_item()
    assert session.pop_calls == 2


@pytest.mark.asyncio
async def test_id_stripping_session_removes_ids_on_add() -> None:
    session = IdStrippingSession()
    items: list[TResponseInputItem] = [
        cast(TResponseInputItem, {"id": "keep-removed", "content": "hello", "role": "user"}),
        cast(TResponseInputItem, {"content": "no-id", "role": "assistant"}),
    ]

    await session.add_items(items)
    stored = await session.get_items()

    assert all("id" not in item for item in stored if isinstance(item, dict))
    # pop_calls should increment when rewinding.
    await session.pop_item()
    assert session.pop_calls == 1

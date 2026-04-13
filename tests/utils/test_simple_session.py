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
async def test_simple_list_session_get_items_offset() -> None:
    """Test that SimpleListSession.get_items respects the offset parameter."""
    items: list[TResponseInputItem] = [
        cast(TResponseInputItem, {"content": f"msg{i}", "role": "user"}) for i in range(6)
    ]
    session = SimpleListSession(history=items)

    # offset=0 is default — same as no offset
    page0 = await session.get_items(limit=2, offset=0)
    assert [i["content"] for i in page0] == ["msg4", "msg5"]

    # offset=2 skips the 2 most-recent, returns the next 2
    page1 = await session.get_items(limit=2, offset=2)
    assert [i["content"] for i in page1] == ["msg2", "msg3"]

    # offset=4 skips 4 most-recent, returns the 2 oldest
    page2 = await session.get_items(limit=2, offset=4)
    assert [i["content"] for i in page2] == ["msg0", "msg1"]

    # offset without limit returns all except the N most-recent
    without_limit = await session.get_items(offset=2)
    assert [i["content"] for i in without_limit] == ["msg0", "msg1", "msg2", "msg3"]

    # offset >= total returns empty
    empty = await session.get_items(limit=2, offset=10)
    assert empty == []


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

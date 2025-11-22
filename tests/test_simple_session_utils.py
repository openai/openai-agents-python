from __future__ import annotations

from typing import Any, cast

import pytest

from agents.items import ItemHelpers, TResponseInputItem
from tests.utils.simple_session import SimpleListSession


@pytest.mark.asyncio
async def test_simple_session_add_pop_clear():
    session = SimpleListSession(session_id="session-1")
    first_batch = ItemHelpers.input_to_new_input_list("hi")
    await session.add_items(first_batch)

    items = await session.get_items()
    assert len(items) == 1

    popped = await session.pop_item()
    assert isinstance(popped, dict)
    popped_dict = cast(dict[str, Any], popped)
    assert popped_dict["content"] == "hi"
    assert await session.pop_item() is None

    second_batch = ItemHelpers.input_to_new_input_list("again")
    third_batch = ItemHelpers.input_to_new_input_list("ok")
    await session.add_items(second_batch + third_batch)
    await session.clear_session()
    assert await session.get_items() == []


@pytest.mark.asyncio
async def test_simple_session_get_items_limit():
    session = SimpleListSession()
    first = ItemHelpers.input_to_new_input_list("first")
    second = ItemHelpers.input_to_new_input_list("second")
    entries: list[TResponseInputItem] = first + second
    await session.add_items(entries)

    assert await session.get_items(limit=1) == entries[-1:]
    assert await session.get_items(limit=0) == []

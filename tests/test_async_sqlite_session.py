"""Tests for AsyncSQLiteSession functionality."""

import tempfile
from pathlib import Path

import pytest

from agents import AsyncSQLiteSession, TResponseInputItem


@pytest.mark.asyncio
async def test_async_sqlite_session_basic_flow():
    """Test AsyncSQLiteSession add/get/clear behavior."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_basic.db"
        session = AsyncSQLiteSession("async_basic", db_path)

        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        await session.add_items(items)
        retrieved = await session.get_items()
        assert retrieved == items

        await session.clear_session()
        assert await session.get_items() == []

        await session.close()


@pytest.mark.asyncio
async def test_async_sqlite_session_pop_item():
    """Test AsyncSQLiteSession pop_item behavior."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_pop.db"
        session = AsyncSQLiteSession("async_pop", db_path)

        assert await session.pop_item() is None

        items: list[TResponseInputItem] = [
            {"role": "user", "content": "One"},
            {"role": "assistant", "content": "Two"},
        ]
        await session.add_items(items)

        popped = await session.pop_item()
        assert popped == items[-1]
        assert await session.get_items() == items[:-1]

        await session.close()


@pytest.mark.asyncio
async def test_async_sqlite_session_get_items_limit():
    """Test AsyncSQLiteSession get_items limit handling."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_limit.db"
        session = AsyncSQLiteSession("async_limit", db_path)

        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Message 2"},
        ]
        await session.add_items(items)

        latest = await session.get_items(limit=2)
        assert latest == items[-2:]

        none = await session.get_items(limit=0)
        assert none == []

        await session.close()


@pytest.mark.asyncio
async def test_async_sqlite_session_unicode_content():
    """Test AsyncSQLiteSession stores unicode content."""
    session = AsyncSQLiteSession("async_unicode")
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "😊👍"},
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert retrieved == items

    await session.close()

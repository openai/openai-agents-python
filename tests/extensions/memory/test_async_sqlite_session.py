"""Tests for AsyncSQLiteSession functionality."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("aiosqlite")  # Skip tests if aiosqlite is not installed

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory import AsyncSQLiteSession
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


def _item_ids(items: Sequence[TResponseInputItem]) -> list[str]:
    result: list[str] = []
    for item in items:
        item_dict = cast(dict[str, Any], item)
        result.append(cast(str, item_dict["id"]))
    return result


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


async def test_async_sqlite_session_unicode_content():
    """Test AsyncSQLiteSession stores unicode content."""
    session = AsyncSQLiteSession("async_unicode")
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "こんにちは"},
        {"role": "assistant", "content": "Привет"},
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert retrieved == items

    await session.close()


async def test_async_sqlite_session_runner_integration(agent: Agent):
    """Test that AsyncSQLiteSession works correctly with the agent Runner."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_runner_integration.db"
        session = AsyncSQLiteSession("runner_integration_test", db_path)

        assert isinstance(agent.model, FakeModel)

        agent.model.set_next_output([get_text_message("San Francisco")])
        result1 = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        assert result1.final_output == "San Francisco"

        agent.model.set_next_output([get_text_message("California")])
        result2 = await Runner.run(agent, "What state is it in?", session=session)
        assert result2.final_output == "California"

        last_input = agent.model.last_turn_args["input"]
        assert isinstance(last_input, list)
        assert len(last_input) > 1
        assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)

        await session.close()


async def test_async_sqlite_session_session_isolation(agent: Agent):
    """Test that different session IDs result in isolated conversation histories."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_isolation.db"
        session1 = AsyncSQLiteSession("session_1", db_path)
        session2 = AsyncSQLiteSession("session_2", db_path)

        assert isinstance(agent.model, FakeModel)
        agent.model.set_next_output([get_text_message("I like cats.")])
        await Runner.run(agent, "I like cats.", session=session1)

        agent.model.set_next_output([get_text_message("I like dogs.")])
        await Runner.run(agent, "I like dogs.", session=session2)

        agent.model.set_next_output([get_text_message("You said you like cats.")])
        result = await Runner.run(agent, "What animal did I say I like?", session=session1)
        assert "cats" in result.final_output.lower()
        assert "dogs" not in result.final_output.lower()

        await session1.close()
        await session2.close()


async def test_async_sqlite_session_add_empty_items_list():
    """Test that adding an empty list of items is a no-op."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_add_empty.db"
        session = AsyncSQLiteSession("add_empty_test", db_path)

        assert await session.get_items() == []
        await session.add_items([])
        assert await session.get_items() == []

        await session.close()


async def test_async_sqlite_session_pop_from_empty_session():
    """Test that pop_item returns None on an empty session."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_pop_empty.db"
        session = AsyncSQLiteSession("empty_session", db_path)

        popped = await session.pop_item()
        assert popped is None

        await session.close()


async def test_async_sqlite_session_get_items_with_limit_more_than_available():
    """Test limit behavior when requesting more items than exist."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_limit_more.db"
        session = AsyncSQLiteSession("limit_more_test", db_path)

        items: list[TResponseInputItem] = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "3"},
            {"role": "assistant", "content": "4"},
        ]
        await session.add_items(items)

        retrieved = await session.get_items(limit=10)
        assert retrieved == items

        await session.close()


async def test_async_sqlite_session_get_items_same_timestamp_consistent_order():
    """Test that items with identical timestamps keep insertion order."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_same_timestamp.db"
        session = AsyncSQLiteSession("same_timestamp_test", db_path)

        older_item = cast(
            TResponseInputItem, {"id": "older_same_ts", "role": "user", "content": "old"}
        )
        reasoning_item = cast(TResponseInputItem, {"id": "rs_same_ts", "type": "reasoning"})
        message_item = cast(
            TResponseInputItem,
            {"id": "msg_same_ts", "type": "message", "role": "assistant", "content": []},
        )

        await session.add_items([older_item])
        await session.add_items([reasoning_item, message_item])

        conn = await session._get_connection()
        cursor = await conn.execute(
            f"SELECT id, message_data FROM {session.messages_table} WHERE session_id = ?",
            (session.session_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        id_map: dict[str, int] = {
            cast(str, json.loads(message_json)["id"]): cast(int, row_id)
            for row_id, message_json in rows
        }

        shared = datetime(2025, 10, 15, 17, 26, 39, 132483)
        shared_str = shared.strftime("%Y-%m-%d %H:%M:%S.%f")
        await conn.execute(
            f"""
            UPDATE {session.messages_table}
            SET created_at = ?
            WHERE id IN (?, ?, ?)
            """,
            (
                shared_str,
                id_map["older_same_ts"],
                id_map["rs_same_ts"],
                id_map["msg_same_ts"],
            ),
        )
        await conn.commit()

        retrieved = await session.get_items()
        assert _item_ids(retrieved) == ["older_same_ts", "rs_same_ts", "msg_same_ts"]

        latest_two = await session.get_items(limit=2)
        assert _item_ids(latest_two) == ["rs_same_ts", "msg_same_ts"]

        await session.close()


async def test_async_sqlite_session_pop_item_same_timestamp_returns_latest():
    """Test that pop_item returns the newest item when timestamps tie."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_same_timestamp_pop.db"
        session = AsyncSQLiteSession("same_timestamp_pop_test", db_path)

        reasoning_item = cast(TResponseInputItem, {"id": "rs_pop_same_ts", "type": "reasoning"})
        message_item = cast(
            TResponseInputItem,
            {"id": "msg_pop_same_ts", "type": "message", "role": "assistant", "content": []},
        )

        await session.add_items([reasoning_item, message_item])

        conn = await session._get_connection()
        shared = datetime(2025, 10, 15, 17, 26, 39, 132483)
        shared_str = shared.strftime("%Y-%m-%d %H:%M:%S.%f")
        await conn.execute(
            f"UPDATE {session.messages_table} SET created_at = ? WHERE session_id = ?",
            (shared_str, session.session_id),
        )
        await conn.commit()

        popped = await session.pop_item()
        assert popped is not None
        assert cast(dict[str, Any], popped)["id"] == "msg_pop_same_ts"

        remaining = await session.get_items()
        assert _item_ids(remaining) == ["rs_pop_same_ts"]

        await session.close()

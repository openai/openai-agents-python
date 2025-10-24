from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from openai.types.responses.response_output_message_param import ResponseOutputMessageParam
from openai.types.responses.response_output_text_param import ResponseOutputTextParam
from openai.types.responses.response_reasoning_item_param import (
    ResponseReasoningItemParam,
    Summary,
)
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql import Select

pytest.importorskip("sqlalchemy")  # Skip tests if SQLAlchemy is not installed

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

# Use in-memory SQLite for tests
DB_URL = "sqlite+aiosqlite:///:memory:"


def _make_message_item(item_id: str, text_value: str) -> TResponseInputItem:
    content: ResponseOutputTextParam = {
        "type": "output_text",
        "text": text_value,
        "annotations": [],
    }
    message: ResponseOutputMessageParam = {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [content],
    }
    return cast(TResponseInputItem, message)


def _make_reasoning_item(item_id: str, summary_text: str) -> TResponseInputItem:
    summary: Summary = {"type": "summary_text", "text": summary_text}
    reasoning: ResponseReasoningItemParam = {
        "id": item_id,
        "type": "reasoning",
        "summary": [summary],
    }
    return cast(TResponseInputItem, reasoning)


def _item_ids(items: Sequence[TResponseInputItem]) -> list[str]:
    result: list[str] = []
    for item in items:
        item_dict = cast(dict[str, Any], item)
        result.append(cast(str, item_dict["id"]))
    return result


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


async def test_sqlalchemy_session_direct_ops(agent: Agent):
    """Test direct database operations of SQLAlchemySession."""
    session_id = "direct_ops_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    # 1. Add items
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    # 2. Get items and verify
    retrieved = await session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"

    # 3. Pop item
    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "Hi there!"
    retrieved_after_pop = await session.get_items()
    assert len(retrieved_after_pop) == 1
    assert retrieved_after_pop[0].get("content") == "Hello"

    # 4. Clear session
    await session.clear_session()
    retrieved_after_clear = await session.get_items()
    assert len(retrieved_after_clear) == 0


async def test_runner_integration(agent: Agent):
    """Test that SQLAlchemySession works correctly with the agent Runner."""
    session_id = "runner_integration_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    # First turn
    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("San Francisco")])
    result1 = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    assert result1.final_output == "San Francisco"

    # Second turn
    agent.model.set_next_output([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"

    # Verify history was passed to the model on the second turn
    last_input = agent.model.last_turn_args["input"]
    assert len(last_input) > 1
    assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)


async def test_session_isolation(agent: Agent):
    """Test that different session IDs result in isolated conversation histories."""
    session_id_1 = "session_1"
    session1 = SQLAlchemySession.from_url(session_id_1, url=DB_URL, create_tables=True)

    session_id_2 = "session_2"
    session2 = SQLAlchemySession.from_url(session_id_2, url=DB_URL, create_tables=True)

    # Interact with session 1
    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("I like cats.")])
    await Runner.run(agent, "I like cats.", session=session1)

    # Interact with session 2
    agent.model.set_next_output([get_text_message("I like dogs.")])
    await Runner.run(agent, "I like dogs.", session=session2)

    # Go back to session 1 and check its memory
    agent.model.set_next_output([get_text_message("You said you like cats.")])
    result = await Runner.run(agent, "What animal did I say I like?", session=session1)
    assert "cats" in result.final_output.lower()
    assert "dogs" not in result.final_output.lower()


async def test_get_items_with_limit(agent: Agent):
    """Test the limit parameter in get_items."""
    session_id = "limit_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
        {"role": "assistant", "content": "4"},
    ]
    await session.add_items(items)

    # Get last 2 items
    latest_2 = await session.get_items(limit=2)
    assert len(latest_2) == 2
    assert latest_2[0].get("content") == "3"
    assert latest_2[1].get("content") == "4"

    # Get all items
    all_items = await session.get_items()
    assert len(all_items) == 4

    # Get more than available
    more_than_all = await session.get_items(limit=10)
    assert len(more_than_all) == 4


async def test_pop_from_empty_session():
    """Test that pop_item returns None on an empty session."""
    session = SQLAlchemySession.from_url("empty_session", url=DB_URL, create_tables=True)
    popped = await session.pop_item()
    assert popped is None


async def test_add_empty_items_list():
    """Test that adding an empty list of items is a no-op."""
    session_id = "add_empty_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    initial_items = await session.get_items()
    assert len(initial_items) == 0

    await session.add_items([])

    items_after_add = await session.get_items()
    assert len(items_after_add) == 0


async def test_get_items_same_timestamp_consistent_order():
    """Test that items with identical timestamps keep insertion order."""
    session_id = "same_timestamp_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    older_item = _make_message_item("older_same_ts", "old")
    reasoning_item = _make_reasoning_item("rs_same_ts", "...")
    message_item = _make_message_item("msg_same_ts", "...")
    await session.add_items([older_item])
    await session.add_items([reasoning_item, message_item])

    async with session._session_factory() as sess:
        rows = await sess.execute(
            select(session._messages.c.id, session._messages.c.message_data).where(
                session._messages.c.session_id == session.session_id
            )
        )
        id_map = {
            json.loads(message_json)["id"]: row_id for row_id, message_json in rows.fetchall()
        }
        shared = datetime(2025, 10, 15, 17, 26, 39, 132483)
        older = shared - timedelta(milliseconds=1)
        await sess.execute(
            update(session._messages)
            .where(
                session._messages.c.id.in_(
                    [
                        id_map["rs_same_ts"],
                        id_map["msg_same_ts"],
                    ]
                )
            )
            .values(created_at=shared)
        )
        await sess.execute(
            update(session._messages)
            .where(session._messages.c.id == id_map["older_same_ts"])
            .values(created_at=older)
        )
        await sess.commit()

    real_factory = session._session_factory

    class FakeResult:
        def __init__(self, rows: Iterable[Any]):
            self._rows = list(rows)

        def all(self) -> list[Any]:
            return list(self._rows)

    def needs_shuffle(statement: Any) -> bool:
        if not isinstance(statement, Select):
            return False
        orderings = list(statement._order_by_clause)
        if not orderings:
            return False
        id_asc = session._messages.c.id.asc()
        id_desc = session._messages.c.id.desc()

        def references_id(clause) -> bool:
            try:
                return bool(clause.compare(id_asc) or clause.compare(id_desc))
            except AttributeError:
                return False

        if any(references_id(clause) for clause in orderings):
            return False
        # Only shuffle queries that target the messages table.
        target_tables: set[str] = set()
        for from_clause in statement.get_final_froms():
            name_attr = getattr(from_clause, "name", None)
            if isinstance(name_attr, str):
                target_tables.add(name_attr)
        table_name_obj = getattr(session._messages, "name", "")
        table_name = table_name_obj if isinstance(table_name_obj, str) else ""
        return bool(table_name in target_tables)

    @asynccontextmanager
    async def shuffled_session():
        async with real_factory() as inner:
            original_execute = inner.execute

            async def execute_with_shuffle(statement: Any, *args: Any, **kwargs: Any) -> Any:
                result = await original_execute(statement, *args, **kwargs)
                if needs_shuffle(statement):
                    rows = result.all()
                    shuffled = list(rows)
                    shuffled.reverse()
                    return FakeResult(shuffled)
                return result

            cast(Any, inner).execute = execute_with_shuffle
            try:
                yield inner
            finally:
                cast(Any, inner).execute = original_execute

    session._session_factory = cast(Any, shuffled_session)
    try:
        retrieved = await session.get_items()
        assert _item_ids(retrieved) == ["older_same_ts", "rs_same_ts", "msg_same_ts"]

        latest_two = await session.get_items(limit=2)
        assert _item_ids(latest_two) == ["rs_same_ts", "msg_same_ts"]
    finally:
        session._session_factory = real_factory


async def test_pop_item_same_timestamp_returns_latest():
    """Test that pop_item returns the newest item when timestamps tie."""
    session_id = "same_timestamp_pop_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    reasoning_item = _make_reasoning_item("rs_pop_same_ts", "...")
    message_item = _make_message_item("msg_pop_same_ts", "...")
    await session.add_items([reasoning_item, message_item])

    async with session._session_factory() as sess:
        await sess.execute(
            text(
                "UPDATE agent_messages SET created_at = :created_at WHERE session_id = :session_id"
            ),
            {
                "created_at": "2025-10-15 17:26:39.132483",
                "session_id": session.session_id,
            },
        )
        await sess.commit()

    popped = await session.pop_item()
    assert popped is not None
    assert cast(dict[str, Any], popped)["id"] == "msg_pop_same_ts"

    remaining = await session.get_items()
    assert _item_ids(remaining) == ["rs_pop_same_ts"]


async def test_get_items_orders_by_id_for_ties():
    """Test that get_items adds id ordering to break timestamp ties."""
    session_id = "order_by_id_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    await session.add_items(
        [
            _make_reasoning_item("rs_first", "..."),
            _make_message_item("msg_second", "..."),
        ]
    )

    real_factory = session._session_factory
    recorded: list[Any] = []

    @asynccontextmanager
    async def wrapped_session():
        async with real_factory() as inner:
            original_execute = inner.execute

            async def recording_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                recorded.append(statement)
                return await original_execute(statement, *args, **kwargs)

            cast(Any, inner).execute = recording_execute
            try:
                yield inner
            finally:
                cast(Any, inner).execute = original_execute

    session._session_factory = cast(Any, wrapped_session)
    try:
        retrieved_full = await session.get_items()
        retrieved_limited = await session.get_items(limit=2)
    finally:
        session._session_factory = real_factory

    assert len(recorded) >= 2
    orderings_full = [str(clause) for clause in recorded[0]._order_by_clause]
    assert orderings_full == [
        "agent_messages.created_at ASC",
        "agent_messages.id ASC",
    ]

    orderings_limited = [str(clause) for clause in recorded[1]._order_by_clause]
    assert orderings_limited == [
        "agent_messages.created_at DESC",
        "agent_messages.id DESC",
    ]

    assert _item_ids(retrieved_full) == ["rs_first", "msg_second"]
    assert _item_ids(retrieved_limited) == ["rs_first", "msg_second"]


async def test_engine_property_from_url():
    """Test that the engine property returns the AsyncEngine from from_url."""
    session_id = "engine_property_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    # Verify engine property returns an AsyncEngine instance
    assert isinstance(session.engine, AsyncEngine)

    # Verify we can use the engine for advanced operations
    # For example, check pool status
    assert session.engine.pool is not None

    # Verify we can manually dispose the engine
    await session.engine.dispose()


async def test_engine_property_from_external_engine():
    """Test that the engine property returns the external engine."""
    session_id = "external_engine_test"

    # Create engine externally
    external_engine = create_async_engine(DB_URL)

    # Create session with external engine
    session = SQLAlchemySession(session_id, engine=external_engine, create_tables=True)

    # Verify engine property returns the same engine instance
    assert session.engine is external_engine

    # Verify we can use the engine
    assert isinstance(session.engine, AsyncEngine)

    # Clean up - user is responsible for disposing external engine
    await external_engine.dispose()


async def test_engine_property_is_read_only():
    """Test that the engine property cannot be modified."""
    session_id = "readonly_engine_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    # Verify engine property exists
    assert hasattr(session, "engine")

    # Verify it's a property (read-only, cannot be set)
    # Type ignore needed because mypy correctly detects this is read-only
    with pytest.raises(AttributeError):
        session.engine = create_async_engine(DB_URL)  # type: ignore[misc]

    # Clean up
    await session.engine.dispose()

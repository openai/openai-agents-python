"""Tests for AsyncSQLiteSession functionality."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("aiosqlite")  # Skip tests if aiosqlite is not installed

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory import AsyncSQLiteSession
from agents.memory import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio


def _assert_cancel_message(exc: asyncio.CancelledError, expected: str) -> None:
    """Account for Python 3.10 dropping Task cancellation messages when re-awaited."""
    expected_args = (expected,) if sys.version_info >= (3, 11) else ()
    assert exc.args == expected_args


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


async def test_async_sqlite_session_pop_item_skips_corrupt_most_recent():
    """pop_item skips corrupt newest rows and returns the next valid item."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_pop_corrupt.db"
        session = AsyncSQLiteSession("async_pop_corrupt", db_path)

        valid_item: TResponseInputItem = {"role": "user", "content": "valid"}
        await session.add_items([valid_item])

        conn = await session._get_connection()
        await conn.execute(
            f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
            (session.session_id, "not valid json {{{"),
        )
        await conn.commit()

        assert await session.pop_item() == valid_item
        assert await session.get_items() == []

        await session.close()


async def test_async_sqlite_session_pop_item_returns_none_after_dropping_only_corrupt_rows():
    """pop_item removes corrupt rows and returns None when no valid items remain."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_pop_only_corrupt.db"
        session = AsyncSQLiteSession("async_pop_only_corrupt", db_path)

        conn = await session._get_connection()
        await conn.execute(
            f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
            (session.session_id, "not valid json {{{"),
        )
        await conn.commit()

        assert await session.pop_item() is None
        assert await session.get_items() == []

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


async def test_async_sqlite_session_get_items_limit_skips_corrupt_newest_rows():
    """limit counts valid items, expanding past corrupt newest rows."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_limit_corrupt.db"
        session = AsyncSQLiteSession("async_limit_corrupt", db_path)

        await session.add_items(
            [
                {"role": "user", "content": "valid 0"},
                {"role": "assistant", "content": "valid 1"},
                {"role": "user", "content": "valid 2"},
            ]
        )

        conn = await session._get_connection()
        await conn.execute(
            f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
            (session.session_id, "not valid json {{{"),
        )
        await conn.commit()

        limited = await session.get_items(limit=2)
        assert [item.get("content") for item in limited] == ["valid 1", "valid 2"]

        await session.close()


async def test_async_sqlite_session_session_settings_default():
    """Test that session_settings defaults to empty SessionSettings."""
    session = AsyncSQLiteSession("async_default_settings")

    assert isinstance(session.session_settings, SessionSettings)
    assert session.session_settings.limit is None

    await session.close()


@pytest.mark.parametrize("use_dictionary", [False, True], ids=["class", "dictionary"])
async def test_async_sqlite_session_session_settings_constructor(use_dictionary: bool):
    """Test passing session_settings via constructor."""
    session = AsyncSQLiteSession(
        "async_constructor_settings",
        session_settings={"limit": 5} if use_dictionary else SessionSettings(limit=5),
    )

    assert isinstance(session.session_settings, SessionSettings)
    assert session.session_settings.limit == 5

    await session.close()


async def test_async_sqlite_session_get_items_uses_session_settings_limit():
    """Test that get_items uses session_settings.limit as default."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_settings_limit.db"
        session = AsyncSQLiteSession(
            "async_settings_limit",
            db_path,
            session_settings=SessionSettings(limit=3),
        )

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(5)
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert retrieved == items[-3:]

        await session.close()


async def test_async_sqlite_session_explicit_limit_overrides_session_settings():
    """Test that explicit limit parameter overrides session_settings."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "async_settings_override.db"
        session = AsyncSQLiteSession(
            "async_settings_override",
            db_path,
            session_settings=SessionSettings(limit=5),
        )

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(10)
        ]
        await session.add_items(items)

        retrieved = await session.get_items(limit=2)
        assert retrieved == items[-2:]

        no_items = await session.get_items(limit=0)
        assert no_items == []

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


async def test_async_sqlite_session_closed_operations_raise_runtime_error():
    """Operations on a closed session must fail instead of reopening the database."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "closed_state.db"
        session = AsyncSQLiteSession("closed_state_test", db_path)
        await session.add_items([{"role": "user", "content": "before close"}])
        await session.close()

        with pytest.raises(RuntimeError, match="AsyncSQLiteSession is closed"):
            await session.get_items()

        with pytest.raises(RuntimeError, match="AsyncSQLiteSession is closed"):
            await session.add_items([{"role": "user", "content": "after close"}])

        with pytest.raises(RuntimeError, match="AsyncSQLiteSession is closed"):
            await session.pop_item()

        with pytest.raises(RuntimeError, match="AsyncSQLiteSession is closed"):
            await session.clear_session()


async def test_async_sqlite_session_closed_rejects_empty_add_items():
    """add_items([]) must not bypass the closed check through the empty-list fast path."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "closed_empty_add.db"
        session = AsyncSQLiteSession("closed_empty_add_test", db_path)
        await session.close()

        with pytest.raises(RuntimeError, match="AsyncSQLiteSession is closed"):
            await session.add_items([])


async def test_async_sqlite_session_close_before_use_is_terminal():
    """close() before the connection is opened must still make the session terminal."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "close_before_use.db"
        session = AsyncSQLiteSession("close_before_use_test", db_path)
        await session.close()

        with pytest.raises(RuntimeError, match="AsyncSQLiteSession is closed"):
            await session.get_items()


async def test_async_sqlite_session_close_is_idempotent():
    """Repeated and concurrent close() calls must remain safe no-ops."""
    import asyncio

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "close_idempotent.db"
        session = AsyncSQLiteSession("close_idempotent_test", db_path)
        await session.add_items([{"role": "user", "content": "before close"}])

        await asyncio.gather(session.close(), session.close())
        await session.close()

        with pytest.raises(RuntimeError, match="AsyncSQLiteSession is closed"):
            await session.get_items()


async def test_cancelled_close_finishes_cleanup_and_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
):
    """Repeated cancellation must propagate after the owned connection closes."""
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    session = AsyncSQLiteSession("cancelled_close")
    conn: Any = None
    real_close: Any = None
    close_task: asyncio.Task[None] | None = None
    try:
        conn = await session._get_connection()
        real_close = conn.close

        async def controlled_close() -> None:
            close_started.set()
            await allow_close.wait()
            await real_close()

        monkeypatch.setattr(conn, "close", controlled_close)
        close_task = asyncio.create_task(session.close())
        try:
            await close_started.wait()
            close_task.cancel()
            await asyncio.sleep(0)
            close_task.cancel()
            await asyncio.sleep(0)
            allow_close.set()
            with pytest.raises(asyncio.CancelledError):
                await close_task
        finally:
            allow_close.set()
            if not close_task.done():
                close_task.cancel()
                await asyncio.gather(close_task, return_exceptions=True)

        assert session._closed is True
        assert session._connection is None
        assert session._quarantined_connections == set()
        assert conn._running is False
    finally:
        allow_close.set()
        if close_task is not None and not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        if conn is not None and real_close is not None:
            monkeypatch.setattr(conn, "close", real_close)
        try:
            await session.close()
        finally:
            if conn is not None and real_close is not None and conn._running:
                await real_close()


@pytest.mark.parametrize("operation", ["add", "pop", "clear"])
async def test_post_commit_cancellation_propagates_after_known_mutation_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    """Cancellation after async commit must propagate without inviting a retry."""
    db_path = tmp_path / f"async_post_commit_{operation}.db"
    session = AsyncSQLiteSession(f"async_post_commit_{operation}", db_path)
    item: TResponseInputItem = {"role": "user", "content": "once"}
    try:
        if operation != "add":
            await session.add_items([item])

        conn = await session._get_connection()
        real_commit = conn.commit
        commit_finished = asyncio.Event()
        allow_return = asyncio.Event()
        pause_commit = True

        async def controlled_commit() -> None:
            nonlocal pause_commit
            await real_commit()
            if pause_commit:
                pause_commit = False
                commit_finished.set()
                await allow_return.wait()

        monkeypatch.setattr(conn, "commit", controlled_commit)
        if operation == "add":
            mutation: asyncio.Task[Any] = asyncio.create_task(session.add_items([item]))
        elif operation == "pop":
            mutation = asyncio.create_task(session.pop_item())
        else:
            mutation = asyncio.create_task(session.clear_session())

        try:
            await commit_finished.wait()
            mutation.cancel()
            await asyncio.sleep(0)
            mutation.cancel()
            await asyncio.sleep(0)
            allow_return.set()
            with pytest.raises(asyncio.CancelledError):
                await mutation
        finally:
            allow_return.set()
            if not mutation.done():
                mutation.cancel()
                await asyncio.gather(mutation, return_exceptions=True)

        if operation == "add":
            assert await session.get_items() == [item]
        elif operation == "pop":
            assert await session.get_items() == []
        else:
            assert await session.get_items() == []
        assert mutation.cancelled()
    finally:
        await session.close()


def _drop_sqlite_table(db_path: Path, table: str) -> None:
    """Drop a table from an independent connection to make a later statement fail."""
    helper = sqlite3.connect(str(db_path))
    try:
        helper.execute(f"DROP TABLE {table}")
        helper.commit()
    finally:
        helper.close()


def _sqlite_write_lock_is_free(db_path: Path) -> bool:
    """Return whether an independent writer can take the SQLite write lock."""
    probe = sqlite3.connect(str(db_path), timeout=0)
    try:
        probe.execute("CREATE TABLE IF NOT EXISTS probe_lock (x INTEGER)")
        probe.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


async def test_failed_add_items_rolls_back_and_reuses_connection():
    """A failed add must roll back its partial write and leave the session reusable."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "add_rollback.db"
        session = AsyncSQLiteSession("add_rollback", db_path)
        unserializable = cast(TResponseInputItem, {"role": "user", "content": object()})
        try:
            with pytest.raises(TypeError):
                await session.add_items([unserializable])

            conn = await session._get_connection()
            assert conn.in_transaction is False
            assert _sqlite_write_lock_is_free(db_path)

            await session.add_items([{"role": "user", "content": "after failure"}])
            assert [item.get("content") for item in await session.get_items()] == ["after failure"]
        finally:
            await session.close()


async def test_failed_clear_session_rolls_back():
    """A failed clear must restore earlier statements and release the shared write lock."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "clear_rollback.db"
        session = AsyncSQLiteSession("clear_rollback", db_path)
        try:
            await session.add_items([{"role": "user", "content": "kept"}])

            _drop_sqlite_table(db_path, "agent_sessions")

            with pytest.raises(sqlite3.OperationalError):
                await session.clear_session()

            conn = await session._get_connection()
            assert conn.in_transaction is False
            assert _sqlite_write_lock_is_free(db_path)
        finally:
            await session.close()


async def test_failed_pop_item_releases_write_lock():
    """A failed pop must not leave a write transaction on the shared connection."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "pop_rollback.db"
        session = AsyncSQLiteSession("pop_rollback", db_path)
        try:
            await session.add_items([{"role": "user", "content": "kept"}])

            _drop_sqlite_table(db_path, "agent_messages")

            with pytest.raises(sqlite3.OperationalError):
                await session.pop_item()

            conn = await session._get_connection()
            assert conn.in_transaction is False
            assert _sqlite_write_lock_is_free(db_path)
        finally:
            await session.close()


async def test_failed_initialization_closes_candidate_connection():
    """A failed initialization must not retain a half-initialized connection."""

    class FailingInitSession(AsyncSQLiteSession):
        captured_connection: Any = None

        async def _init_db_for_connection(self, conn: Any) -> None:
            self.captured_connection = conn
            raise RuntimeError("initialization failed")

    session = FailingInitSession("failed_init")
    try:
        with pytest.raises(RuntimeError, match="initialization failed"):
            await session.get_items()

        assert session._connection is None
        assert session.captured_connection._running is False
    finally:
        try:
            await session.close()
        finally:
            if session.captured_connection is not None and session.captured_connection._running:
                await session.captured_connection.close()


async def test_cancelled_add_items_rolls_back_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancellation after the first write must release the transaction and database lock."""
    db_path = tmp_path / "cancelled_add.db"
    session = AsyncSQLiteSession("cancelled_add", db_path)
    try:
        await session.get_items()
        conn = await session._get_connection()
        real_execute = conn.execute
        real_rollback = conn.rollback
        first_write_started = asyncio.Event()
        rollback_started = asyncio.Event()
        allow_rollback = asyncio.Event()

        async def pause_after_first_write(*args: Any, **kwargs: Any) -> Any:
            cursor = await real_execute(*args, **kwargs)
            first_write_started.set()
            await asyncio.Event().wait()
            return cursor

        async def controlled_rollback() -> None:
            rollback_started.set()
            await allow_rollback.wait()
            await real_rollback()

        monkeypatch.setattr(conn, "execute", pause_after_first_write)
        monkeypatch.setattr(conn, "rollback", controlled_rollback)
        task = asyncio.create_task(session.add_items([{"role": "user", "content": "cancelled"}]))
        try:
            await first_write_started.wait()
            task.cancel()
            await rollback_started.wait()
            task.cancel()
            allow_rollback.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            allow_rollback.set()
            monkeypatch.setattr(conn, "execute", real_execute)
            monkeypatch.setattr(conn, "rollback", real_rollback)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert conn.in_transaction is False
        assert _sqlite_write_lock_is_free(db_path)
        assert await session.get_items() == []
    finally:
        await session.close()


async def test_operation_failure_then_cancellation_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancellation during rollback must supersede an earlier operation failure."""
    db_path = tmp_path / "failure_then_cancelled_rollback.db"
    session = AsyncSQLiteSession("failure_then_cancelled_rollback", db_path)
    task: asyncio.Task[None] | None = None
    try:
        await session.get_items()
        conn = await session._get_connection()
        real_execute = conn.execute
        real_rollback = conn.rollback
        rollback_started = asyncio.Event()
        allow_rollback = asyncio.Event()

        async def fail_after_write(*args: Any, **kwargs: Any) -> Any:
            await real_execute(*args, **kwargs)
            raise RuntimeError("operation failed")

        async def controlled_rollback() -> None:
            rollback_started.set()
            await allow_rollback.wait()
            await real_rollback()

        monkeypatch.setattr(conn, "execute", fail_after_write)
        monkeypatch.setattr(conn, "rollback", controlled_rollback)
        task = asyncio.create_task(session.add_items([{"role": "user", "content": "cancelled"}]))
        try:
            await rollback_started.wait()
            task.cancel("first-caller-cancel")
            await asyncio.sleep(0)
            task.cancel("second-caller-cancel")
            allow_rollback.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await task
        finally:
            allow_rollback.set()
            monkeypatch.setattr(conn, "execute", real_execute)
            monkeypatch.setattr(conn, "rollback", real_rollback)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        _assert_cancel_message(exc_info.value, "first-caller-cancel")
        assert conn.in_transaction is False
        assert _sqlite_write_lock_is_free(db_path)
        assert await session.get_items() == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await session.close()


async def test_rollback_failure_closes_and_evicts_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A connection that cannot roll back must not remain cached or retain its write lock."""
    db_path = tmp_path / "rollback_failure.db"
    session = AsyncSQLiteSession("rollback_failure", db_path)
    try:
        await session.get_items()
        conn = await session._get_connection()

        async def fail_rollback() -> None:
            raise RuntimeError("rollback failed")

        monkeypatch.setattr(conn, "rollback", fail_rollback)
        unserializable = cast(TResponseInputItem, {"role": "user", "content": object()})

        with pytest.raises(TypeError):
            await session.add_items([unserializable])

        assert session._connection is None
        assert session._closed is False
        assert _sqlite_write_lock_is_free(db_path)

        await session.add_items([{"role": "user", "content": "after failure"}])
        assert [item.get("content") for item in await session.get_items()] == ["after failure"]
    finally:
        await session.close()


async def test_close_retries_connection_quarantined_after_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A failed invalidation close must remain owned until a later close succeeds."""
    db_path = tmp_path / "close_retry.db"
    session = AsyncSQLiteSession("close_retry", db_path)
    conn: Any = None
    real_close: Any = None
    try:
        await session.get_items()
        conn = await session._get_connection()
        real_close = conn.close

        async def fail_rollback() -> None:
            raise RuntimeError("rollback failed")

        async def fail_close() -> None:
            raise RuntimeError("close failed")

        monkeypatch.setattr(conn, "rollback", fail_rollback)
        monkeypatch.setattr(conn, "close", fail_close)
        unserializable = cast(TResponseInputItem, {"role": "user", "content": object()})

        with pytest.raises(TypeError):
            await session.add_items([unserializable])

        assert session._closed is True
        assert session._connection is None
        assert conn in session._quarantined_connections
        assert conn._running is True
        assert _sqlite_write_lock_is_free(db_path) is False

        monkeypatch.setattr(conn, "close", real_close)
        await session.close()

        assert session._quarantined_connections == set()
        assert conn._running is False
        assert _sqlite_write_lock_is_free(db_path)
    finally:
        if conn is not None and real_close is not None:
            monkeypatch.setattr(conn, "close", real_close)
        try:
            await session.close()
        finally:
            if conn is not None and real_close is not None and conn._running:
                await real_close()


async def test_close_retries_quarantined_failed_initialization_candidate(
    monkeypatch: pytest.MonkeyPatch,
):
    """A failed initialization candidate close must remain owned for close retry."""

    class FailingInitSession(AsyncSQLiteSession):
        captured_connection: Any = None
        real_close: Any = None

        async def _init_db_for_connection(self, conn: Any) -> None:
            self.captured_connection = conn
            self.real_close = conn.close

            async def fail_close() -> None:
                raise RuntimeError("close failed")

            monkeypatch.setattr(conn, "close", fail_close)
            raise RuntimeError("initialization failed")

    session = FailingInitSession("failed_init_close_retry")
    try:
        with pytest.raises(RuntimeError, match="initialization failed"):
            await session.get_items()

        conn = session.captured_connection
        assert session._closed is True
        assert conn in session._quarantined_connections
        assert conn._running is True

        monkeypatch.setattr(conn, "close", session.real_close)
        await session.close()

        assert session._quarantined_connections == set()
        assert conn._running is False
    finally:
        if session.captured_connection is not None and session.real_close is not None:
            monkeypatch.setattr(session.captured_connection, "close", session.real_close)
        try:
            await session.close()
        finally:
            if (
                session.captured_connection is not None
                and session.real_close is not None
                and session.captured_connection._running
            ):
                await session.real_close()


async def test_cancelled_initialization_finishes_candidate_close(
    monkeypatch: pytest.MonkeyPatch,
):
    """Repeated cancellation must not release initialization ownership before close finishes."""
    init_started = asyncio.Event()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class CancelledInitSession(AsyncSQLiteSession):
        captured_connection: Any = None
        real_close: Any = None

        async def _init_db_for_connection(self, conn: Any) -> None:
            self.captured_connection = conn
            self.real_close = conn.close

            async def controlled_close() -> None:
                close_started.set()
                await allow_close.wait()
                await self.real_close()

            monkeypatch.setattr(conn, "close", controlled_close)
            init_started.set()
            await asyncio.Event().wait()

    session = CancelledInitSession("cancelled_init")
    task = asyncio.create_task(session.get_items())
    try:
        try:
            await init_started.wait()
            task.cancel()
            await close_started.wait()
            task.cancel()
            allow_close.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            allow_close.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert session._connection is None
        assert session.captured_connection._running is False
    finally:
        if session.captured_connection is not None and session.real_close is not None:
            monkeypatch.setattr(session.captured_connection, "close", session.real_close)
        try:
            await session.close()
        finally:
            if (
                session.captured_connection is not None
                and session.real_close is not None
                and session.captured_connection._running
            ):
                await session.real_close()


async def test_initialization_failure_then_cancellation_during_candidate_close(
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancellation during candidate close must supersede an initialization failure."""
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class FailingInitSession(AsyncSQLiteSession):
        captured_connection: Any = None
        real_close: Any = None

        async def _init_db_for_connection(self, conn: Any) -> None:
            self.captured_connection = conn
            self.real_close = conn.close

            async def controlled_close() -> None:
                close_started.set()
                await allow_close.wait()
                await self.real_close()

            monkeypatch.setattr(conn, "close", controlled_close)
            raise RuntimeError("initialization failed")

    session = FailingInitSession("failed_init_then_cancelled_close")
    task = asyncio.create_task(session.get_items())
    try:
        try:
            await close_started.wait()
            task.cancel("first-caller-cancel")
            await asyncio.sleep(0)
            task.cancel("second-caller-cancel")
            allow_close.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await task
        finally:
            allow_close.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        _assert_cancel_message(exc_info.value, "first-caller-cancel")
        assert session._connection is None
        assert session.captured_connection._running is False
    finally:
        allow_close.set()
        if session.captured_connection is not None and session.real_close is not None:
            monkeypatch.setattr(session.captured_connection, "close", session.real_close)
        try:
            await session.close()
        finally:
            if (
                session.captured_connection is not None
                and session.real_close is not None
                and session.captured_connection._running
            ):
                await session.real_close()


async def test_cancelled_connect_closes_eventually_acquired_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancellation during connect must wait for and close the eventual connection."""
    import aiosqlite

    real_connect = aiosqlite.connect
    connect_started = asyncio.Event()
    allow_connect = asyncio.Event()
    created_connections: list[Any] = []

    async def controlled_connect(database: str) -> Any:
        connect_started.set()
        await allow_connect.wait()
        conn = await real_connect(database)
        created_connections.append(conn)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", controlled_connect)
    session = AsyncSQLiteSession("cancelled_connect")
    task = asyncio.create_task(session.get_items())
    try:
        try:
            await connect_started.wait()
            task.cancel()
            allow_connect.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            allow_connect.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert len(created_connections) == 1
        assert created_connections[0]._running is False
        assert session._connection is None
    finally:
        await session.close()
        for conn in created_connections:
            if conn._running:
                await conn.close()

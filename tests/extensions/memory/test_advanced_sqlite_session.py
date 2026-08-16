"""Tests for AdvancedSQLiteSession functionality."""

import asyncio
import contextlib
import json
import logging
import multiprocessing
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("sqlalchemy")  # Skip tests if SQLAlchemy is not installed
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

import agents._debug as _debug
from agents import Agent, Runner, TResponseInputItem, function_tool
from agents.extensions.memory import AdvancedSQLiteSession
from agents.result import RunResult
from agents.run_context import RunContextWrapper
from agents.testing import ScriptedModel
from agents.usage import Usage
from tests.test_responses import get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio


def _claim_structure_tables_in_process(
    db_path: str,
    sessions_table: str,
    messages_table: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    """Construct a create_tables session in a child process and report the outcome."""
    pair = (sessions_table, messages_table)
    ready.set()
    start.wait(timeout=30)
    try:
        session = AdvancedSQLiteSession(
            session_id="concurrent",
            db_path=db_path,
            create_tables=True,
            sessions_table=sessions_table,
            messages_table=messages_table,
        )
        session.close()
    except ValueError:
        results.put(("rejected", pair))
    except BaseException as exc:  # pragma: no cover - surfaced in the assertion below
        results.put((f"error:{type(exc).__name__}", pair))
    else:
        results.put(("claimed", pair))


def _create_owner_bearing_structure_tables(
    db_path: Path,
    *,
    create_base_tables: bool = True,
    message_foreign_keys: str = "",
    usage_foreign_key: str = "",
    message_session_column: str = "session_id",
    message_id_column: str = "message_id",
    usage_session_column: str = "session_id",
) -> None:
    """Create structurally usable owner tables with caller-selected ownership metadata."""
    message_constraints = f", {message_foreign_keys}" if message_foreign_keys else ""
    usage_constraint = f", {usage_foreign_key}" if usage_foreign_key else ""
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        if create_base_tables:
            conn.execute("""
                CREATE TABLE agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES agent_sessions (session_id)
                        ON DELETE CASCADE
                )
            """)
        conn.execute("CREATE TABLE wrong_sessions (session_id TEXT PRIMARY KEY)")
        conn.execute(f"""
            CREATE TABLE message_structure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                {message_session_column} TEXT NOT NULL,
                {message_id_column} INTEGER NOT NULL,
                branch_id TEXT NOT NULL DEFAULT 'main',
                message_type TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                user_turn_number INTEGER,
                branch_turn_number INTEGER,
                tool_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                {message_constraints}
            )
        """)
        conn.execute(f"""
            CREATE TABLE turn_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                {usage_session_column} TEXT NOT NULL,
                branch_id TEXT NOT NULL DEFAULT 'main',
                user_turn_number INTEGER NOT NULL,
                requests INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                input_tokens_details JSON,
                output_tokens_details JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, branch_id, user_turn_number)
                {usage_constraint}
            )
        """)
        conn.commit()


def _multiprocessing_context() -> Any:
    method = "spawn" if sys.platform == "win32" else "forkserver"
    return multiprocessing.get_context(method)


def _assert_cancel_message(exc: asyncio.CancelledError, expected: str) -> None:
    """Account for Python 3.10 dropping Task cancellation messages when re-awaited."""
    expected_args = (expected,) if sys.version_info >= (3, 11) else ()
    assert exc.args == expected_args


@function_tool
async def test_tool(query: str) -> str:
    """A test tool for testing tool call tracking."""
    return f"Tool result for: {query}"


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a scripted model."""
    return Agent(name="test", model=ScriptedModel(), tools=[test_tool])


@pytest.fixture
def usage_data() -> Usage:
    """Fixture for test usage data."""
    return Usage(
        requests=1,
        input_tokens=50,
        output_tokens=30,
        total_tokens=80,
        input_tokens_details=InputTokensDetails.model_validate(
            {"cache_write_tokens": 0, "cached_tokens": 10}
        ),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
    )


def create_mock_run_result(usage: Usage | None = None, agent: Agent | None = None) -> RunResult:
    """Helper function to create a mock RunResult for testing."""
    if agent is None:
        agent = Agent(name="test", model=ScriptedModel())

    if usage is None:
        usage = Usage(
            requests=1,
            input_tokens=50,
            output_tokens=30,
            total_tokens=80,
            input_tokens_details=InputTokensDetails.model_validate(
                {"cache_write_tokens": 0, "cached_tokens": 10}
            ),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
        )

    context_wrapper = RunContextWrapper(context=None, usage=usage)

    return RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="test output",
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=context_wrapper,
        _last_agent=agent,
        interruptions=[],
    )


class FailingOnceStructureMetadataSession(AdvancedSQLiteSession):
    """Advanced session test double that fails the next structure metadata write."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.fail_structure_metadata_once = True

    def _insert_structure_metadata(
        self,
        conn: Any,
        items: list[TResponseInputItem],
    ) -> None:
        if self.fail_structure_metadata_once:
            self.fail_structure_metadata_once = False
            raise RuntimeError("structure metadata failed")
        super()._insert_structure_metadata(conn, items)


class PartiallyFailingStructureMetadataSession(AdvancedSQLiteSession):
    """Advanced session test double that fails after writing one structure row."""

    def _insert_structure_metadata(
        self,
        conn: Any,
        items: list[TResponseInputItem],
    ) -> None:
        cursor = conn.execute(
            f"SELECT id FROM {self.messages_table} WHERE session_id = ? ORDER BY id ASC LIMIT 1",
            (self.session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("no inserted message id found")

        conn.execute(
            """
            INSERT INTO message_structure
            (session_id, message_id, branch_id, message_type, sequence_number,
             user_turn_number, branch_turn_number, tool_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self.session_id, row[0], self._current_branch_id, "user", 1, 1, 1, None),
        )
        raise RuntimeError("structure metadata failed after partial write")


async def test_advanced_session_basic_functionality(agent: Agent):
    """Test basic AdvancedSQLiteSession functionality."""
    session_id = "advanced_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test basic session operations work
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    # Get items and verify
    retrieved = await session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"

    session.close()


@pytest.mark.parametrize("redacted", [True, False])
async def test_create_branch_logging_respects_model_data_policy(monkeypatch, redacted: bool):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    mock_logger = Mock()
    session = AdvancedSQLiteSession(
        session_id="advanced_branch_logging",
        create_tables=True,
        logger=mock_logger,
    )
    secret = "SECRET_BRANCH_TURN_CONTENT"

    try:
        await session.add_items(
            [
                {"role": "user", "content": secret},
                {"role": "assistant", "content": "response"},
            ]
        )
        await session.create_branch_from_turn(1, "branch")

        logged = str(mock_logger.debug.call_args)
        assert (secret not in logged) is redacted
    finally:
        session.close()


async def test_advanced_session_respects_custom_table_names():
    """AdvancedSQLiteSession should consistently use configured table names."""
    session = AdvancedSQLiteSession(
        session_id="advanced_custom_tables",
        create_tables=True,
        sessions_table="custom_agent_sessions",
        messages_table="custom_agent_messages",
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "Let's do some math"},
        {"role": "assistant", "content": "Sure"},
    ]
    await session.add_items(items)

    assert await session.get_items() == items

    conversation_turns = await session.get_conversation_turns()
    assert [turn["turn"] for turn in conversation_turns] == [1, 2]

    matching_turns = await session.find_turns_by_content("math")
    assert [turn["turn"] for turn in matching_turns] == [2]

    conn = session._get_connection()
    structure_foreign_keys = {
        row[2] for row in conn.execute("PRAGMA foreign_key_list(message_structure)").fetchall()
    }
    usage_foreign_keys = {
        row[2] for row in conn.execute("PRAGMA foreign_key_list(turn_usage)").fetchall()
    }
    assert structure_foreign_keys == {
        session.messages_table,
        session.sessions_table,
    }
    assert usage_foreign_keys == {session.sessions_table}

    branch_name = await session.create_branch_from_turn(2, "custom_branch")
    assert branch_name == "custom_branch"
    assert await session.get_items() == items[:2]
    assert await session.get_items(branch_id="main") == items

    session.close()


async def test_add_items_rolls_back_messages_when_structure_metadata_fails():
    """Failed structure metadata writes should not leave invisible message rows."""
    session = FailingOnceStructureMetadataSession(
        session_id="advanced_add_items_rollback",
        create_tables=True,
    )
    items: list[TResponseInputItem] = [{"role": "user", "content": "not saved"}]

    try:
        with pytest.raises(RuntimeError, match="structure metadata failed"):
            await session.add_items(items)

        assert await session.get_items() == []

        with session._locked_connection() as conn:
            message_count = conn.execute(
                f"SELECT COUNT(*) FROM {session.messages_table} WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
            structure_count = conn.execute(
                "SELECT COUNT(*) FROM message_structure WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]

        assert message_count == 0
        assert structure_count == 0
    finally:
        session.close()


async def test_add_items_can_retry_after_structure_metadata_failure():
    """Retrying after a metadata failure should persist the batch exactly once."""
    session = FailingOnceStructureMetadataSession(
        session_id="advanced_add_items_retry",
        create_tables=True,
    )
    items: list[TResponseInputItem] = [{"role": "user", "content": "saved once"}]

    try:
        with pytest.raises(RuntimeError, match="structure metadata failed"):
            await session.add_items(items)

        await session.add_items(items)

        assert await session.get_items() == items

        with session._locked_connection() as conn:
            message_count = conn.execute(
                f"SELECT COUNT(*) FROM {session.messages_table} WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
            structure_count = conn.execute(
                "SELECT COUNT(*) FROM message_structure WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]

        assert message_count == 1
        assert structure_count == 1
    finally:
        session.close()


async def test_add_items_failure_preserves_existing_history():
    """A failed batch should not roll back or hide previously committed messages."""
    session = FailingOnceStructureMetadataSession(
        session_id="advanced_add_items_existing_history",
        create_tables=True,
    )
    existing_items: list[TResponseInputItem] = [{"role": "user", "content": "already saved"}]
    failed_items: list[TResponseInputItem] = [{"role": "assistant", "content": "not saved"}]

    try:
        session.fail_structure_metadata_once = False
        await session.add_items(existing_items)

        session.fail_structure_metadata_once = True
        with pytest.raises(RuntimeError, match="structure metadata failed"):
            await session.add_items(failed_items)

        assert await session.get_items() == existing_items

        with session._locked_connection() as conn:
            message_count = conn.execute(
                f"SELECT COUNT(*) FROM {session.messages_table} WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
            structure_count = conn.execute(
                "SELECT COUNT(*) FROM message_structure WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]

        assert message_count == 1
        assert structure_count == 1
    finally:
        session.close()


async def test_advanced_sqlite_session_closed_rejects_empty_add_items():
    """add_items([]) must not bypass the closed check through the empty-list fast path."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "closed_empty_add.db"
        session = AdvancedSQLiteSession(
            session_id="advanced_closed_empty_add",
            db_path=db_path,
            create_tables=True,
        )
        session.close()

        with pytest.raises(RuntimeError, match="SQLiteSession is closed"):
            await session.add_items([])


async def test_add_items_rolls_back_partial_structure_metadata_write():
    """Partial metadata writes should roll back with the message rows in the same batch."""
    session = PartiallyFailingStructureMetadataSession(
        session_id="advanced_add_items_partial_metadata",
        create_tables=True,
    )
    items: list[TResponseInputItem] = [{"role": "user", "content": "not saved"}]

    try:
        with pytest.raises(RuntimeError, match="structure metadata failed after partial write"):
            await session.add_items(items)

        assert await session.get_items() == []

        with session._locked_connection() as conn:
            message_count = conn.execute(
                f"SELECT COUNT(*) FROM {session.messages_table} WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
            structure_count = conn.execute(
                "SELECT COUNT(*) FROM message_structure WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]

        assert message_count == 0
        assert structure_count == 0
    finally:
        session.close()


async def test_add_items_rollback_failure_invalidates_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Advanced add failures must use the base rollback-failure invalidation path."""

    class FailingRollbackConnection(sqlite3.Connection):
        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

    db_path = tmp_path / "advanced_rollback_failure.db"
    session = AdvancedSQLiteSession(
        session_id="advanced_rollback_failure",
        db_path=db_path,
        create_tables=True,
    )
    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        factory=FailingRollbackConnection,
    )
    with session._connections_lock:
        session._connections.add(conn)
    real_get_connection = session._get_connection
    real_insert_structure_metadata = session._insert_structure_metadata
    monkeypatch.setattr(session, "_get_connection", lambda: conn)

    def fail_structure_metadata(*_args: Any) -> None:
        raise RuntimeError("structure metadata failed")

    monkeypatch.setattr(session, "_insert_structure_metadata", fail_structure_metadata)

    with pytest.raises(RuntimeError, match="structure metadata failed"):
        await session.add_items([{"role": "user", "content": "not saved"}])

    assert conn not in session._connections
    probe = sqlite3.connect(str(db_path), timeout=0)
    try:
        probe.execute("CREATE TABLE IF NOT EXISTS probe_lock (x INTEGER)")
        probe.commit()
    finally:
        probe.close()

    monkeypatch.setattr(session, "_get_connection", real_get_connection)
    monkeypatch.setattr(session, "_insert_structure_metadata", real_insert_structure_metadata)
    await session.add_items([{"role": "user", "content": "after failure"}])
    assert await session.get_items() == [{"role": "user", "content": "after failure"}]
    session.close()


async def test_structure_initialization_failure_invalidates_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Initialization must close its transaction connection after a schema failure."""

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    db_path = tmp_path / "advanced_init_failure.db"
    setup = AdvancedSQLiteSession(
        session_id="advanced_init_failure",
        db_path=db_path,
        create_tables=True,
    )
    try:
        await setup.add_items([{"role": "user", "content": "existing"}])
    finally:
        setup.close()

    conflict = sqlite3.connect(str(db_path))
    try:
        conflict.execute("DROP TABLE branch_reservations")
        conflict.execute("DROP INDEX idx_structure_session_seq")
        conflict.execute("CREATE TABLE idx_structure_session_seq (value INTEGER)")
        conflict.commit()
    finally:
        conflict.close()

    captured_connections: list[TrackingConnection] = []
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> TrackingConnection:
        connection = cast(
            TrackingConnection,
            real_connect(*args, **kwargs, factory=TrackingConnection),
        )
        captured_connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="already a table"):
        AdvancedSQLiteSession(
            session_id="advanced_init_failure",
            db_path=db_path,
            create_tables=True,
        )

    assert len(captured_connections) == 1
    assert captured_connections[0].closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        captured_connections[0].execute("SELECT 1")

    monkeypatch.setattr(sqlite3, "connect", real_connect)
    probe = real_connect(str(db_path), timeout=0)
    try:
        probe.execute("CREATE TABLE IF NOT EXISTS probe_lock (x INTEGER)")
        probe.commit()
    finally:
        probe.close()


@pytest.mark.parametrize("operation", ["add", "pop", "clear"])
async def test_post_commit_cancellation_propagates_after_known_mutation_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    """Cancellation after commit must propagate without inviting a mutation retry."""

    class PausingCommitConnection(sqlite3.Connection):
        pause_commit = False
        commit_finished = threading.Event()
        allow_return = threading.Event()

        def commit(self) -> None:
            super().commit()
            if self.pause_commit:
                self.pause_commit = False
                self.commit_finished.set()
                assert self.allow_return.wait(timeout=10)

    db_path = tmp_path / f"advanced_post_commit_{operation}.db"
    session = AdvancedSQLiteSession(
        session_id=f"advanced_post_commit_{operation}",
        db_path=db_path,
        create_tables=True,
    )
    item: TResponseInputItem = {"role": "user", "content": "once"}
    if operation != "add":
        await session.add_items([item])

    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        factory=PausingCommitConnection,
    )
    with session._connections_lock:
        session._connections.add(conn)
    monkeypatch.setattr(session, "_get_connection", lambda: conn)
    conn.pause_commit = True

    if operation == "add":
        mutation: asyncio.Task[Any] = asyncio.create_task(session.add_items([item]))
    elif operation == "pop":
        mutation = asyncio.create_task(session.pop_item())
    else:
        mutation = asyncio.create_task(session.clear_session())

    try:
        assert await asyncio.to_thread(conn.commit_finished.wait, 10)
        mutation.cancel()
        await asyncio.sleep(0)
        mutation.cancel()
        await asyncio.sleep(0)
        conn.allow_return.set()
        with pytest.raises(asyncio.CancelledError):
            await mutation
    finally:
        conn.allow_return.set()
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
    session.close()


@pytest.mark.parametrize("operation", ["create_branch", "delete_branch", "cleanup", "usage"])
async def test_auxiliary_mutation_cancellation_waits_for_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    usage_data: Usage,
    operation: str,
):
    """Branch and ancillary mutations must settle before cancellation propagates."""

    class PausingCommitConnection(sqlite3.Connection):
        pause_commit = False
        commit_finished = threading.Event()
        allow_return = threading.Event()

        def commit(self) -> None:
            super().commit()
            if self.pause_commit:
                self.pause_commit = False
                self.commit_finished.set()
                assert self.allow_return.wait(timeout=10)

    session = AdvancedSQLiteSession(
        session_id=f"advanced_auxiliary_cancel_{operation}",
        db_path=tmp_path / f"advanced_auxiliary_cancel_{operation}.db",
        create_tables=True,
    )
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    mutation: asyncio.Task[Any] | None = None

    try:
        if operation in {"create_branch", "delete_branch"}:
            await session.add_items(items)
        if operation == "delete_branch":
            await session.create_branch_from_turn(2, "cancelled_branch")
            await session.switch_to_branch("main")
        elif operation == "cleanup":
            with session._write_connection() as setup_connection:
                session._insert_items(
                    setup_connection,
                    [{"role": "user", "content": "orphan"}],
                )
                setup_connection.commit()
        elif operation == "usage":
            await session.add_items([{"role": "user", "content": "usage turn"}])

        connection = sqlite3.connect(
            str(session.db_path),
            check_same_thread=False,
            factory=PausingCommitConnection,
        )
        with session._connections_lock:
            session._connections.add(connection)
        monkeypatch.setattr(session, "_get_connection", lambda: connection)
        connection.pause_commit = True

        if operation == "create_branch":
            mutation = asyncio.create_task(session.create_branch_from_turn(2, "cancelled_branch"))
        elif operation == "delete_branch":
            mutation = asyncio.create_task(session.delete_branch("cancelled_branch"))
        elif operation == "cleanup":
            mutation = asyncio.create_task(session._cleanup_orphaned_messages())
        else:
            mutation = asyncio.create_task(
                session.store_run_usage(create_mock_run_result(usage_data))
            )

        assert await asyncio.to_thread(connection.commit_finished.wait, 10)
        mutation.cancel("first-caller-cancel")
        await asyncio.sleep(0)
        mutation.cancel("second-caller-cancel")
        await asyncio.sleep(0)
        connection.allow_return.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await mutation

        _assert_cancel_message(exc_info.value, "first-caller-cancel")
        assert mutation.cancelled()

        if operation == "create_branch":
            branches = await session.list_branches()
            assert {branch["branch_id"] for branch in branches} == {"main", "cancelled_branch"}
            assert session._current_branch_id == "cancelled_branch"
        elif operation == "delete_branch":
            branches = await session.list_branches()
            assert {branch["branch_id"] for branch in branches} == {"main"}
        elif operation == "cleanup":
            assert _count_rows(session, session.messages_table) == 0
        else:
            turn_usage = await session.get_turn_usage(1)
            assert isinstance(turn_usage, dict)
            assert turn_usage["total_tokens"] == usage_data.total_tokens
    finally:
        PausingCommitConnection.allow_return.set()
        if mutation is not None and not mutation.done():
            mutation.cancel()
            await asyncio.gather(mutation, return_exceptions=True)
        session.close()


async def test_message_structure_tracking(agent: Agent):
    """Test that message structure is properly tracked."""
    session_id = "structure_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add various types of messages
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "What's 2+2?"},
        {"type": "function_call", "name": "calculator", "arguments": '{"expression": "2+2"}'},  # type: ignore
        {"type": "function_call_output", "output": "4"},  # type: ignore
        {"role": "assistant", "content": "The answer is 4"},
        {"type": "reasoning", "summary": [{"text": "Simple math", "type": "summary_text"}]},  # type: ignore
    ]
    await session.add_items(items)

    # Get conversation structure
    conversation_turns = await session.get_conversation_by_turns()
    assert len(conversation_turns) == 1  # Should be one user turn

    turn_1_items = conversation_turns[1]
    assert len(turn_1_items) == 5

    # Verify item types are classified correctly
    item_types = [item["type"] for item in turn_1_items]
    assert "user" in item_types
    assert "function_call" in item_types
    assert "function_call_output" in item_types
    assert "assistant" in item_types
    assert "reasoning" in item_types

    session.close()


async def test_tool_usage_tracking(agent: Agent):
    """Test tool usage tracking functionality."""
    session_id = "tools_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items with tool calls
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Search for cats"},
        {"type": "function_call", "name": "web_search", "arguments": '{"query": "cats"}'},  # type: ignore
        {"type": "function_call_output", "output": "Found cat information"},  # type: ignore
        {"type": "function_call", "name": "calculator", "arguments": '{"expression": "1+1"}'},  # type: ignore
        {"type": "function_call_output", "output": "2"},  # type: ignore
        {"role": "assistant", "content": "I found information about cats and calculated 1+1=2"},
    ]
    await session.add_items(items)

    # Get tool usage
    tool_usage = await session.get_tool_usage()
    assert len(tool_usage) == 2  # Two different tools used

    tool_names = {usage[0] for usage in tool_usage}
    assert "web_search" in tool_names
    assert "calculator" in tool_names

    session.close()


async def test_tool_usage_tracking_preserves_namespaces_and_tool_search(agent: Agent):
    """Tool usage should retain namespaces and count tool_search calls once."""
    session_id = "tools_namespace_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Look up the same account in multiple systems"},
        {
            "type": "function_call",
            "name": "lookup_account",
            "namespace": "crm",
            "arguments": '{"account_id": "acct_123"}',
            "call_id": "crm-call",
        },
        {
            "type": "function_call",
            "name": "lookup_account",
            "namespace": "billing",
            "arguments": '{"account_id": "acct_123"}',
            "call_id": "billing-call",
        },
        {
            "type": "tool_search_call",
            "id": "tsc_memory",
            "arguments": {"paths": ["crm"], "query": "lookup_account"},
            "execution": "server",
            "status": "completed",
        },
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "id": "tso_memory",
                "execution": "server",
                "status": "completed",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_account",
                        "description": "Look up an account.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "account_id": {
                                    "type": "string",
                                }
                            },
                            "required": ["account_id"],
                        },
                        "defer_loading": True,
                    }
                ],
            },
        ),
    ]
    await session.add_items(items)

    usage_by_tool = {tool_name: count for tool_name, count, _turn in await session.get_tool_usage()}

    assert usage_by_tool["crm.lookup_account"] == 1
    assert usage_by_tool["billing.lookup_account"] == 1
    assert usage_by_tool["tool_search"] == 1

    session.close()


async def test_tool_usage_tracking_counts_tool_search_output_without_matching_call(
    agent: Agent,
) -> None:
    """Tool-search output-only histories should still report one tool_search usage."""
    session_id = "tools_tool_search_output_only_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Look up customer_42"},
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "id": "tso_memory_only",
                "execution": "server",
                "status": "completed",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_account",
                        "description": "Look up an account.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "account_id": {
                                    "type": "string",
                                }
                            },
                            "required": ["account_id"],
                        },
                    }
                ],
            },
        ),
    ]
    await session.add_items(items)

    usage_by_tool = {tool_name: count for tool_name, count, _turn in await session.get_tool_usage()}

    assert usage_by_tool["tool_search"] == 1

    session.close()


async def test_tool_usage_tracking_uses_bare_name_for_deferred_top_level_calls(agent: Agent):
    """Deferred top-level tool calls should not retain synthetic namespace aliases."""
    session_id = "tools_deferred_top_level_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "What is the weather?"},
        {
            "type": "function_call",
            "name": "get_weather",
            "arguments": '{"city": "Tokyo"}',
            "call_id": "weather-call",
        },
        {
            "type": "function_call",
            "name": "get_weather",
            "namespace": "get_weather",
            "arguments": '{"city": "Osaka"}',
            "call_id": "weather-call-2",
        },
    ]
    await session.add_items(items)

    usage_by_tool = {tool_name: count for tool_name, count, _turn in await session.get_tool_usage()}

    assert usage_by_tool["get_weather"] == 2
    assert "get_weather.get_weather" not in usage_by_tool

    session.close()


async def test_tool_usage_tracking_collapses_reserved_same_name_namespace_shape(
    agent: Agent,
):
    """Reserved same-name namespace wire shapes should collapse to the bare tool name."""
    session_id = "tools_deferred_top_level_namespace_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "What is the weather?"},
        {
            "type": "function_call",
            "name": "lookup_account",
            "namespace": "lookup_account",
            "arguments": '{"account_id": "acct_123"}',
            "call_id": "lookup-call",
        },
    ]
    await session.add_items(items)

    usage_by_tool = {tool_name: count for tool_name, count, _turn in await session.get_tool_usage()}

    assert usage_by_tool["lookup_account"] == 1
    assert "lookup_account.lookup_account" not in usage_by_tool

    session.close()


async def test_branching_functionality(agent: Agent):
    """Test branching functionality - create, switch, and delete branches."""
    session_id = "branching_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add multiple turns to main branch
    turn_1_items: list[TResponseInputItem] = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
    ]
    await session.add_items(turn_1_items)

    turn_2_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Second question"},
        {"role": "assistant", "content": "Second answer"},
    ]
    await session.add_items(turn_2_items)

    turn_3_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Third question"},
        {"role": "assistant", "content": "Third answer"},
    ]
    await session.add_items(turn_3_items)

    # Verify all items are in main branch
    all_items = await session.get_items()
    assert len(all_items) == 6

    # Create a branch from turn 2
    branch_name = await session.create_branch_from_turn(2, "test_branch")
    assert branch_name == "test_branch"

    # Verify we're now on the new branch
    assert session._current_branch_id == "test_branch"

    # Verify the branch has the same content up to turn 2 (copies messages before turn 2)
    branch_items = await session.get_items()
    assert len(branch_items) == 2  # Only first turn items (before turn 2)
    assert branch_items[0].get("content") == "First question"
    assert branch_items[1].get("content") == "First answer"

    # Switch back to main branch
    await session.switch_to_branch("main")
    assert session._current_branch_id == "main"

    # Verify main branch still has all items
    main_items = await session.get_items()
    assert len(main_items) == 6

    # List branches
    branches = await session.list_branches()
    assert len(branches) == 2
    branch_ids = [b["branch_id"] for b in branches]
    assert "main" in branch_ids
    assert "test_branch" in branch_ids

    # Delete the test branch
    await session.delete_branch("test_branch")

    # Verify branch is deleted
    branches_after_delete = await session.list_branches()
    assert len(branches_after_delete) == 1
    assert branches_after_delete[0]["branch_id"] == "main"

    session.close()


def _branch_collision_items() -> list[TResponseInputItem]:
    """Return three user turns with assistant replies for branch collision tests."""
    return [
        {"role": "user", "content": "Turn one question"},
        {"role": "assistant", "content": "Turn one answer"},
        {"role": "user", "content": "Turn two question"},
        {"role": "assistant", "content": "Turn two answer"},
        {"role": "user", "content": "Turn three question"},
        {"role": "assistant", "content": "Turn three answer"},
    ]


def _create_branch_in_process(
    worker_name: str,
    db_path: str,
    session_id: str,
    branch_name: str | None,
    turn_number: int,
    ready: Any,
    start: Any,
    attempted: Any,
    checked: Any,
    release_check: Any,
    hold_after_check: bool,
    results: Any,
) -> None:
    """Create a branch in a separate process with a controllable ID check."""

    class InstrumentedSession(AdvancedSQLiteSession):
        def __init__(self, **kwargs: Any) -> None:
            self._reported_reservation = False
            super().__init__(**kwargs)

        @contextlib.contextmanager
        def _locked_connection(self) -> Iterator[Any]:
            class BeginObservableConnection:
                def __init__(self, connection: Any) -> None:
                    self._connection = connection

                def execute(self, sql: str, parameters: Any = ()) -> Any:
                    if sql == "BEGIN IMMEDIATE":
                        attempted.set()
                    return self._connection.execute(sql, parameters)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._connection, name)

            with super()._locked_connection() as connection:
                yield BeginObservableConnection(connection)

        def _reserve_branch_id(
            self, cursor: Any, new_branch_id: str | None, from_turn_number: int
        ) -> str:
            branch_id = super()._reserve_branch_id(cursor, new_branch_id, from_turn_number)
            if not self._reported_reservation:
                self._reported_reservation = True
                checked.set()
                if hold_after_check and not release_check.wait(timeout=10):
                    raise TimeoutError("Timed out waiting to release the branch ID reservation")
            return branch_id

    session = InstrumentedSession(session_id=session_id, db_path=db_path)
    try:
        ready.set()
        if not start.wait(timeout=10):
            raise TimeoutError("Timed out waiting to start branch creation")
        with patch(
            "agents.extensions.memory.advanced_sqlite_session.time.time",
            return_value=1_700_000_000.0,
        ):
            branch_id = asyncio.run(session.create_branch_from_turn(turn_number, branch_name))
        branch_item: TResponseInputItem = {
            "role": "user",
            "content": f"{worker_name} branch item",
        }
        asyncio.run(session.add_items([branch_item]))
        results.put((worker_name, "success", branch_id))
    except Exception as exc:
        results.put((worker_name, "error", type(exc).__name__, str(exc)))
    finally:
        session.close()


def _pop_item_in_process(
    db_path: str,
    session_id: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    """Pop one AdvancedSQLite item in a separately synchronized process."""
    session = AdvancedSQLiteSession(
        session_id=session_id,
        db_path=db_path,
        create_tables=False,
    )
    try:
        ready.set()
        if not start.wait(timeout=10):
            raise TimeoutError("Timed out waiting to start pop")
        results.put(("ok", asyncio.run(session.pop_item())))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        session.close()


@pytest.mark.parametrize("branch_id", ["main", "existing_branch"])
async def test_create_branch_rejects_populated_branch_id(branch_id: str):
    """Creating a branch must not append history to a populated branch."""
    session = AdvancedSQLiteSession(
        session_id=f"branch_collision_{branch_id}",
        create_tables=True,
    )
    items = _branch_collision_items()

    try:
        await session.add_items(items)
        if branch_id != "main":
            await session.create_branch_from_turn(3, branch_id)
            await session.switch_to_branch("main")

        branch_items_before = await session.get_items(branch_id=branch_id)

        with pytest.raises(ValueError, match="already been used"):
            await session.create_branch_from_turn(2, branch_id)

        assert session._current_branch_id == "main"
        assert await session.get_items(branch_id=branch_id) == branch_items_before
        assert await session.get_items(branch_id="main") == items
    finally:
        session.close()


async def test_generated_branch_ids_do_not_merge_within_the_same_second(monkeypatch):
    """Repeated generated IDs must not merge copied branch histories."""
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    session = AdvancedSQLiteSession(
        session_id="generated_branch_collision",
        create_tables=True,
    )
    items = _branch_collision_items()

    try:
        await session.add_items(items)
        first_branch = await session.create_branch_from_turn(3)
        await session.switch_to_branch("main")
        second_branch = await session.create_branch_from_turn(3)

        assert first_branch == "branch_from_turn_3_1700000000"
        assert second_branch == "branch_from_turn_3_1700000000_2"
        assert await session.get_items(branch_id=first_branch) == items[:4]
        assert await session.get_items(branch_id=second_branch) == items[:4]
        assert await session.get_items(branch_id="main") == items
    finally:
        session.close()


async def test_failed_branch_reservation_rolls_back_and_allows_retry(tmp_path: Path):
    """A failure after reservation must not burn the branch ID or retain a transaction."""

    class FailAfterReservationSession(AdvancedSQLiteSession):
        fail_after_reservation = True

        def _reserve_branch_id(
            self, cursor: Any, new_branch_id: str | None, from_turn_number: int
        ) -> str:
            branch_id = super()._reserve_branch_id(cursor, new_branch_id, from_turn_number)
            if self.fail_after_reservation:
                self.fail_after_reservation = False
                raise RuntimeError("failed after reservation")
            return branch_id

    session = FailAfterReservationSession(
        session_id="failed_branch_reservation",
        db_path=tmp_path / "failed_branch_reservation.db",
        create_tables=True,
    )
    items = _branch_collision_items()

    try:
        await session.add_items(items)

        with pytest.raises(RuntimeError, match="failed after reservation"):
            await session.create_branch_from_turn(3, "retryable_branch")

        with session._locked_connection() as conn:
            reservation_count = conn.execute(
                """
                SELECT COUNT(*) FROM branch_reservations
                WHERE session_id = ? AND branch_id = ?
                """,
                (session.session_id, "retryable_branch"),
            ).fetchone()[0]
        assert reservation_count == 0
        assert await session.get_items(branch_id="retryable_branch") == []
        with session._connections_lock:
            assert all(not conn.in_transaction for conn in session._connections)

        assert await session.create_branch_from_turn(3, "retryable_branch") == "retryable_branch"
        assert await session.get_items(branch_id="retryable_branch") == items[:4]
    finally:
        session.close()


async def test_branch_ids_remain_reserved_after_delete_and_clear():
    """Deleted and cleared branch IDs must not be reused by stale session instances."""
    session = AdvancedSQLiteSession(
        session_id="branch_reservation_tombstones",
        create_tables=True,
    )
    items = _branch_collision_items()

    try:
        await session.add_items(items)
        await session.create_branch_from_turn(3, "used_branch")
        await session.switch_to_branch("main")
        await session.delete_branch("used_branch")

        with pytest.raises(ValueError, match="already been used"):
            await session.create_branch_from_turn(2, "used_branch")

        await session.clear_session()
        await session.add_items(items)
        with pytest.raises(ValueError, match="already been used"):
            await session.create_branch_from_turn(1, "used_branch")
    finally:
        session.close()


async def test_branch_reservations_migrate_existing_populated_branches(tmp_path: Path):
    """Existing databases must backfill branch reservations before allocating IDs."""
    db_path = tmp_path / "branch_reservation_migration.db"
    session_id = "branch_reservation_migration"
    items = _branch_collision_items()
    setup_session = AdvancedSQLiteSession(
        session_id=session_id,
        db_path=db_path,
        create_tables=True,
    )
    try:
        await setup_session.add_items(items)
        await setup_session.create_branch_from_turn(3, "existing_branch")
        with setup_session._locked_connection() as conn:
            conn.execute("DROP TABLE branch_reservations")
            conn.commit()
    finally:
        setup_session.close()

    session = AdvancedSQLiteSession(session_id=session_id, db_path=db_path)
    try:
        with pytest.raises(ValueError, match="already been used"):
            await session.create_branch_from_turn(2, "existing_branch")
        await session.create_branch_from_turn(1, "empty_branch")

        with session._locked_connection() as conn:
            reservations = conn.execute(
                """
                SELECT branch_id FROM branch_reservations
                WHERE session_id = ?
                ORDER BY branch_id
                """,
                (session_id,),
            ).fetchall()
        assert reservations == [("empty_branch",), ("existing_branch",), ("main",)]
    finally:
        session.close()


@pytest.mark.parametrize("operation", ["clear", "delete", "pop"])
async def test_legacy_branch_ids_are_backfilled_before_destructive_operations(
    tmp_path: Path, operation: str
):
    """Destructive operations must preserve IDs from databases created before reservations."""
    db_path = tmp_path / f"legacy_branch_{operation}.db"
    session_id = f"legacy_branch_{operation}"
    items = _branch_collision_items()
    setup_session = AdvancedSQLiteSession(
        session_id=session_id,
        db_path=db_path,
        create_tables=True,
    )
    try:
        await setup_session.add_items(items)
        await setup_session.create_branch_from_turn(3, "legacy_branch")
        with setup_session._locked_connection() as conn:
            conn.execute("DROP TABLE branch_reservations")
            conn.commit()
    finally:
        setup_session.close()

    session = AdvancedSQLiteSession(session_id=session_id, db_path=db_path)
    try:
        if operation == "clear":
            await session.clear_session()
            await session.add_items(items)
        elif operation == "delete":
            await session.delete_branch("legacy_branch")
        else:
            await session.switch_to_branch("legacy_branch")
            while await session.pop_item() is not None:
                pass
            await session.switch_to_branch("main")

        with pytest.raises(ValueError, match="already been used"):
            await session.create_branch_from_turn(2, "legacy_branch")
    finally:
        session.close()


@pytest.mark.parametrize("operation", ["missing_delete", "empty_pop"])
async def test_legacy_destructive_noops_leave_database_unlocked(tmp_path: Path, operation: str):
    """Lazy migration must not retain a writer transaction after a no-op or error."""
    db_path = tmp_path / f"legacy_noop_{operation}.db"
    session_id = f"legacy_noop_{operation}"
    setup_session = AdvancedSQLiteSession(
        session_id=session_id,
        db_path=db_path,
        create_tables=True,
    )
    try:
        await setup_session.add_items(_branch_collision_items())
        await setup_session.create_branch_from_turn(3, "legacy_branch")
        if operation == "empty_pop":
            await setup_session.switch_to_branch("main")
            while await setup_session.pop_item() is not None:
                pass
        with setup_session._locked_connection() as conn:
            conn.execute("DROP TABLE branch_reservations")
            conn.commit()
    finally:
        setup_session.close()

    session = AdvancedSQLiteSession(session_id=session_id, db_path=db_path)
    contender = AdvancedSQLiteSession(session_id=f"{session_id}_contender", db_path=db_path)
    try:
        if operation == "missing_delete":
            with pytest.raises(ValueError, match="does not exist"):
                await session.delete_branch("missing_branch")
        else:
            assert await session.pop_item() is None

        with session._connections_lock:
            assert all(not conn.in_transaction for conn in session._connections)

        await contender.add_items([{"role": "user", "content": "writer acquired"}])
    finally:
        contender.close()
        session.close()


@pytest.mark.parametrize("branch_name", [None, "shared_branch"])
@pytest.mark.parametrize("turn_number", [1, 3])
@pytest.mark.review_optional
async def test_branch_allocation_is_serialized_across_processes(
    tmp_path: Path, branch_name: str | None, turn_number: int
):
    """Processes must serialize branch reservations, including empty branches."""
    db_path = tmp_path / "branch_allocation.db"
    session_id = f"branch_allocation_{branch_name}_{turn_number}"
    setup_session = AdvancedSQLiteSession(
        session_id=session_id,
        db_path=db_path,
        create_tables=True,
    )
    items = _branch_collision_items()
    await setup_session.add_items(items)
    setup_session.close()

    context = _multiprocessing_context()
    results = context.Queue()
    release_check = context.Event()
    processes = []

    try:
        worker_events = []
        for worker_name, hold_after_check in (("first", True), ("second", False)):
            ready = context.Event()
            start = context.Event()
            attempted = context.Event()
            checked = context.Event()
            process = context.Process(
                target=_create_branch_in_process,
                args=(
                    worker_name,
                    str(db_path),
                    session_id,
                    branch_name,
                    turn_number,
                    ready,
                    start,
                    attempted,
                    checked,
                    release_check,
                    hold_after_check,
                    results,
                ),
            )
            process.start()
            processes.append(process)
            worker_events.append((ready, start, attempted, checked))

        first_ready, first_start, _, first_checked = worker_events[0]
        second_ready, second_start, second_attempted, second_checked = worker_events[1]
        assert first_ready.wait(timeout=30)
        first_start.set()
        assert first_checked.wait(timeout=30)
        assert second_ready.wait(timeout=30)
        second_start.set()
        assert second_attempted.wait(timeout=30)

        # The second process has entered branch creation, but SQLite's write transaction
        # must keep it from reserving an ID until the first process commits.
        assert not second_checked.wait(timeout=0.2)
        release_check.set()

        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        process_results = [results.get(timeout=5), results.get(timeout=5)]
        successful_ids = [result[2] for result in process_results if result[1] == "success"]
        if branch_name is None:
            assert all(result[1] == "success" for result in process_results)
            assert sorted(successful_ids) == [
                f"branch_from_turn_{turn_number}_1700000000",
                f"branch_from_turn_{turn_number}_1700000000_2",
            ]
        else:
            assert successful_ids == [branch_name]
            errors = [result for result in process_results if result[1] == "error"]
            assert len(errors) == 1
            assert errors[0][2] == "ValueError"
            assert "already been used" in errors[0][3]

        verification_session = AdvancedSQLiteSession(session_id=session_id, db_path=db_path)
        copied_items = items[: 2 * (turn_number - 1)]
        successful_results = [result for result in process_results if result[1] == "success"]
        for worker_name, _, branch_id in successful_results:
            assert await verification_session.get_items(branch_id=branch_id) == [
                *copied_items,
                {"role": "user", "content": f"{worker_name} branch item"},
            ]
        assert await verification_session.get_items(branch_id="main") == items
        verification_session.close()
    finally:
        release_check.set()
        for _, start, _, _ in worker_events:
            start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()


async def test_delete_branch_removes_branch_only_messages():
    """Deleting a branch should not leave unreferenced branch-only messages behind."""
    session_id = "branch_delete_cleanup_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    main_items: list[TResponseInputItem] = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
        {"role": "assistant", "content": "Second answer"},
    ]
    await session.add_items(main_items)

    await session.create_branch_from_turn(2, "cleanup_branch")
    branch_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Branch-only question"},
        {"role": "assistant", "content": "Branch-only answer"},
    ]
    await session.add_items(branch_items)

    await session.delete_branch("cleanup_branch", force=True)

    with session._locked_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT message_data
            FROM {session.messages_table}
            WHERE session_id = ?
            ORDER BY id
            """,
            (session.session_id,),
        ).fetchall()

    contents = [json.loads(message_data)["content"] for (message_data,) in rows]
    assert contents == [
        "First question",
        "First answer",
        "Second question",
        "Second answer",
    ]
    assert await session.get_items(branch_id="main") == main_items

    session.close()


async def test_delete_branch_keeps_messages_still_referenced_by_another_branch():
    """Deleting one branch should keep messages inherited by a surviving branch."""
    session = AdvancedSQLiteSession(
        session_id="branch_delete_shared_descendant_test",
        create_tables=True,
    )

    main_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Main first question"},
        {"role": "assistant", "content": "Main first answer"},
        {"role": "user", "content": "Main second question"},
        {"role": "assistant", "content": "Main second answer"},
    ]
    branch_a_shared_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Branch A shared question"},
        {"role": "assistant", "content": "Branch A shared answer"},
    ]
    branch_a_only_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Branch A only question"},
        {"role": "assistant", "content": "Branch A only answer"},
    ]

    try:
        await session.add_items(main_items)
        await session.create_branch_from_turn(2, "branch_a")
        await session.add_items(branch_a_shared_items + branch_a_only_items)

        await session.create_branch_from_turn(3, "branch_b")
        await session.delete_branch("branch_a")

        with session._locked_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT message_data
                FROM {session.messages_table}
                WHERE session_id = ?
                ORDER BY id
                """,
                (session.session_id,),
            ).fetchall()

        contents = [json.loads(message_data)["content"] for (message_data,) in rows]
        assert "Branch A shared question" in contents
        assert "Branch A shared answer" in contents
        assert "Branch A only question" not in contents
        assert "Branch A only answer" not in contents
        assert await session.get_items(branch_id="branch_b") == [
            *main_items[:2],
            *branch_a_shared_items,
        ]
    finally:
        session.close()


async def test_orphan_cleanup_uses_set_based_delete_for_many_messages():
    """Orphan cleanup should not build one DELETE parameter per orphaned row."""

    class RecordingCursor:
        def __init__(self, cursor: Any, connection: "RecordingConnection") -> None:
            self._cursor = cursor
            self._connection = connection

        @property
        def rowcount(self) -> int:
            return cast(int, self._cursor.rowcount)

        def execute(self, sql: str, parameters: Any = None) -> Any:
            normalized_sql = " ".join(sql.split()).upper()
            if normalized_sql.startswith("DELETE"):
                self._connection.delete_parameter_counts.append(len(parameters or ()))
            if parameters is None:
                return self._cursor.execute(sql)
            return self._cursor.execute(sql, parameters)

        def fetchall(self) -> Any:
            return self._cursor.fetchall()

        def close(self) -> None:
            self._cursor.close()

    class RecordingConnection:
        def __init__(self, conn: Any) -> None:
            self._conn = conn
            self.delete_parameter_counts: list[int] = []

        def cursor(self) -> RecordingCursor:
            return RecordingCursor(self._conn.cursor(), self)

    session = AdvancedSQLiteSession(
        session_id="branch_delete_many_orphans_cleanup",
        create_tables=True,
    )
    orphan_items: list[TResponseInputItem] = [
        {"role": "user", "content": f"orphan {i}"} for i in range(1200)
    ]

    try:
        with session._locked_connection() as conn:
            session._insert_items(conn, orphan_items)
            conn.commit()

            recording_conn = RecordingConnection(conn)
            deleted_count = session._cleanup_orphaned_messages_sync(cast(Any, recording_conn))
            conn.commit()

            remaining_count = conn.execute(
                f"SELECT COUNT(*) FROM {session.messages_table} WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]

        assert deleted_count == len(orphan_items)
        assert remaining_count == 0
        assert recording_conn.delete_parameter_counts == [2]
    finally:
        session.close()


async def test_get_conversation_turns():
    """Test get_conversation_turns functionality."""
    session_id = "conversation_turns_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add multiple turns
    turn_1_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello there"},
        {"role": "assistant", "content": "Hi!"},
    ]
    await session.add_items(turn_1_items)

    turn_2_items: list[TResponseInputItem] = [
        {"role": "user", "content": "How are you doing today?"},
        {"role": "assistant", "content": "I'm doing well, thanks!"},
    ]
    await session.add_items(turn_2_items)

    # Get conversation turns
    turns = await session.get_conversation_turns()
    assert len(turns) == 2

    # Verify turn structure
    assert turns[0]["turn"] == 1
    assert turns[0]["content"] == "Hello there"
    assert turns[0]["full_content"] == "Hello there"
    assert turns[0]["can_branch"] is True
    assert "timestamp" in turns[0]

    assert turns[1]["turn"] == 2
    assert turns[1]["content"] == "How are you doing today?"
    assert turns[1]["full_content"] == "How are you doing today?"
    assert turns[1]["can_branch"] is True

    session.close()


async def test_find_turns_by_content():
    """Test find_turns_by_content functionality."""
    session_id = "find_turns_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add multiple turns with different content
    turn_1_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Tell me about cats"},
        {"role": "assistant", "content": "Cats are great pets"},
    ]
    await session.add_items(turn_1_items)

    turn_2_items: list[TResponseInputItem] = [
        {"role": "user", "content": "What about dogs?"},
        {"role": "assistant", "content": "Dogs are also great pets"},
    ]
    await session.add_items(turn_2_items)

    turn_3_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Tell me about cats again"},
        {"role": "assistant", "content": "Cats are wonderful companions"},
    ]
    await session.add_items(turn_3_items)

    # Search for turns containing "cats"
    cat_turns = await session.find_turns_by_content("cats")
    assert len(cat_turns) == 2
    assert cat_turns[0]["turn"] == 1
    assert cat_turns[1]["turn"] == 3

    # Search for turns containing "dogs"
    dog_turns = await session.find_turns_by_content("dogs")
    assert len(dog_turns) == 1
    assert dog_turns[0]["turn"] == 2

    # Search for non-existent content
    no_turns = await session.find_turns_by_content("elephants")
    assert len(no_turns) == 0

    session.close()


async def test_get_conversation_turns_with_list_content():
    """List (multimodal) content is previewed as a string instead of crashing or leaking a list."""
    session_id = "conversation_turns_list_content_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # A short list content must be previewed as a string, not returned as the raw list.
    short_items: list[TResponseInputItem] = [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
    ]
    await session.add_items(short_items)

    # A long list content must not raise when the preview is built.
    long_items: list[TResponseInputItem] = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": str(i)} for i in range(101)],
        },
    ]
    await session.add_items(long_items)

    turns = await session.get_conversation_turns()
    assert len(turns) == 2

    # 'content' is the documented truncated preview string, while 'full_content' keeps the list.
    assert isinstance(turns[0]["content"], str)
    assert isinstance(turns[0]["full_content"], list)

    assert isinstance(turns[1]["content"], str)
    assert turns[1]["content"].endswith("...")
    assert isinstance(turns[1]["full_content"], list)

    session.close()


async def test_find_turns_by_content_with_list_content():
    """find_turns_by_content returns a string preview for list (multimodal) content."""
    session_id = "find_turns_list_content_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": [{"type": "input_text", "text": "hello world"}]},
    ]
    await session.add_items(items)

    matches = await session.find_turns_by_content("hello")
    assert len(matches) == 1
    assert isinstance(matches[0]["content"], str)
    assert isinstance(matches[0]["full_content"], list)

    session.close()


async def test_create_branch_from_content():
    """Test create_branch_from_content functionality."""
    session_id = "branch_from_content_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add multiple turns
    turn_1_items: list[TResponseInputItem] = [
        {"role": "user", "content": "First question about math"},
        {"role": "assistant", "content": "Math answer"},
    ]
    await session.add_items(turn_1_items)

    turn_2_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Second question about science"},
        {"role": "assistant", "content": "Science answer"},
    ]
    await session.add_items(turn_2_items)

    turn_3_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Another math question"},
        {"role": "assistant", "content": "Another math answer"},
    ]
    await session.add_items(turn_3_items)

    # Create branch from first occurrence of "math"
    branch_name = await session.create_branch_from_content("math", "math_branch")
    assert branch_name == "math_branch"

    # Verify we're on the new branch
    assert session._current_branch_id == "math_branch"

    # Verify branch contains only items up to the first math turn (copies messages before turn 1)
    branch_items = await session.get_items()
    assert len(branch_items) == 0  # No messages before turn 1

    # Test error case - search term not found
    with pytest.raises(ValueError, match="No user turns found containing 'nonexistent'"):
        await session.create_branch_from_content("nonexistent", "error_branch")

    session.close()


async def test_branch_specific_operations():
    """Test operations that work with specific branches."""
    session_id = "branch_specific_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items to main branch
    turn_1_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Main branch question"},
        {"role": "assistant", "content": "Main branch answer"},
    ]
    await session.add_items(turn_1_items)

    # Add usage data for main branch
    usage_main = Usage(requests=1, input_tokens=50, output_tokens=30, total_tokens=80)
    run_result_main = create_mock_run_result(usage_main)
    await session.store_run_usage(run_result_main)

    # Create a branch from turn 1 (copies messages before turn 1, so empty)
    await session.create_branch_from_turn(1, "test_branch")

    # Add items to the new branch
    turn_2_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Branch question"},
        {"role": "assistant", "content": "Branch answer"},
    ]
    await session.add_items(turn_2_items)

    # Add usage data for branch
    usage_branch = Usage(requests=1, input_tokens=40, output_tokens=20, total_tokens=60)
    run_result_branch = create_mock_run_result(usage_branch)
    await session.store_run_usage(run_result_branch)

    # Test get_items with branch_id parameter
    main_items = await session.get_items(branch_id="main")
    assert len(main_items) == 2
    assert main_items[0].get("content") == "Main branch question"

    current_items = await session.get_items()  # Should get from current branch
    assert len(current_items) == 2  # Only the items added to the branch (copied branch is empty)

    # Test get_conversation_turns with branch_id
    main_turns = await session.get_conversation_turns(branch_id="main")
    assert len(main_turns) == 1
    assert main_turns[0]["content"] == "Main branch question"

    current_turns = await session.get_conversation_turns()  # Should get from current branch
    assert len(current_turns) == 1  # Only one turn in the current branch

    # Test get_session_usage with branch_id
    main_usage = await session.get_session_usage(branch_id="main")
    assert main_usage is not None
    assert main_usage["total_turns"] == 1

    all_usage = await session.get_session_usage()  # Should get from all branches
    assert all_usage is not None
    assert all_usage["total_turns"] == 2  # Main branch has 1, current branch has 1

    session.close()


async def test_branch_error_handling():
    """Test error handling in branching operations."""
    session_id = "branch_error_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test creating branch from non-existent turn
    with pytest.raises(ValueError, match="Turn 5 does not contain a user message"):
        await session.create_branch_from_turn(5, "error_branch")

    # Test switching to non-existent branch
    with pytest.raises(ValueError, match="Branch 'nonexistent' does not exist"):
        await session.switch_to_branch("nonexistent")

    # Test deleting non-existent branch
    with pytest.raises(ValueError, match="Branch 'nonexistent' does not exist"):
        await session.delete_branch("nonexistent")

    # Test deleting main branch
    with pytest.raises(ValueError, match="Cannot delete the 'main' branch"):
        await session.delete_branch("main")

    # Test deleting empty branch ID
    with pytest.raises(ValueError, match="Branch ID cannot be empty"):
        await session.delete_branch("")

    # Test deleting empty branch ID (whitespace only)
    with pytest.raises(ValueError, match="Branch ID cannot be empty"):
        await session.delete_branch("   ")

    session.close()


async def test_branch_deletion_with_force():
    """Test branch deletion with force parameter."""
    session_id = "force_delete_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items to main branch
    await session.add_items([{"role": "user", "content": "Main question"}])
    await session.add_items([{"role": "user", "content": "Second question"}])

    # Create and switch to a branch from turn 2
    await session.create_branch_from_turn(2, "temp_branch")
    assert session._current_branch_id == "temp_branch"

    # Add some content to the branch so it exists
    await session.add_items([{"role": "user", "content": "Branch question"}])

    # Verify branch exists
    branches = await session.list_branches()
    branch_ids = [b["branch_id"] for b in branches]
    assert "temp_branch" in branch_ids

    # Try to delete current branch without force (should fail)
    with pytest.raises(ValueError, match="Cannot delete current branch"):
        await session.delete_branch("temp_branch")

    # Delete current branch with force (should succeed and switch to main)
    await session.delete_branch("temp_branch", force=True)

    # Verify we're back on main branch
    assert session._current_branch_id == "main"

    # Verify branch is deleted
    branches_after = await session.list_branches()
    assert len(branches_after) == 1
    assert branches_after[0]["branch_id"] == "main"

    session.close()


async def test_get_items_with_parameters():
    """Test get_items with new parameters (include_inactive, branch_id)."""
    session_id = "get_items_params_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items to main branch
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
        {"role": "assistant", "content": "Second answer"},
    ]
    await session.add_items(items)

    # Test get_items with limit (gets most recent N items)
    limited_items = await session.get_items(limit=2)
    assert len(limited_items) == 2
    assert limited_items[0].get("content") == "Second question"  # Most recent first
    assert limited_items[1].get("content") == "Second answer"

    # Test get_items with branch_id
    main_items = await session.get_items(branch_id="main")
    assert len(main_items) == 4

    # Test get_items (no longer has include_inactive parameter)
    all_items = await session.get_items()
    assert len(all_items) == 4

    # Create a branch from turn 2 and test branch-specific get_items
    await session.create_branch_from_turn(2, "test_branch")

    # Add items to branch
    branch_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Branch question"},
        {"role": "assistant", "content": "Branch answer"},
    ]
    await session.add_items(branch_items)

    # Test getting items from specific branch (should include copied items + new items)
    branch_items_result = await session.get_items(branch_id="test_branch")
    assert len(branch_items_result) == 4  # 2 copied from main (before turn 2) + 2 new items

    # Test getting items from main branch while on different branch
    main_items_from_branch = await session.get_items(branch_id="main")
    assert len(main_items_from_branch) == 4

    session.close()


async def test_usage_tracking_storage(agent: Agent, usage_data: Usage):
    """Test usage data storage and retrieval."""
    session_id = "usage_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Simulate adding items for turn 1 to increment turn counter
    await session.add_items([{"role": "user", "content": "First turn"}])
    run_result_1 = create_mock_run_result(usage_data)
    await session.store_run_usage(run_result_1)

    # Create different usage data for turn 2
    usage_data_2 = Usage(
        requests=2,
        input_tokens=75,
        output_tokens=45,
        total_tokens=120,
        input_tokens_details=InputTokensDetails.model_validate(
            {"cache_write_tokens": 0, "cached_tokens": 20}
        ),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=15),
    )

    # Simulate adding items for turn 2 to increment turn counter
    await session.add_items([{"role": "user", "content": "Second turn"}])
    run_result_2 = create_mock_run_result(usage_data_2)
    await session.store_run_usage(run_result_2)

    # Test session-level usage aggregation
    session_usage = await session.get_session_usage()
    assert session_usage is not None
    assert session_usage["requests"] == 3  # 1 + 2
    assert session_usage["total_tokens"] == 200  # 80 + 120
    assert session_usage["input_tokens"] == 125  # 50 + 75
    assert session_usage["output_tokens"] == 75  # 30 + 45
    assert session_usage["total_turns"] == 2

    # Test turn-level usage retrieval
    turn_1_usage = await session.get_turn_usage(1)
    assert isinstance(turn_1_usage, dict)
    assert turn_1_usage["requests"] == 1
    assert turn_1_usage["total_tokens"] == 80
    assert turn_1_usage["input_tokens_details"]["cached_tokens"] == 10
    assert turn_1_usage["output_tokens_details"]["reasoning_tokens"] == 5

    turn_2_usage = await session.get_turn_usage(2)
    assert isinstance(turn_2_usage, dict)
    assert turn_2_usage["requests"] == 2
    assert turn_2_usage["total_tokens"] == 120
    assert turn_2_usage["input_tokens_details"]["cached_tokens"] == 20
    assert turn_2_usage["output_tokens_details"]["reasoning_tokens"] == 15

    # Test getting all turn usage
    all_turn_usage = await session.get_turn_usage()
    assert isinstance(all_turn_usage, list)
    assert len(all_turn_usage) == 2
    assert all_turn_usage[0]["user_turn_number"] == 1
    assert all_turn_usage[1]["user_turn_number"] == 2

    session.close()


async def test_failed_usage_write_rolls_back_cached_connection(usage_data: Usage):
    """A swallowed usage-write failure must not strand a transaction or SQLite lock."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "usage_rollback.db"
        session = AdvancedSQLiteSession(
            session_id="usage_rollback",
            db_path=db_path,
            create_tables=True,
        )
        await session.add_items([{"role": "user", "content": "turn"}])

        helper = session._get_connection()
        helper.execute(
            """
            CREATE TRIGGER fail_turn_usage
            BEFORE INSERT ON turn_usage
            BEGIN
                SELECT RAISE(ABORT, 'usage write failed');
            END
            """
        )
        helper.commit()

        await session.store_run_usage(create_mock_run_result(usage_data))

        assert all(not conn.in_transaction for conn in session._connections)
        probe = sqlite3.connect(str(db_path), timeout=0)
        try:
            probe.execute("CREATE TABLE usage_lock_probe (x INTEGER)")
            probe.commit()
        finally:
            probe.close()

        helper.execute("DROP TRIGGER fail_turn_usage")
        helper.commit()
        await session.store_run_usage(create_mock_run_result(usage_data))
        assert await session.get_turn_usage(1)
        session.close()


async def test_runner_integration_with_usage_tracking(agent: Agent):
    """Test integration with Runner and automatic usage tracking pattern."""
    session_id = "integration_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    async def store_session_usage(result: Any, session: AdvancedSQLiteSession):
        """Helper function to store usage after runner completes."""
        try:
            await session.store_run_usage(result)
        except Exception:
            # Ignore errors in test helper
            pass

    # Set up scripted model responses.
    assert isinstance(agent.model, ScriptedModel)
    scripted_model = agent.model
    scripted_model.enqueue([get_text_message("San Francisco")])

    # First turn
    result1 = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    assert result1.final_output == "San Francisco"
    await store_session_usage(result1, session)

    # Second turn
    scripted_model.enqueue([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"
    await store_session_usage(result2, session)

    # Verify conversation structure
    conversation_turns = await session.get_conversation_by_turns()
    assert len(conversation_turns) == 2

    # Verify usage was tracked
    session_usage = await session.get_session_usage()
    assert session_usage is not None
    assert session_usage["total_turns"] == 2
    # ScriptedModel doesn't generate realistic usage data, so we just check structure exists
    assert "requests" in session_usage
    assert "total_tokens" in session_usage

    session.close()


async def test_sequence_ordering():
    """Test that sequence ordering works correctly even with same timestamps."""
    session_id = "sequence_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add multiple items quickly to test sequence ordering
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Response 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Response 2"},
    ]
    await session.add_items(items)

    # Get items and verify order is preserved
    retrieved = await session.get_items()
    assert len(retrieved) == 4
    assert retrieved[0].get("content") == "Message 1"
    assert retrieved[1].get("content") == "Response 1"
    assert retrieved[2].get("content") == "Message 2"
    assert retrieved[3].get("content") == "Response 2"

    session.close()


async def test_conversation_structure_with_multiple_turns():
    """Test conversation structure tracking with multiple user turns."""
    session_id = "multi_turn_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Turn 1
    turn_1: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi!"},
    ]
    await session.add_items(turn_1)

    # Turn 2
    turn_2: list[TResponseInputItem] = [
        {"role": "user", "content": "How are you?"},
        {"type": "function_call", "name": "mood_check", "arguments": "{}"},  # type: ignore
        {"type": "function_call_output", "output": "I'm good"},  # type: ignore
        {"role": "assistant", "content": "I'm doing well!"},
    ]
    await session.add_items(turn_2)

    # Turn 3
    turn_3: list[TResponseInputItem] = [
        {"role": "user", "content": "Goodbye"},
        {"role": "assistant", "content": "See you later!"},
    ]
    await session.add_items(turn_3)

    # Verify conversation structure
    conversation_turns = await session.get_conversation_by_turns()
    assert len(conversation_turns) == 3

    # Turn 1 should have 2 items
    assert len(conversation_turns[1]) == 2
    assert conversation_turns[1][0]["type"] == "user"
    assert conversation_turns[1][1]["type"] == "assistant"

    # Turn 2 should have 4 items including tool calls
    assert len(conversation_turns[2]) == 4
    turn_2_types = [item["type"] for item in conversation_turns[2]]
    assert "user" in turn_2_types
    assert "function_call" in turn_2_types
    assert "function_call_output" in turn_2_types
    assert "assistant" in turn_2_types

    # Turn 3 should have 2 items
    assert len(conversation_turns[3]) == 2

    session.close()


async def test_empty_session_operations():
    """Test operations on empty sessions."""
    session_id = "empty_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test getting items from empty session
    items = await session.get_items()
    assert len(items) == 0

    # Test getting conversation from empty session
    conversation = await session.get_conversation_by_turns()
    assert len(conversation) == 0

    # Test getting tool usage from empty session
    tool_usage = await session.get_tool_usage()
    assert len(tool_usage) == 0

    # Test getting session usage from empty session
    session_usage = await session.get_session_usage()
    assert session_usage is None

    # Test getting turns from empty session
    turns = await session.get_conversation_turns()
    assert len(turns) == 0

    session.close()


async def test_json_serialization_edge_cases(usage_data: Usage):
    """Test edge cases in JSON serialization of usage data."""
    session_id = "json_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test with normal usage data (need to add user message first to create turn)
    await session.add_items([{"role": "user", "content": "First test"}])
    run_result_1 = create_mock_run_result(usage_data)
    await session.store_run_usage(run_result_1)

    # Test with None usage data
    run_result_none = create_mock_run_result(None)
    await session.store_run_usage(run_result_none)

    # Test with usage data missing details
    minimal_usage = Usage(
        requests=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    await session.add_items([{"role": "user", "content": "Second test"}])
    run_result_2 = create_mock_run_result(minimal_usage)
    await session.store_run_usage(run_result_2)

    # Verify we can retrieve the data
    turn_1_usage = await session.get_turn_usage(1)
    assert isinstance(turn_1_usage, dict)
    assert turn_1_usage["requests"] == 1
    assert turn_1_usage["input_tokens_details"]["cached_tokens"] == 10

    turn_2_usage = await session.get_turn_usage(2)
    assert isinstance(turn_2_usage, dict)
    assert turn_2_usage["requests"] == 1
    # Should have default values for minimal data (Usage class provides defaults)
    assert turn_2_usage["input_tokens_details"]["cached_tokens"] == 0
    assert turn_2_usage["output_tokens_details"]["reasoning_tokens"] == 0

    session.close()


async def test_session_isolation():
    """Test that different session IDs maintain separate data."""
    session1 = AdvancedSQLiteSession(session_id="session_1", create_tables=True)
    session2 = AdvancedSQLiteSession(session_id="session_2", create_tables=True)

    # Add data to session 1
    await session1.add_items([{"role": "user", "content": "Session 1 message"}])

    # Add data to session 2
    await session2.add_items([{"role": "user", "content": "Session 2 message"}])

    # Verify isolation
    session1_items = await session1.get_items()
    session2_items = await session2.get_items()

    assert len(session1_items) == 1
    assert len(session2_items) == 1
    assert session1_items[0].get("content") == "Session 1 message"
    assert session2_items[0].get("content") == "Session 2 message"

    # Test conversation structure isolation
    session1_turns = await session1.get_conversation_by_turns()
    session2_turns = await session2.get_conversation_by_turns()

    assert len(session1_turns) == 1
    assert len(session2_turns) == 1

    session1.close()
    session2.close()


async def test_error_handling_in_usage_tracking(usage_data: Usage):
    """Test that usage tracking errors don't break the main flow."""
    session_id = "error_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test normal operation
    run_result = create_mock_run_result(usage_data)
    await session.store_run_usage(run_result)

    # Close the session to simulate database errors
    session.close()

    # This should not raise an exception (error should be caught)
    await session.store_run_usage(run_result)


@pytest.mark.parametrize(
    ("model_redacted", "tool_redacted"),
    [(True, False), (False, True), (False, False)],
)
async def test_usage_tracking_failure_identity_follows_model_data_policy(
    usage_data: Usage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    model_redacted: bool,
    tool_redacted: bool,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)
    session_id = "SECRET_USAGE_SESSION_ID"
    test_logger = logging.getLogger("advanced-sqlite-usage-failure")
    session = AdvancedSQLiteSession(
        session_id=session_id,
        create_tables=True,
        logger=test_logger,
    )
    secret = "SECRET_USAGE_FAILURE"
    run_result = create_mock_run_result(usage_data)

    original_record_factory = logging.getLogRecordFactory()

    def application_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = original_record_factory(*args, **kwargs)
        record.session_id = "APPLICATION_SESSION_ID"
        return record

    logging.setLogRecordFactory(application_record_factory)
    try:
        with (
            patch.object(
                session,
                "_update_turn_usage_internal",
                side_effect=RuntimeError(secret),
            ),
            caplog.at_level(logging.ERROR, logger=test_logger.name),
        ):
            await session.store_run_usage(run_result)
    finally:
        logging.setLogRecordFactory(original_record_factory)

    record = next(
        record
        for record in caplog.records
        if "Failed to store session usage" in record.getMessage()
    )
    assert record.__dict__["session_id"] == "APPLICATION_SESSION_ID"
    if model_redacted:
        assert record.msg == "%s"
        assert record.args == ("Failed to store session usage",)
        assert record.exc_info is None
        assert "openai_agents_diagnostic_context" not in record.__dict__
        assert secret not in caplog.text
        assert session_id not in caplog.text
    else:
        assert record.__dict__["openai_agents_diagnostic_context"] == {"session_id": session_id}
        assert record.exc_info is not None
        assert record.exc_info[1] is not None
        assert secret in caplog.text

    session.close()


async def test_advanced_tool_name_extraction():
    """Test advanced tool name extraction for different tool types."""
    session_id = "advanced_tool_names_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items with various tool types and naming patterns
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Use various tools"},
        # MCP tools with server labels
        {"type": "mcp_call", "server_label": "filesystem", "name": "read_file", "arguments": "{}"},  # type: ignore
        {
            "type": "mcp_approval_request",
            "server_label": "database",
            "name": "execute_query",
            "arguments": "{}",
        },  # type: ignore
        # Built-in tool types
        {"type": "computer_call", "arguments": "{}"},  # type: ignore
        {"type": "file_search_call", "arguments": "{}"},  # type: ignore
        {"type": "web_search_call", "arguments": "{}"},  # type: ignore
        {"type": "code_interpreter_call", "arguments": "{}"},  # type: ignore
        # Regular function calls
        {"type": "function_call", "name": "calculator", "arguments": "{}"},  # type: ignore
        {"type": "custom_tool_call", "name": "custom_tool", "arguments": "{}"},  # type: ignore
    ]
    await session.add_items(items)

    # Get conversation structure and verify tool names
    conversation_turns = await session.get_conversation_by_turns()
    turn_items = conversation_turns[1]

    tool_items = [item for item in turn_items if item["tool_name"]]
    tool_names = [item["tool_name"] for item in tool_items]

    # Verify MCP tools get server_label.name format
    assert "filesystem.read_file" in tool_names
    assert "database.execute_query" in tool_names

    # Verify built-in tools use their type as name
    assert "computer_call" in tool_names
    assert "file_search_call" in tool_names
    assert "web_search_call" in tool_names
    assert "code_interpreter_call" in tool_names

    # Verify regular function calls use their name
    assert "calculator" in tool_names
    assert "custom_tool" in tool_names

    session.close()


async def test_branch_usage_tracking():
    """Test usage tracking across different branches."""
    session_id = "branch_usage_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items and usage to main branch
    await session.add_items([{"role": "user", "content": "Main question"}])
    usage_main = Usage(requests=1, input_tokens=50, output_tokens=30, total_tokens=80)
    run_result_main = create_mock_run_result(usage_main)
    await session.store_run_usage(run_result_main)

    # Create a branch and add usage there
    await session.create_branch_from_turn(1, "usage_branch")
    await session.add_items([{"role": "user", "content": "Branch question"}])
    usage_branch = Usage(requests=2, input_tokens=100, output_tokens=60, total_tokens=160)
    run_result_branch = create_mock_run_result(usage_branch)
    await session.store_run_usage(run_result_branch)

    # Test branch-specific usage
    main_usage = await session.get_session_usage(branch_id="main")
    assert main_usage is not None
    assert main_usage["requests"] == 1
    assert main_usage["total_tokens"] == 80
    assert main_usage["total_turns"] == 1

    branch_usage = await session.get_session_usage(branch_id="usage_branch")
    assert branch_usage is not None
    assert branch_usage["requests"] == 2
    assert branch_usage["total_tokens"] == 160
    assert branch_usage["total_turns"] == 1

    # Test total usage across all branches
    total_usage = await session.get_session_usage()
    assert total_usage is not None
    assert total_usage["requests"] == 3  # 1 + 2
    assert total_usage["total_tokens"] == 240  # 80 + 160
    assert total_usage["total_turns"] == 2

    # Test turn usage for specific branch
    branch_turn_usage = await session.get_turn_usage(branch_id="usage_branch")
    assert isinstance(branch_turn_usage, list)
    assert len(branch_turn_usage) == 1
    assert branch_turn_usage[0]["requests"] == 2

    session.close()


async def test_tool_name_extraction():
    """Test that tool names are correctly extracted from different item types."""
    session_id = "tool_names_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items with different ways of specifying tool names
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Use tools please"},  # Need user message to create turn
        {"type": "function_call", "name": "search_web", "arguments": "{}"},  # type: ignore
        {"type": "function_call_output", "tool_name": "search_web", "output": "result"},  # type: ignore
        {"type": "function_call", "name": "calculator", "arguments": "{}"},  # type: ignore
    ]
    await session.add_items(items)

    # Get conversation structure and verify tool names
    conversation_turns = await session.get_conversation_by_turns()
    turn_items = conversation_turns[1]

    tool_items = [item for item in turn_items if item["tool_name"]]
    tool_names = [item["tool_name"] for item in tool_items]

    assert "search_web" in tool_names
    assert "calculator" in tool_names

    session.close()


async def test_tool_execution_integration(agent: Agent):
    """Test integration with actual tool execution."""
    session_id = "tool_integration_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Set up the scripted model to trigger a tool call.
    scripted_model = cast(ScriptedModel, agent.model)
    scripted_model.enqueue(
        [
            {  # type: ignore
                "type": "function_call",
                "name": "test_tool",
                "arguments": '{"query": "test query"}',
                "call_id": "call_123",
            }
        ]
    )

    # Then set the final response
    scripted_model.enqueue([get_text_message("Tool executed successfully")])

    # Run the agent
    result = await Runner.run(
        agent,
        "Please use the test tool",
        session=session,
    )

    # Verify the tool was executed
    assert "Tool result for: test query" in str(result.new_items)

    # Verify tool usage was tracked
    tool_usage = await session.get_tool_usage()
    assert len(tool_usage) > 0

    session.close()


# ============================================================================
# SessionSettings Tests
# ============================================================================


async def test_session_settings_default():
    """Test that session_settings defaults to empty SessionSettings."""
    from agents.memory import SessionSettings

    session = AdvancedSQLiteSession(session_id="default_settings_test", create_tables=True)

    # Should have default SessionSettings (inherited from SQLiteSession)
    assert isinstance(session.session_settings, SessionSettings)
    assert session.session_settings.limit is None

    session.close()


@pytest.mark.parametrize("use_dictionary", [False, True], ids=["class", "dictionary"])
async def test_session_settings_constructor(use_dictionary: bool):
    """Test passing session_settings via constructor."""
    from agents.memory import SessionSettings

    session = AdvancedSQLiteSession(
        session_id="constructor_settings_test",
        create_tables=True,
        session_settings={"limit": 5} if use_dictionary else SessionSettings(limit=5),
    )

    assert isinstance(session.session_settings, SessionSettings)
    assert session.session_settings.limit == 5

    session.close()


async def test_get_items_uses_session_settings_limit():
    """Test that get_items uses session_settings.limit as default."""
    from agents.memory import SessionSettings

    session = AdvancedSQLiteSession(
        session_id="uses_settings_limit_test",
        create_tables=True,
        session_settings=SessionSettings(limit=3),
    )

    # Add 5 items
    items: list[TResponseInputItem] = [
        {"role": "user", "content": f"Message {i}"} for i in range(5)
    ]
    await session.add_items(items)

    # get_items() with no limit should use session_settings.limit=3
    retrieved = await session.get_items()
    assert len(retrieved) == 3
    # Should get the last 3 items
    assert retrieved[0].get("content") == "Message 2"
    assert retrieved[1].get("content") == "Message 3"
    assert retrieved[2].get("content") == "Message 4"

    session.close()


async def test_get_items_explicit_limit_overrides_session_settings():
    """Test that explicit limit parameter overrides session_settings."""
    from agents.memory import SessionSettings

    session = AdvancedSQLiteSession(
        session_id="explicit_override_test",
        create_tables=True,
        session_settings=SessionSettings(limit=5),
    )

    # Add 10 items
    items: list[TResponseInputItem] = [
        {"role": "user", "content": f"Message {i}"} for i in range(10)
    ]
    await session.add_items(items)

    # Explicit limit=2 should override session_settings.limit=5
    retrieved = await session.get_items(limit=2)
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Message 8"
    assert retrieved[1].get("content") == "Message 9"

    session.close()


async def test_get_items_limit_skips_corrupt_newest_rows():
    """limit counts valid items, expanding past corrupt newest rows."""
    session = AdvancedSQLiteSession(session_id="limit_corrupt_test", create_tables=True)

    await session.add_items(
        [
            {"role": "user", "content": "valid 0"},
            {"role": "assistant", "content": "valid 1"},
            {"role": "user", "content": "valid 2"},
        ]
    )

    # Append a corrupt newest row, with the branch structure the JOIN needs.
    conn = session._get_connection()
    cursor = conn.execute(
        f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
        (session.session_id, "not valid json {{{"),
    )
    next_sequence = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM message_structure "
        "WHERE session_id = ? AND branch_id = ?",
        (session.session_id, "main"),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO message_structure "
        "(session_id, message_id, branch_id, sequence_number, message_type, "
        "user_turn_number, branch_turn_number) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session.session_id, cursor.lastrowid, "main", next_sequence, "user", 1, 1),
    )
    conn.commit()

    limited = await session.get_items(limit=2)
    assert [item.get("content") for item in limited] == ["valid 1", "valid 2"]

    # The explicit-branch call resolves to the same rows.
    limited_explicit = await session.get_items(limit=2, branch_id="main")
    assert [item.get("content") for item in limited_explicit] == ["valid 1", "valid 2"]

    session.close()


async def test_get_items_limit_returns_fewer_when_history_exhausted():
    """Window expansion stops at the end of history instead of looping."""
    session = AdvancedSQLiteSession(session_id="limit_exhausted_test", create_tables=True)

    await session.add_items([{"role": "user", "content": "only valid"}])

    retrieved = await session.get_items(limit=5)
    assert [item.get("content") for item in retrieved] == ["only valid"]

    session.close()


async def test_session_settings_resolve():
    """Test SessionSettings.resolve() method."""
    from agents.memory import SessionSettings

    base = SessionSettings(limit=100)
    override = SessionSettings(limit=50)

    final = base.resolve(override)

    assert final.limit == 50  # Override wins
    assert base.limit == 100  # Original unchanged

    # Resolving with None returns self
    final_none = base.resolve(None)
    assert final_none.limit == 100


async def test_runner_with_session_settings_override(agent: Agent):
    """Test that RunConfig can override session's default settings."""
    from agents import RunConfig
    from agents.memory import SessionSettings

    # Session with default limit=100
    session = AdvancedSQLiteSession(
        session_id="runner_override_test",
        create_tables=True,
        session_settings=SessionSettings(limit=100),
    )

    # Add some history
    items: list[TResponseInputItem] = [{"role": "user", "content": f"Turn {i}"} for i in range(10)]
    await session.add_items(items)

    # Use RunConfig to override limit to 2
    assert isinstance(agent.model, ScriptedModel)
    agent.model.enqueue([get_text_message("Got it")])

    await Runner.run(
        agent,
        "New question",
        session=session,
        run_config=RunConfig(
            session_settings=SessionSettings(limit=2)  # Override to 2
        ),
    )

    # Verify the agent received only the last 2 history items + new question
    last_input = agent.model.calls[-1].input
    # Filter out the new "New question" input
    history_items = [item for item in last_input if item.get("content") != "New question"]
    # Should have 2 history items (last two from the 10 we added)
    assert len(history_items) == 2

    session.close()


async def test_concurrent_add_items_preserves_message_structure_for_file_db():
    """Concurrent add_items calls should keep agent_messages and message_structure aligned."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "advanced_concurrent.db"
        session = AdvancedSQLiteSession(
            session_id="advanced_concurrent",
            db_path=db_path,
            create_tables=True,
        )

        async def add_batch(worker_id: int) -> list[str]:
            contents = [f"worker-{worker_id}-message-{index}" for index in range(10)]
            await session.add_items([{"role": "user", "content": content} for content in contents])
            return contents

        expected_batches = await asyncio.gather(*(add_batch(worker_id) for worker_id in range(8)))
        expected_contents = {content for batch in expected_batches for content in batch}

        retrieved_items = await session.get_items()
        retrieved_contents = {
            content
            for item in retrieved_items
            for content in [item.get("content")]
            if isinstance(content, str)
        }

        assert retrieved_contents == expected_contents
        assert len(retrieved_items) == len(expected_contents)

        with session._locked_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.message_data
                FROM {session.messages_table} m
                JOIN message_structure s ON s.message_id = m.id
                WHERE m.session_id = ?
                ORDER BY s.sequence_number ASC
                """,
                (session.session_id,),
            ).fetchall()

        structured_contents = {json.loads(message_data).get("content") for (message_data,) in rows}

        assert structured_contents == expected_contents
        assert len(rows) == len(expected_contents)

        session.close()


async def test_output_tokens_details_persisted_when_input_details_missing():
    """Regression: output_tokens_details must persist even if input_tokens_details is None.

    Previously the output serialization branch was nested inside the input branch,
    silently dropping output_tokens_details whenever input_tokens_details was falsy
    (e.g., when a provider populated only output details).
    """
    session = AdvancedSQLiteSession(session_id="output_only_usage", create_tables=True)
    usage = Usage(
        requests=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=42),
    )
    # Mimic providers that bypass validation and leave input_tokens_details unset.
    object.__setattr__(usage, "input_tokens_details", None)

    await session.add_items([{"role": "user", "content": "hi"}])
    await session.store_run_usage(create_mock_run_result(usage))

    turn_usage = await session.get_turn_usage(1)
    assert isinstance(turn_usage, dict)
    assert turn_usage["output_tokens_details"] == {"reasoning_tokens": 42}
    assert turn_usage["input_tokens_details"] is None
    session.close()


def _count_rows(session: AdvancedSQLiteSession, table: str) -> int:
    """Helper: count rows for the session in one of the metadata tables."""
    with session._locked_connection() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
        return cast(int, row[0])


async def test_clear_session_removes_structure_and_usage_metadata(usage_data: Usage):
    """Regression: clear_session must also clear message_structure and turn_usage.

    Those tables declare an ON DELETE CASCADE foreign key, but SQLite does not
    enforce foreign keys by default, so the inherited base clear_session left the
    rows behind. That leaked stale structure/usage data and permanently offset
    sequence and turn numbering for items added after clearing.
    """
    session = AdvancedSQLiteSession(session_id="clear_metadata_test", create_tables=True)

    await session.add_items(
        [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
        ]
    )
    await session.store_run_usage(create_mock_run_result(usage_data))

    assert _count_rows(session, "message_structure") > 0
    assert _count_rows(session, "turn_usage") > 0

    await session.clear_session()

    assert await session.get_items() == []
    assert _count_rows(session, "message_structure") == 0
    assert _count_rows(session, "turn_usage") == 0

    # Numbering must reset: the next item starts a fresh sequence and turn.
    await session.add_items([{"role": "user", "content": "Fresh start"}])
    with session._locked_connection() as conn:
        rows = conn.execute(
            """
            SELECT sequence_number, user_turn_number
            FROM message_structure
            WHERE session_id = ?
            """,
            (session.session_id,),
        ).fetchall()
    assert rows == [(1, 1)]

    session.close()


async def test_pop_item_removes_its_structure_row():
    """Regression: pop_item must delete the popped message's structure row.

    The inherited base pop_item removed only the message row, leaving an orphaned
    message_structure row that corrupted later MAX(sequence_number)/turn numbering.
    """
    session = AdvancedSQLiteSession(session_id="pop_structure_test", create_tables=True)

    await session.add_items(
        [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]
    )

    popped = await session.pop_item()
    assert popped == {"role": "assistant", "content": "Answer"}

    with session._locked_connection() as conn:
        message_ids = {
            row[0]
            for row in conn.execute(
                f"SELECT id FROM {session.messages_table} WHERE session_id = ?",
                (session.session_id,),
            ).fetchall()
        }
        structure_message_ids = {
            row[0]
            for row in conn.execute(
                "SELECT message_id FROM message_structure WHERE session_id = ?",
                (session.session_id,),
            ).fetchall()
        }

    # No structure row may reference a message that no longer exists.
    assert structure_message_ids <= message_ids
    assert await session.get_items() == [{"role": "user", "content": "Question"}]

    session.close()


async def test_pop_item_removes_turn_usage_only_when_turn_emptied(usage_data: Usage):
    """Regression: pop_item must drop a turn's turn_usage row once the turn has no
    remaining items on the current branch, while keeping it for a partial pop.
    """
    session = AdvancedSQLiteSession(session_id="pop_turn_usage_test", create_tables=True)

    # One turn with two items, plus stored usage for that turn.
    await session.add_items(
        [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]
    )
    await session.store_run_usage(create_mock_run_result(usage_data))
    assert _count_rows(session, "turn_usage") == 1

    # Popping only the assistant item leaves the turn non-empty: usage is kept.
    await session.pop_item()
    assert _count_rows(session, "turn_usage") == 1

    # Popping the last item of the turn removes the now-stale usage row.
    await session.pop_item()
    assert _count_rows(session, "turn_usage") == 0
    assert not await session.get_turn_usage(1)

    session.close()


async def test_pop_item_respects_current_branch_and_keeps_shared_messages():
    """Regression: pop_item must pop from the current branch and preserve messages
    still referenced by another branch (branches share the underlying message rows).
    """
    session = AdvancedSQLiteSession(session_id="pop_branch_test", create_tables=True)

    main_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Main first question"},
        {"role": "assistant", "content": "Main first answer"},
        {"role": "user", "content": "Main second question"},
        {"role": "assistant", "content": "Main second answer"},
    ]

    try:
        await session.add_items(main_items)
        # Branch from turn 2 copies turn 1's shared messages into the new branch.
        await session.create_branch_from_turn(2, "branch_a")
        await session.switch_to_branch("branch_a")
        await session.add_items([{"role": "user", "content": "Branch-only question"}])

        # Popping on branch_a removes only its own newest item.
        popped = await session.pop_item()
        assert popped == {"role": "user", "content": "Branch-only question"}

        # The main branch, which shares turn 1's messages, is untouched.
        assert await session.get_items(branch_id="main") == main_items

        # No orphaned structure rows anywhere in the session.
        with session._locked_connection() as conn:
            message_ids = {
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM {session.messages_table} WHERE session_id = ?",
                    (session.session_id,),
                ).fetchall()
            }
            structure_message_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT message_id FROM message_structure WHERE session_id = ?",
                    (session.session_id,),
                ).fetchall()
            }
        assert structure_message_ids <= message_ids
    finally:
        session.close()


async def test_pop_item_deletes_shared_copied_message_only_when_unreferenced():
    """Regression: popping a message that was copied into a branch (branches share
    the underlying message row) must keep the message while another branch still
    references it, and only remove it once no branch references it anymore.
    """
    session = AdvancedSQLiteSession(session_id="pop_shared_copy_test", create_tables=True)

    main_items: list[TResponseInputItem] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]

    def message_count() -> int:
        with session._locked_connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {session.messages_table} WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            return cast(int, row[0])

    try:
        await session.add_items(main_items)
        # Branch from turn 2 copies turn 1 (u1, a1) into branch_a as shared rows.
        await session.create_branch_from_turn(2, "branch_a")
        await session.switch_to_branch("branch_a")
        await session.add_items([{"role": "user", "content": "branch-only"}])

        assert message_count() == 5  # u1, a1, u2, a2, branch-only

        # Pop the branch-only item (not shared): its message row is removed.
        assert await session.pop_item() == {"role": "user", "content": "branch-only"}
        assert message_count() == 4

        # Pop the copied, shared a1 and u1 off branch_a. They remain in the
        # messages table because the main branch still references them.
        assert await session.pop_item() == {"role": "assistant", "content": "a1"}
        assert await session.pop_item() == {"role": "user", "content": "u1"}
        assert message_count() == 4
        assert await session.get_items(branch_id="main") == main_items
        assert await session.get_items(branch_id="branch_a") == []

        # Now drain main: once no branch references u1/a1, the rows are removed.
        await session.switch_to_branch("main")
        for _ in range(len(main_items)):
            await session.pop_item()
        assert message_count() == 0
        assert await session.get_items() == []

        # No orphaned structure rows at any point.
        with session._locked_connection() as conn:
            leftover = conn.execute(
                "SELECT COUNT(*) FROM message_structure WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
        assert leftover == 0
    finally:
        session.close()


@contextlib.contextmanager
def _gate_worker(target_name: str):
    """Deterministically pause a session worker to control interleaving.

    Patches ``asyncio.to_thread`` in the session module so the first dispatch of
    a worker whose ``__name__`` equals ``target_name`` signals ``started`` and
    blocks on ``release`` before running. The pause happens before the worker
    acquires the connection lock, so other operations can run to completion
    while it is held. Yields ``(started, release)`` threading events.
    """
    started = threading.Event()
    release = threading.Event()
    real_to_thread = asyncio.to_thread
    state = {"gated": False}

    async def gated(func, /, *args, **kwargs):
        if not state["gated"] and getattr(func, "__name__", "") == target_name:
            state["gated"] = True
            started.set()
            await real_to_thread(release.wait)
        return await real_to_thread(func, *args, **kwargs)

    with patch(
        "agents.extensions.memory.advanced_sqlite_session.asyncio.to_thread",
        gated,
    ):
        yield started, real_to_thread, release


async def test_pop_item_uses_branch_snapshot_when_branch_switches_concurrently():
    """Regression: pop_item snapshots the current branch at call time, so a branch
    switch that interleaves after dispatch cannot redirect the pop to another branch.

    Uses a barrier (not sleep) to prove the ordering: the pop worker is held after
    its branch snapshot is taken while a full switch_to_branch("main") completes.
    """
    session = AdvancedSQLiteSession(session_id="pop_snapshot_test", create_tables=True)

    main_items: list[TResponseInputItem] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]

    try:
        await session.add_items(main_items)
        await session.create_branch_from_turn(2, "branch_a")
        await session.switch_to_branch("branch_a")
        await session.add_items([{"role": "user", "content": "branch-only"}])

        with _gate_worker("_pop_item_sync") as (started, real_to_thread, release):
            # pop_item snapshots _current_branch_id ("branch_a") synchronously,
            # then dispatches its worker, which parks at the barrier.
            task = asyncio.ensure_future(session.pop_item())
            await real_to_thread(started.wait)
            # Switch to main completes fully while the pop worker is parked.
            await session.switch_to_branch("main")
            release.set()
            popped = await task

        # The pop targeted branch_a (its state at call time), not main.
        assert popped == {"role": "user", "content": "branch-only"}
        assert await session.get_items(branch_id="main") == main_items
    finally:
        session.close()


@pytest.mark.review_optional
async def test_pop_item_claim_is_unique_across_processes(tmp_path: Path):
    """Two processes must not return the same destructively read item."""
    db_path = tmp_path / "advanced_pop_processes.db"
    session_id = "advanced_pop_processes"
    item: TResponseInputItem = {"role": "user", "content": "only"}
    setup = AdvancedSQLiteSession(session_id=session_id, db_path=db_path, create_tables=True)
    await setup.add_items([item])
    setup.close()

    context = _multiprocessing_context()
    start = context.Event()
    results = context.Queue()
    ready_events = [context.Event(), context.Event()]
    processes = [
        context.Process(
            target=_pop_item_in_process,
            args=(str(db_path), session_id, ready, start, results),
        )
        for ready in ready_events
    ]

    try:
        for process in processes:
            process.start()
        for ready in ready_events:
            assert ready.wait(timeout=30)
        start.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        outcomes = [results.get(timeout=5), results.get(timeout=5)]
        assert all(outcome[0] == "ok" for outcome in outcomes)
        popped_items = [outcome[1] for outcome in outcomes]
        assert popped_items.count(item) == 1
        assert popped_items.count(None) == 1
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()


async def test_stale_switch_after_clear_does_not_repoint_to_deleted_branch():
    """A switch_to_branch that commits its pointer after clear_session must not
    resurrect the deleted branch; the generation guard makes it a no-op.
    """
    session = AdvancedSQLiteSession(session_id="stale_switch_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
            ]
        )
        await session.create_branch_from_turn(2, "branch_a")
        await session.switch_to_branch("main")
        assert session._current_branch_id == "main"

        with _gate_worker("_commit_branch_pointer") as (started, real_to_thread, release):
            # switch validates branch_a and captures the generation, then parks
            # right before committing the pointer.
            task = asyncio.ensure_future(session.switch_to_branch("branch_a"))
            await real_to_thread(started.wait)
            # A full clear commits: it bumps the generation and resets to main.
            await session.clear_session()
            release.set()
            await task

        # The stale switch saw a newer generation and left the pointer on main.
        assert session._current_branch_id == "main"
        assert await session.get_items() == []
    finally:
        session.close()


async def test_stale_create_branch_after_clear_does_not_repoint():
    """A create_branch_from_turn that commits its pointer after clear_session must
    not point at the branch clear removed.
    """
    session = AdvancedSQLiteSession(session_id="stale_create_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
            ]
        )

        with _gate_worker("_commit_branch_pointer") as (started, real_to_thread, release):
            task = asyncio.ensure_future(session.create_branch_from_turn(2, "branch_b"))
            await real_to_thread(started.wait)
            await session.clear_session()
            release.set()
            await task

        # clear won: the pointer stays on main, not the wiped branch_b.
        assert session._current_branch_id == "main"
        assert await session.get_items() == []
    finally:
        session.close()


async def test_clear_before_branch_transaction_prevents_stale_reservation():
    """A clear that wins before transactional validation must leave no reservation."""
    session = AdvancedSQLiteSession(
        session_id="clear_before_branch_transaction_test",
        create_tables=True,
    )

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
            ]
        )

        with _gate_worker("_copy_sync") as (started, real_to_thread, release):
            task = asyncio.ensure_future(session.create_branch_from_turn(1, "stale_branch"))
            await real_to_thread(started.wait)
            await session.clear_session()
            release.set()
            with pytest.raises(ValueError, match="does not contain a user message"):
                await task

        assert session._current_branch_id == "main"
        assert await session.list_branches() == []
        with session._locked_connection() as conn:
            reservations = conn.execute(
                "SELECT branch_id FROM branch_reservations WHERE session_id = ?",
                (session.session_id,),
            ).fetchall()
        assert reservations == [("main",)]
    finally:
        session.close()


async def test_stale_store_run_usage_skipped_when_turn_removed_by_pop(usage_data: Usage):
    """A store_run_usage that reads a turn and then races with pop_item removing
    that turn must not reinsert usage for the now-nonexistent turn.
    """
    session = AdvancedSQLiteSession(session_id="stale_usage_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        result = create_mock_run_result(usage_data)

        with _gate_worker("_update_sync") as (started, real_to_thread, release):
            # store_run_usage reads current_turn (1) and captures the turn-usage
            # version, then parks before writing turn_usage.
            task = asyncio.ensure_future(session.store_run_usage(result))
            await real_to_thread(started.wait)
            # Pop both items of turn 1 so the turn no longer exists.
            await session.pop_item()
            await session.pop_item()
            release.set()
            await task

        # The stale usage write was skipped: no row for the removed turn.
        assert _count_rows(session, "turn_usage") == 0
    finally:
        session.close()


async def test_stale_store_run_usage_not_recorded_against_reused_turn_number(
    usage_data: Usage,
):
    """A store_run_usage that read turn N must not record its usage when that turn
    is popped and a *new* turn later reuses the same numeric id (the ABA case).

    An existence-only guard would pass here because turn 1 exists again; the
    turn-usage version counter invalidates the stale write.
    """
    session = AdvancedSQLiteSession(session_id="stale_usage_aba_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
            ]
        )
        result = create_mock_run_result(usage_data)

        with _gate_worker("_update_sync") as (started, real_to_thread, release):
            # Reads current_turn (1), captures the turn anchor, parks before write.
            task = asyncio.ensure_future(session.store_run_usage(result))
            await real_to_thread(started.wait)
            # Remove turn 1 entirely, then create a brand-new turn that reuses the
            # numeric id 1.
            await session.pop_item()
            await session.pop_item()
            await session.add_items([{"role": "user", "content": "fresh turn"}])
            release.set()
            await task

        # The new turn 1 must not carry the previous run's usage.
        assert _count_rows(session, "turn_usage") == 0
        assert not await session.get_turn_usage(1)
    finally:
        session.close()


async def test_store_run_usage_survives_unrelated_branch_deletion(usage_data: Usage):
    """A store_run_usage in flight must not be dropped when an unrelated turn is
    removed (e.g. delete_branch on a non-current branch). The invalidation is
    scoped to the captured branch/turn, so the write still lands.
    """
    session = AdvancedSQLiteSession(session_id="usage_scope_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
            ]
        )
        # A separate branch that shares turn 1's messages; deleting it must not
        # affect usage captured for the current (main) branch.
        await session.create_branch_from_turn(2, "side_branch")
        await session.switch_to_branch("main")
        result = create_mock_run_result(usage_data)

        with _gate_worker("_update_sync") as (started, real_to_thread, release):
            # Captures main/turn 2 and its anchor, then parks before the write.
            task = asyncio.ensure_future(session.store_run_usage(result))
            await real_to_thread(started.wait)
            # Delete an unrelated branch while the usage write is parked.
            await session.delete_branch("side_branch")
            release.set()
            await task

        # The write landed: main's turn 2 usage is recorded despite the deletion.
        assert _count_rows(session, "turn_usage") == 1
        turn_2_usage = await session.get_turn_usage(2)
        assert isinstance(turn_2_usage, dict)
        assert turn_2_usage["total_tokens"] == usage_data.total_tokens
    finally:
        session.close()


async def test_clear_session_resets_current_branch_to_main():
    """Regression: clear_session must reset the in-memory branch pointer to 'main'
    (inside the locked operation) since every branch was removed.
    """
    session = AdvancedSQLiteSession(session_id="clear_branch_reset_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
            ]
        )
        await session.create_branch_from_turn(2, "branch_a")
        await session.switch_to_branch("branch_a")
        assert session._current_branch_id == "branch_a"
        assert _count_rows(session, "branch_reservations") == 2

        await session.clear_session()

        assert session._current_branch_id == "main"
        assert await session.get_items() == []
        assert _count_rows(session, "branch_reservations") == 2
    finally:
        session.close()


async def test_external_clear_resets_stale_branch_before_next_write(tmp_path: Path):
    """A second instance's clear must prevent stale branch resurrection."""
    db_path = tmp_path / "external_clear_generation.db"
    stale = AdvancedSQLiteSession(
        session_id="external_clear_generation",
        db_path=db_path,
        create_tables=True,
    )
    clearer = AdvancedSQLiteSession(
        session_id="external_clear_generation",
        db_path=db_path,
    )

    try:
        await stale.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ]
        )
        await stale.create_branch_from_turn(2, "stale")
        assert stale._current_branch_id == "stale"

        await clearer.clear_session()
        await stale.add_items([{"role": "user", "content": "after clear"}])

        assert stale._current_branch_id == "main"
        assert [item.get("content") for item in await stale.get_items()] == ["after clear"]
        assert await stale.get_items(branch_id="stale") == []
        assert {branch["branch_id"] for branch in await stale.list_branches()} == {"main"}
    finally:
        stale.close()
        clearer.close()


async def test_external_clear_resets_stale_branch_before_pop(tmp_path: Path):
    """A stale instance must pop the current main tail after an external clear."""
    db_path = tmp_path / "external_clear_pop_generation.db"
    stale = AdvancedSQLiteSession(
        session_id="external_clear_pop_generation",
        db_path=db_path,
        create_tables=True,
    )
    clearer = AdvancedSQLiteSession(
        session_id="external_clear_pop_generation",
        db_path=db_path,
    )

    try:
        await stale.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ]
        )
        await stale.create_branch_from_turn(2, "stale")
        assert stale._current_branch_id == "stale"

        await clearer.clear_session()
        item: TResponseInputItem = {"role": "user", "content": "after clear"}
        await clearer.add_items([item])

        assert await stale.pop_item() == item
        assert stale._current_branch_id == "main"
        assert await clearer.get_items() == []
    finally:
        stale.close()
        clearer.close()


@pytest.mark.parametrize(
    "read_path",
    ["items", "turns", "search", "conversation", "tools", "usage", "branches"],
)
async def test_external_clear_resets_stale_branch_before_default_reads(
    tmp_path: Path,
    usage_data: Usage,
    read_path: str,
):
    """Default reads must recover from a stale branch pointer after an external clear."""
    db_path = tmp_path / f"external_clear_read_generation_{read_path}.db"
    session_id = f"external_clear_read_generation_{read_path}"
    stale = AdvancedSQLiteSession(
        session_id=session_id,
        db_path=db_path,
        create_tables=True,
    )
    clearer = AdvancedSQLiteSession(session_id=session_id, db_path=db_path)

    try:
        await stale.add_items(
            [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "old follow-up"},
            ]
        )
        await stale.create_branch_from_turn(2, "stale")
        assert stale._current_branch_id == "stale"

        await clearer.clear_session()
        new_items: list[TResponseInputItem] = [
            {"role": "user", "content": "new main question"},
            {
                "type": "function_call",
                "name": "lookup",
                "arguments": '{"query": "new"}',
                "call_id": "lookup-new-main",
            },
            {"role": "assistant", "content": "new main answer"},
        ]
        await clearer.add_items(new_items)
        await clearer.store_run_usage(create_mock_run_result(usage_data))

        if read_path == "items":
            assert await stale.get_items() == new_items
        elif read_path == "turns":
            assert [turn["full_content"] for turn in await stale.get_conversation_turns()] == [
                "new main question"
            ]
        elif read_path == "search":
            assert [turn["full_content"] for turn in await stale.find_turns_by_content("new")] == [
                "new main question"
            ]
        elif read_path == "conversation":
            assert set(await stale.get_conversation_by_turns()) == {1}
        elif read_path == "tools":
            assert await stale.get_tool_usage() == [("lookup", 1, 1)]
        elif read_path == "usage":
            assert await stale.get_turn_usage(1) == {
                "requests": 1,
                "input_tokens": 50,
                "output_tokens": 30,
                "total_tokens": 80,
                "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 10},
                "output_tokens_details": {"reasoning_tokens": 5},
            }
        else:
            assert [
                (branch["branch_id"], branch["is_current"])
                for branch in await stale.list_branches()
            ] == [("main", True)]

        assert stale._current_branch_id == "main"
    finally:
        stale.close()
        clearer.close()


async def test_default_read_does_not_initialize_clear_generation_table(tmp_path: Path):
    """Reading a legacy database must not create the clear-generation table."""
    session = AdvancedSQLiteSession(
        session_id="legacy_generation_read",
        db_path=tmp_path / "legacy_generation_read.db",
        create_tables=True,
    )

    try:
        await session.add_items([{"role": "user", "content": "legacy history"}])
        with session._locked_connection() as conn:
            conn.execute("DROP TABLE session_clear_generations")
            conn.commit()

        assert await session.get_items() == [{"role": "user", "content": "legacy history"}]

        with session._locked_connection() as conn:
            table_exists = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'session_clear_generations'
                """
            ).fetchone()
        assert table_exists is None
    finally:
        session.close()


async def test_switch_validation_cancellation_waits_for_generation_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Legacy generation initialization must settle before cancellation propagates."""

    class PausingCommitConnection(sqlite3.Connection):
        pause_commit = False
        commit_finished = threading.Event()
        allow_return = threading.Event()

        def commit(self) -> None:
            super().commit()
            if self.pause_commit:
                self.pause_commit = False
                self.commit_finished.set()
                assert self.allow_return.wait(timeout=10)

    db_path = tmp_path / "switch_validation_cancellation.db"
    session = AdvancedSQLiteSession(
        session_id="switch_validation_cancellation",
        db_path=db_path,
        create_tables=True,
    )
    mutation: asyncio.Task[Any] | None = None

    try:
        await session.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ]
        )
        await session.create_branch_from_turn(2, "target")
        await session.switch_to_branch("main")
        with session._write_connection() as setup_connection:
            setup_connection.execute("DROP TABLE session_clear_generations")
            setup_connection.commit()

        connection = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            factory=PausingCommitConnection,
        )
        with session._connections_lock:
            session._connections.add(connection)
        monkeypatch.setattr(session, "_get_connection", lambda: connection)
        connection.pause_commit = True

        mutation = asyncio.create_task(session.switch_to_branch("target"))
        assert await asyncio.to_thread(connection.commit_finished.wait, 10)
        mutation.cancel("first-caller-cancel")
        await asyncio.sleep(0)
        mutation.cancel("second-caller-cancel")
        await asyncio.sleep(0)
        assert mutation.done() is False
        connection.allow_return.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await mutation

        _assert_cancel_message(exc_info.value, "first-caller-cancel")
        assert session._current_branch_id == "main"
        row = connection.execute(
            "SELECT generation FROM session_clear_generations WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
        assert row == (0,)
    finally:
        PausingCommitConnection.allow_return.set()
        if mutation is not None and not mutation.done():
            mutation.cancel()
            await asyncio.gather(mutation, return_exceptions=True)
        session.close()


async def test_post_clear_switch_synchronizes_generation_before_next_write(tmp_path: Path):
    """A new instance may select and write to a branch created after an earlier clear."""
    db_path = tmp_path / "post_clear_branch_switch.db"
    owner = AdvancedSQLiteSession(
        session_id="post_clear_branch_switch",
        db_path=db_path,
        create_tables=True,
    )
    other = AdvancedSQLiteSession(
        session_id="post_clear_branch_switch",
        db_path=db_path,
    )

    try:
        await owner.clear_session()
        await owner.add_items(
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ]
        )
        await owner.create_branch_from_turn(2, "fresh")

        await other.switch_to_branch("fresh")
        await other.add_items([{"role": "assistant", "content": "on fresh"}])

        assert other._current_branch_id == "fresh"
        assert [item.get("content") for item in await other.get_items()] == [
            "u1",
            "a1",
            "on fresh",
        ]
        assert [item.get("content") for item in await other.get_items(branch_id="main")] == [
            "u1",
            "a1",
            "u2",
        ]
    finally:
        owner.close()
        other.close()


async def test_pop_item_rolls_back_on_failure_after_earlier_delete():
    """Regression: a failure partway through pop_item's delete sequence must
    roll back so no partial mutation or open transaction survives.

    _locked_connection() does not manage transactions itself, so pop_item must
    roll back explicitly on failure. Otherwise the message_structure delete
    that already ran would remain pending in an open transaction for whatever
    the connection does next (on this thread) to inherit and possibly commit.
    """
    session = AdvancedSQLiteSession(session_id="pop_rollback_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "Answer"},
            ]
        )

        message_count_before = _count_rows(session, session.messages_table)
        structure_count_before = _count_rows(session, "message_structure")
        branch_before = session._current_branch_id

        # Fail the step that runs immediately after the message_structure
        # delete, simulating a failure after an earlier delete has executed.
        with patch.object(
            session,
            "_cleanup_orphaned_messages_sync",
            side_effect=RuntimeError("Simulated failure after earlier delete"),
        ):
            with pytest.raises(RuntimeError, match="Simulated failure"):
                await session.pop_item()

        # The message_structure delete that ran before the injected failure
        # must have been rolled back: nothing was actually removed.
        assert _count_rows(session, session.messages_table) == message_count_before
        assert _count_rows(session, "message_structure") == structure_count_before
        assert session._current_branch_id == branch_before
        with session._locked_connection() as conn:
            assert conn.in_transaction is False

        # The connection must be left clean for a subsequent legitimate pop.
        popped = await session.pop_item()
        assert popped == {"role": "assistant", "content": "Answer"}
        assert await session.get_items() == [{"role": "user", "content": "Question"}]
    finally:
        session.close()


async def test_clear_session_rolls_back_on_failure_after_earlier_delete(usage_data: Usage):
    """Regression: a failure partway through clear_session's delete sequence
    must roll back so no partial mutation or open transaction survives.

    _locked_connection() does not manage transactions itself, so clear_session
    must roll back explicitly. Otherwise a failure after the first deletes
    would leave those deletes pending in an open transaction, and the branch
    pointer / generation reset (which only happens after a successful commit)
    could drift out of sync with what's actually persisted.
    """
    session = AdvancedSQLiteSession(session_id="clear_rollback_test", create_tables=True)

    try:
        await session.add_items(
            [
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
            ]
        )
        await session.store_run_usage(create_mock_run_result(usage_data))

        message_count_before = _count_rows(session, session.messages_table)
        structure_count_before = _count_rows(session, "message_structure")
        usage_count_before = _count_rows(session, "turn_usage")
        assert structure_count_before > 0
        assert usage_count_before > 0
        branch_before = session._current_branch_id
        generation_before = session._generation

        real_conn = session._shared_connection

        class _FailOnTurnUsageDelete:
            """Delegates to the real connection but fails the turn_usage
            delete, simulating a failure after the earlier deletes in
            clear_session have already executed against this connection."""

            def execute(self, sql, parameters=()):
                if "DELETE FROM turn_usage" in sql:
                    raise RuntimeError("Simulated failure after earlier deletes")
                return real_conn.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        session._shared_connection = _FailOnTurnUsageDelete()  # type: ignore
        try:
            with pytest.raises(RuntimeError, match="Simulated failure"):
                await session.clear_session()
        finally:
            session._shared_connection = real_conn

        # The earlier deletes (messages, sessions, message_structure) that ran
        # before the injected failure must have been rolled back too.
        assert _count_rows(session, session.messages_table) == message_count_before
        assert _count_rows(session, "message_structure") == structure_count_before
        assert _count_rows(session, "turn_usage") == usage_count_before
        assert real_conn.in_transaction is False
        # In-memory state is only updated after a successful commit, so it
        # must be untouched when the commit never happened.
        assert session._current_branch_id == branch_before
        assert session._generation == generation_before

        # The connection must be left clean for a subsequent legitimate clear.
        await session.clear_session()
        assert _count_rows(session, "message_structure") == 0
        assert _count_rows(session, "turn_usage") == 0
        assert await session.get_items() == []
    finally:
        session.close()


async def test_structure_tables_reject_a_second_base_table_pair(tmp_path: Path) -> None:
    """A second base-table pair in one file would read the first pair's structure rows."""
    db_path = tmp_path / "advanced_shared_structure.db"
    first = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=True,
        sessions_table="first_sessions",
        messages_table="first_messages",
    )
    try:
        await first.add_items([{"role": "user", "content": "first"}])

        with pytest.raises(ValueError, match="first_sessions"):
            AdvancedSQLiteSession(
                session_id="shared",
                db_path=db_path,
                create_tables=True,
                sessions_table="second_sessions",
                messages_table="second_messages",
            )

        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            rejected_objects = conn.execute("""
                SELECT name FROM sqlite_master
                WHERE name IN (
                    'second_sessions',
                    'second_messages',
                    'idx_second_messages_session_id'
                )
            """).fetchall()
        assert rejected_objects == []
        assert await first.get_items() == [{"role": "user", "content": "first"}]
    finally:
        first.close()


async def test_structure_tables_reject_changed_sessions_with_shared_messages(
    tmp_path: Path,
) -> None:
    """Canonicalization must not replace a changed caller-selected sessions table."""
    db_path = tmp_path / "advanced_shared_messages.db"
    first = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=True,
        sessions_table="first_sessions",
        messages_table="shared_messages",
    )
    try:
        await first.add_items([{"role": "user", "content": "first"}])

        with pytest.raises(ValueError, match="first_sessions"):
            AdvancedSQLiteSession(
                session_id="shared",
                db_path=db_path,
                create_tables=True,
                sessions_table="second_sessions",
                messages_table="shared_messages",
            )

        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'second_sessions'"
                ).fetchall()
                == []
            )
        assert await first.get_items() == [{"role": "user", "content": "first"}]
    finally:
        first.close()


async def test_structure_tables_reject_changed_messages_with_shared_sessions(
    tmp_path: Path,
) -> None:
    """A changed messages table must not share structure rows under one sessions table."""
    db_path = tmp_path / "advanced_shared_sessions.db"
    first = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=True,
        sessions_table="shared_sessions",
        messages_table="first_messages",
    )
    try:
        await first.add_items([{"role": "user", "content": "first"}])

        with pytest.raises(ValueError, match="first_messages"):
            AdvancedSQLiteSession(
                session_id="shared",
                db_path=db_path,
                create_tables=True,
                sessions_table="shared_sessions",
                messages_table="second_messages",
            )

        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            rejected_objects = conn.execute("""
                SELECT name FROM sqlite_master
                WHERE name IN (
                    'second_messages',
                    'idx_second_messages_session_id'
                )
            """).fetchall()
        assert rejected_objects == []
        assert await first.get_items() == [{"role": "user", "content": "first"}]
    finally:
        first.close()


async def test_structure_tables_accept_equivalent_identifier_casing(tmp_path: Path) -> None:
    """SQLite resolves table names case-insensitively, so a recased pair is the same pair."""
    db_path = tmp_path / "advanced_recased_structure.db"
    first = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=True,
        sessions_table="FooSessions",
        messages_table="FooMessages",
    )
    try:
        await first.add_items([{"role": "user", "content": "first"}])
    finally:
        first.close()

    recased = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=True,
        sessions_table="foosessions",
        messages_table="foomessages",
    )
    try:
        assert await recased.get_items() == [{"role": "user", "content": "first"}]
    finally:
        recased.close()


@pytest.mark.parametrize("create_tables", [False, True])
async def test_structure_tables_accept_quoted_custom_session_table(
    tmp_path: Path, create_tables: bool
) -> None:
    """Released SQLiteSession accepts SQL-quoted custom session-table identifiers."""
    db_path = tmp_path / "advanced_quoted_session_table.db"
    sessions_table = '"quoted_sessions"'
    messages_table = "quoted_messages"

    if not create_tables:
        setup = AdvancedSQLiteSession(
            session_id="shared",
            db_path=db_path,
            create_tables=True,
            sessions_table=sessions_table,
            messages_table=messages_table,
        )
        setup.close()

    session = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=create_tables,
        sessions_table=sessions_table,
        messages_table=messages_table,
    )
    try:
        await session.add_items([{"role": "user", "content": "quoted"}])
        assert await session.get_items() == [{"role": "user", "content": "quoted"}]
    finally:
        session.close()


async def test_identifier_resolution_leaves_connection_authorized() -> None:
    """Temporary ownership resolution must not deny later SQL on supported Python versions."""
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE FooSessions (session_id TEXT PRIMARY KEY)")
        assert AdvancedSQLiteSession._resolve_table_identifier(conn, '"foosessions"') == (
            "FooSessions"
        )
        assert conn.execute("SELECT 1").fetchone() == (1,)


async def test_structure_tables_reject_distinct_non_ascii_identifiers(tmp_path: Path) -> None:
    """SQLite folds identifiers with ASCII rules, so these are two different pairs.

    Python's `casefold()` equates `ßsessions` and `sssessions`, which would let the second pair
    through and restore the cross-table mixing this change prevents.
    """
    assert "ßsessions".casefold() == "sssessions".casefold()

    db_path = tmp_path / "advanced_non_ascii_structure.db"
    first = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=True,
        sessions_table="ßsessions",
        messages_table="ßmessages",
    )
    try:
        await first.add_items([{"role": "user", "content": "first"}])

        with pytest.raises(ValueError, match="ßsessions"):
            AdvancedSQLiteSession(
                session_id="shared",
                db_path=db_path,
                create_tables=True,
                sessions_table="sssessions",
                messages_table="ssmessages",
            )

        assert await first.get_items() == [{"role": "user", "content": "first"}]
    finally:
        first.close()


async def test_no_create_session_rejects_a_database_without_an_owner(tmp_path: Path) -> None:
    """A no-create session must not open a file before a pair has claimed the structure tables.

    Accepting it would let another pair claim the tables afterwards, leaving this session writing
    and reading structure rows owned by that other pair.
    """
    db_path = tmp_path / "advanced_unclaimed_structure.db"

    with pytest.raises(ValueError, match="create_tables=True"):
        AdvancedSQLiteSession(session_id="shared", db_path=db_path, create_tables=False)

    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []

    owner = AdvancedSQLiteSession(session_id="shared", db_path=db_path, create_tables=True)
    try:
        await owner.add_items([{"role": "user", "content": "first"}])
    finally:
        owner.close()

    # Once a pair owns the layout, the same pair may open it without creating anything.
    reader = AdvancedSQLiteSession(session_id="shared", db_path=db_path, create_tables=False)
    try:
        assert await reader.get_items() == [{"role": "user", "content": "first"}]
    finally:
        reader.close()


async def test_no_create_session_rejects_owner_metadata_without_base_tables(
    tmp_path: Path,
) -> None:
    """Complete-looking owner metadata cannot substitute for the configured base tables."""
    db_path = tmp_path / "advanced_missing_base_tables.db"
    _create_owner_bearing_structure_tables(
        db_path,
        create_base_tables=False,
        message_foreign_keys="FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id), "
        "FOREIGN KEY (message_id) REFERENCES agent_messages(id)",
        usage_foreign_key="FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)",
    )

    with pytest.raises(ValueError, match="configured base tables"):
        AdvancedSQLiteSession(session_id="shared", db_path=db_path, create_tables=False)

    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        base_tables = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE name IN ('agent_sessions', 'agent_messages')
        """).fetchall()
    assert base_tables == []


@pytest.mark.parametrize("create_tables", [False, True])
async def test_structure_tables_reject_an_ownerless_layout(
    tmp_path: Path, create_tables: bool
) -> None:
    """An existing structure table without owner foreign keys is not a usable layout."""
    db_path = tmp_path / "advanced_ownerless_structure.db"
    _create_owner_bearing_structure_tables(db_path)

    with pytest.raises(ValueError, match="exactly one owner foreign key"):
        AdvancedSQLiteSession(
            session_id="shared",
            db_path=db_path,
            create_tables=create_tables,
        )


@pytest.mark.parametrize("create_tables", [False, True])
@pytest.mark.parametrize(
    ("message_foreign_keys", "usage_foreign_key", "error"),
    [
        (
            "FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id), "
            "FOREIGN KEY (session_id) REFERENCES wrong_sessions(session_id), "
            "FOREIGN KEY (message_id) REFERENCES agent_messages(id)",
            "FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)",
            "exactly one owner foreign key",
        ),
        (
            "FOREIGN KEY (session_id) REFERENCES agent_sessions(wrong_id), "
            "FOREIGN KEY (message_id) REFERENCES agent_messages(id)",
            "FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)",
            "already belongs",
        ),
        (
            "FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id), "
            "FOREIGN KEY (message_id) REFERENCES agent_messages(id)",
            "FOREIGN KEY (session_id) REFERENCES wrong_sessions(session_id)",
            "already belongs",
        ),
    ],
)
async def test_structure_tables_reject_malformed_owner_layouts(
    tmp_path: Path,
    create_tables: bool,
    message_foreign_keys: str,
    usage_foreign_key: str,
    error: str,
) -> None:
    """Owner tables must have exactly one complete foreign-key signature per owner."""
    db_path = tmp_path / "advanced_malformed_structure.db"
    _create_owner_bearing_structure_tables(
        db_path,
        message_foreign_keys=message_foreign_keys,
        usage_foreign_key=usage_foreign_key,
    )

    with pytest.raises(ValueError, match=error):
        AdvancedSQLiteSession(
            session_id="shared",
            db_path=db_path,
            create_tables=create_tables,
        )


@pytest.mark.parametrize("create_tables", [False, True])
async def test_structure_tables_accept_equivalent_child_column_casing(
    tmp_path: Path, create_tables: bool
) -> None:
    """SQLite resolves the child and referenced sides of foreign keys identically."""
    db_path = tmp_path / "advanced_recased_owner_columns.db"
    _create_owner_bearing_structure_tables(
        db_path,
        message_session_column="SESSION_ID",
        message_id_column="MESSAGE_ID",
        usage_session_column="SESSION_ID",
        message_foreign_keys="FOREIGN KEY (SESSION_ID) REFERENCES agent_sessions(session_id), "
        "FOREIGN KEY (MESSAGE_ID) REFERENCES agent_messages(id)",
        usage_foreign_key="FOREIGN KEY (SESSION_ID) REFERENCES agent_sessions(session_id)",
    )

    session = AdvancedSQLiteSession(
        session_id="shared",
        db_path=db_path,
        create_tables=create_tables,
    )
    session.close()


@pytest.mark.review_optional
async def test_concurrent_structure_table_claims_leave_one_coherent_owner(tmp_path: Path) -> None:
    """Two processes claiming a fresh file with different pairs must not split the layout."""
    db_path = tmp_path / "advanced_concurrent_claim.db"
    pairs = [("a_sessions", "a_messages"), ("b_sessions", "b_messages")]

    context = _multiprocessing_context()
    start = context.Event()
    results = context.Queue()
    ready_events = [context.Event(), context.Event()]
    processes = [
        context.Process(
            target=_claim_structure_tables_in_process,
            args=(str(db_path), sessions_table, messages_table, ready, start, results),
        )
        for (sessions_table, messages_table), ready in zip(pairs, ready_events, strict=False)
    ]

    try:
        for process in processes:
            process.start()
        for ready in ready_events:
            assert ready.wait(timeout=30)
        start.set()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0

        outcomes = [results.get(timeout=5), results.get(timeout=5)]
        claimed = [pair for status, pair in outcomes if status == "claimed"]
        assert len(claimed) == 1, outcomes
        assert all(status in {"claimed", "rejected"} for status, _ in outcomes), outcomes

        # Every owner-bearing structure table must name the one pair that won.
        winner_sessions, winner_messages = claimed[0]
        loser_sessions, loser_messages = next(pair for pair in pairs if pair != claimed[0])
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            structure_owners = {
                row[3]: row[2] for row in conn.execute("PRAGMA foreign_key_list(message_structure)")
            }
            usage_owners = {
                row[3]: row[2] for row in conn.execute("PRAGMA foreign_key_list(turn_usage)")
            }
            assert structure_owners == {
                "session_id": winner_sessions,
                "message_id": winner_messages,
            }
            assert usage_owners == {"session_id": winner_sessions}
            rejected_objects = conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN (?, ?, ?)",
                (
                    loser_sessions,
                    loser_messages,
                    f"idx_{loser_messages}_session_id",
                ),
            ).fetchall()
            assert rejected_objects == []
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

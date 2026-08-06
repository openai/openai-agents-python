from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from ..items import TResponseInputItem
from .session import SessionABC
from .session_settings import SessionSettings, coerce_session_settings, resolve_session_limit

_T = TypeVar("_T")


async def _await_mutation(awaitable: Awaitable[_T]) -> _T:
    """Wait for a mutation outcome despite repeated caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation from None
        raise
    if cancellation is not None:
        raise cancellation from None
    return result


class SQLiteSession(SessionABC):
    """SQLite-based implementation of session storage.

    This implementation stores conversation history in a SQLite database.
    By default, uses an in-memory database that is lost when the process ends.
    For persistent storage, provide a file path.
    """

    session_settings: SessionSettings | None = None
    _file_locks: ClassVar[dict[Path, threading.RLock]] = {}
    _file_lock_counts: ClassVar[dict[Path, int]] = {}
    _file_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        session_id: str,
        db_path: str | Path = ":memory:",
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ):
        """Initialize the SQLite session.

        Args:
            session_id: Unique identifier for the conversation session
            db_path: Path to the SQLite database file. Defaults to ':memory:' (in-memory database)
            sessions_table: Name of the table to store session metadata. Defaults to
                'agent_sessions'
            messages_table: Name of the table to store message data. Defaults to 'agent_messages'
            session_settings: Session configuration settings including default limit for
                retrieving items. If None, uses default SessionSettings().
        """
        self.session_id = session_id
        self.session_settings = (
            coerce_session_settings(session_settings)
            if session_settings is not None
            else SessionSettings()
        )
        self.db_path = db_path
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._quarantined_connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        self._closed = False

        # For in-memory databases, we need a shared connection to avoid thread isolation
        # For file databases, we use thread-local connections for better concurrency
        self._is_memory_db = str(db_path) == ":memory:"
        self._lock_path: Path | None = None
        self._lock_released = False
        if self._is_memory_db:
            self._lock = threading.RLock()
        else:
            self._lock_path, self._lock = self._acquire_file_lock(Path(self.db_path))

        try:
            if self._is_memory_db:
                self._shared_connection = sqlite3.connect(":memory:", check_same_thread=False)
                self._shared_connection.execute("PRAGMA journal_mode=WAL")
                self._init_db_for_connection(self._shared_connection)
            else:
                # For file databases, initialize the schema once since it persists
                with self._lock:
                    init_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                    init_conn.execute("PRAGMA journal_mode=WAL")
                    self._init_db_for_connection(init_conn)
                    init_conn.close()
        except Exception:
            if self._lock_path is not None and not self._lock_released:
                self._release_file_lock(self._lock_path)
                self._lock_released = True
            raise

    @classmethod
    def _acquire_file_lock(cls, db_path: Path) -> tuple[Path, threading.RLock]:
        """Return the path key and process-local lock for sessions sharing one SQLite file."""
        lock_path = db_path.expanduser().resolve()
        with cls._file_locks_guard:
            lock = cls._file_locks.get(lock_path)
            if lock is None:
                lock = threading.RLock()
                cls._file_locks[lock_path] = lock
                cls._file_lock_counts[lock_path] = 0
            cls._file_lock_counts[lock_path] += 1
            return lock_path, lock

    @classmethod
    def _release_file_lock(cls, lock_path: Path) -> None:
        """Drop the shared lock for a file-backed DB once the last session closes."""
        with cls._file_locks_guard:
            ref_count = cls._file_lock_counts.get(lock_path)
            if ref_count is None:
                return
            if ref_count <= 1:
                cls._file_lock_counts.pop(lock_path, None)
                cls._file_locks.pop(lock_path, None)
            else:
                cls._file_lock_counts[lock_path] = ref_count - 1

    @contextmanager
    def _locked_connection(self) -> Iterator[sqlite3.Connection]:
        """Serialize sqlite3 access while each operation runs in a worker thread."""
        with self._lock:
            yield self._get_connection()

    def _check_not_closed(self) -> None:
        """Raise if the session has already been closed."""
        if self._closed:
            raise RuntimeError("SQLiteSession is closed")

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """Provide a connection that cannot retain a failed write transaction."""
        with self._locked_connection() as conn:
            try:
                yield conn
            except BaseException:
                try:
                    conn.rollback()
                except BaseException:
                    self._invalidate_connection(conn)
                raise

    def _invalidate_connection(self, conn: sqlite3.Connection) -> None:
        """Close and evict a connection that could not roll back safely."""
        try:
            conn.close()
        except BaseException:
            close_failed = True
        else:
            close_failed = False

        with self._connections_lock:
            self._connections.discard(conn)
            if close_failed:
                self._quarantined_connections.add(conn)
            else:
                self._quarantined_connections.discard(conn)
        if getattr(self._local, "connection", None) is conn:
            del self._local.connection
        if self._is_memory_db or close_failed:
            self._closed = True

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        self._check_not_closed()

        if self._is_memory_db:
            # Use shared connection for in-memory database to avoid thread isolation
            return self._shared_connection
        else:
            # Use thread-local connections for file databases
            if not hasattr(self._local, "connection"):
                connection = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                )
                connection.execute("PRAGMA journal_mode=WAL")
                self._local.connection = connection
                with self._connections_lock:
                    self._connections.add(connection)
            assert isinstance(self._local.connection, sqlite3.Connection), (
                f"Expected sqlite3.Connection, got {type(self._local.connection)}"
            )
            return self._local.connection

    def _init_db_for_connection(self, conn: sqlite3.Connection) -> None:
        """Initialize the database schema for a specific connection."""
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.sessions_table} (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.messages_table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES {self.sessions_table} (session_id)
                    ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{self.messages_table}_session_id
            ON {self.messages_table} (session_id, id)
        """
        )

        conn.commit()

    def _insert_items(self, conn: sqlite3.Connection, items: list[TResponseInputItem]) -> None:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {self.sessions_table} (session_id) VALUES (?)
        """,
            (self.session_id,),
        )

        message_data = [(self.session_id, json.dumps(item)) for item in items]
        conn.executemany(
            f"""
            INSERT INTO {self.messages_table} (session_id, message_data) VALUES (?, ?)
        """,
            message_data,
        )

        conn.execute(
            f"""
            UPDATE {self.sessions_table}
            SET updated_at = CURRENT_TIMESTAMP
            WHERE session_id = ?
        """,
            (self.session_id,),
        )

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, uses session_settings.limit.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        session_limit = resolve_session_limit(limit, self.session_settings)

        def _decode_rows(rows: list[Any]) -> list[TResponseInputItem]:
            items: list[TResponseInputItem] = []
            for (message_data,) in rows:
                try:
                    item = json.loads(message_data)
                    items.append(item)
                except (json.JSONDecodeError, TypeError):
                    # Skip invalid JSON entries
                    continue
            return items

        def _get_items_sync():
            with self._locked_connection() as conn:
                if session_limit is None:
                    # Fetch all items in chronological order
                    cursor = conn.execute(
                        f"""
                        SELECT message_data FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY id ASC
                    """,
                        (self.session_id,),
                    )
                    return _decode_rows(cursor.fetchall())

                if session_limit > 0:
                    # Expand the fetch window when corrupt rows sit among the newest entries so
                    # limit counts valid conversation items, matching EncryptedSession and pop_item.
                    window = session_limit
                    while True:
                        cursor = conn.execute(
                            f"""
                            SELECT message_data FROM {self.messages_table}
                            WHERE session_id = ?
                            ORDER BY id DESC
                            LIMIT ?
                            """,
                            (self.session_id, window),
                        )
                        rows = cursor.fetchall()
                        items = _decode_rows(list(reversed(rows)))
                        if len(items) >= session_limit:
                            return items[-session_limit:]
                        if len(rows) < window:
                            return items
                        window *= 2

                # Preserve historical non-positive LIMIT semantics (including SQLite's
                # unlimited behavior for negative values).
                cursor = conn.execute(
                    f"""
                    SELECT message_data FROM {self.messages_table}
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (self.session_id, session_limit),
                )
                return _decode_rows(list(reversed(cursor.fetchall())))

        return await asyncio.to_thread(_get_items_sync)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        # Checked before the empty-list fast path, which would otherwise return
        # successfully on a closed session.
        self._check_not_closed()
        if not items:
            return

        def _add_items_sync():
            with self._write_connection() as conn:
                self._insert_items(conn, items)
                conn.commit()

        await _await_mutation(asyncio.to_thread(_add_items_sync))

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """

        def _pop_item_sync():
            with self._write_connection() as conn:
                # Use DELETE with RETURNING to atomically delete and return the most recent item
                cursor = conn.execute(
                    f"""
                    DELETE FROM {self.messages_table}
                    WHERE id = (
                        SELECT id FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY id DESC
                        LIMIT 1
                    )
                    RETURNING message_data
                    """,
                    (self.session_id,),
                )

                result = cursor.fetchone()
                conn.commit()

                while result:
                    message_data = result[0]
                    try:
                        item = json.loads(message_data)
                        return item
                    except (json.JSONDecodeError, TypeError):
                        # Drop corrupted JSON entries and keep looking for a valid item.
                        cursor = conn.execute(
                            f"""
                            DELETE FROM {self.messages_table}
                            WHERE id = (
                                SELECT id FROM {self.messages_table}
                                WHERE session_id = ?
                                ORDER BY id DESC
                                LIMIT 1
                            )
                            RETURNING message_data
                            """,
                            (self.session_id,),
                        )
                        result = cursor.fetchone()
                        conn.commit()

                return None

        return await _await_mutation(asyncio.to_thread(_pop_item_sync))

    async def clear_session(self) -> None:
        """Clear all items for this session."""

        def _clear_session_sync():
            with self._write_connection() as conn:
                conn.execute(
                    f"DELETE FROM {self.messages_table} WHERE session_id = ?",
                    (self.session_id,),
                )
                conn.execute(
                    f"DELETE FROM {self.sessions_table} WHERE session_id = ?",
                    (self.session_id,),
                )
                conn.commit()

        await _await_mutation(asyncio.to_thread(_clear_session_sync))

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._closed = True
            with self._connections_lock:
                connections = self._connections | self._quarantined_connections
            if self._is_memory_db:
                if hasattr(self, "_shared_connection"):
                    connections.add(self._shared_connection)

            first_error: BaseException | None = None
            for connection in connections:
                try:
                    connection.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    with self._connections_lock:
                        self._connections.discard(connection)
                        self._quarantined_connections.add(connection)
                else:
                    with self._connections_lock:
                        self._connections.discard(connection)
                        self._quarantined_connections.discard(connection)

            if getattr(self._local, "connection", None) in connections:
                del self._local.connection

            with self._connections_lock:
                has_unclosed_connections = bool(self._quarantined_connections)
            if not has_unclosed_connections and self._lock_path is not None:
                with self._connections_lock:
                    self._connections.clear()
                if not self._lock_released:
                    self._release_file_lock(self._lock_path)
                    self._lock_released = True

            if first_error is not None:
                raise first_error

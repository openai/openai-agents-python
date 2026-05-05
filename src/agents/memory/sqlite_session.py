from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from ..items import TResponseInputItem
from .session import SessionABC
from .session_settings import SessionSettings, resolve_session_limit


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
        session_settings: SessionSettings | None = None,
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
        self.session_settings = session_settings or SessionSettings()
        self.db_path = db_path
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
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

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        if self._closed:
            raise RuntimeError("SQLiteSession is closed")

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
                else:
                    # Fetch the latest N items in chronological order
                    cursor = conn.execute(
                        f"""
                        SELECT message_data FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (self.session_id, session_limit),
                    )

                rows = cursor.fetchall()

                # Reverse to get chronological order when using DESC
                if session_limit is not None:
                    rows = list(reversed(rows))

                items = []
                for (message_data,) in rows:
                    try:
                        item = json.loads(message_data)
                        items.append(item)
                    except json.JSONDecodeError:
                        # Skip invalid JSON entries
                        continue

                return items

        return await asyncio.to_thread(_get_items_sync)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        def _add_items_sync():
            with self._locked_connection() as conn:
                self._insert_items(conn, items)
                conn.commit()

        await asyncio.to_thread(_add_items_sync)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """

        def _pop_item_sync():
            with self._locked_connection() as conn:
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

                if result:
                    message_data = result[0]
                    try:
                        item = json.loads(message_data)
                        return item
                    except json.JSONDecodeError:
                        # Return None for corrupted JSON entries (already deleted)
                        return None

                return None

        return await asyncio.to_thread(_pop_item_sync)

    async def clear_session(self) -> None:
        """Clear all items for this session."""

        def _clear_session_sync():
            with self._locked_connection() as conn:
                conn.execute(
                    f"DELETE FROM {self.messages_table} WHERE session_id = ?",
                    (self.session_id,),
                )
                conn.execute(
                    f"DELETE FROM {self.sessions_table} WHERE session_id = ?",
                    (self.session_id,),
                )
                conn.commit()

        await asyncio.to_thread(_clear_session_sync)

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._closed:
                return

            self._closed = True
            if self._is_memory_db:
                if hasattr(self, "_shared_connection"):
                    self._shared_connection.close()
            else:
                with self._connections_lock:
                    connections = list(self._connections)
                    self._connections.clear()
                for connection in connections:
                    connection.close()
            if self._lock_path is not None and not self._lock_released:
                self._release_file_lock(self._lock_path)
                self._lock_released = True

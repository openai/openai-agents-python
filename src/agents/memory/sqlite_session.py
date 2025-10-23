from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

from ..items import TResponseInputItem
from .session import SessionABC


class SQLiteSession(SessionABC):
    """SQLite-based implementation of session storage.

    This implementation stores conversation history in a SQLite database.
    By default, uses an in-memory database that is lost when the process ends.
    For persistent storage, provide a file path.
    """

    def __init__(
        self,
        session_id: str,
        db_path: str | Path = ":memory:",
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
    ):
        """Initialize the SQLite session.

        Args:
            session_id: Unique identifier for the conversation session
            db_path: Path to the SQLite database file. Defaults to ':memory:' (in-memory database)
            sessions_table: Name of the table to store session metadata. Defaults to
                'agent_sessions'
            messages_table: Name of the table to store message data. Defaults to 'agent_messages'
        """
        self.session_id = session_id
        self.db_path = db_path
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self._lock = threading.RLock()

        # Keep _is_memory_db for backward compatibility with AdvancedSQLiteSession
        self._is_memory_db = str(db_path) == ":memory:"

        # Use a shared connection for all database types
        # This avoids file descriptor leaks from thread-local connections
        # WAL mode enables concurrent readers/writers even with a shared connection
        self._shared_connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._shared_connection.execute("PRAGMA journal_mode=WAL")
        self._init_db_for_connection(self._shared_connection)

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return self._shared_connection

    async def _to_thread_with_lock(self, func, *args, **kwargs):
        """Execute a function in a thread pool with lock protection.

        This ensures thread-safe access to the shared database connection
        when operations are executed via asyncio.to_thread(). Uses RLock
        so it's safe to call even if the lock is already held.

        Args:
            func: The function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            The result of the function execution
        """

        def wrapped():
            with self._lock:
                return func(*args, **kwargs)

        return await asyncio.to_thread(wrapped)

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
            ON {self.messages_table} (session_id, created_at)
        """
        )

        conn.commit()

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """

        def _get_items_sync():
            conn = self._get_connection()
            with self._lock:
                if limit is None:
                    # Fetch all items in chronological order
                    cursor = conn.execute(
                        f"""
                        SELECT message_data FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY created_at ASC
                    """,
                        (self.session_id,),
                    )
                else:
                    # Fetch the latest N items in chronological order
                    cursor = conn.execute(
                        f"""
                        SELECT message_data FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (self.session_id, limit),
                    )

                rows = cursor.fetchall()

                # Reverse to get chronological order when using DESC
                if limit is not None:
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
            conn = self._get_connection()

            with self._lock:
                # Ensure session exists
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {self.sessions_table} (session_id) VALUES (?)
                """,
                    (self.session_id,),
                )

                # Add items
                message_data = [(self.session_id, json.dumps(item)) for item in items]
                conn.executemany(
                    f"""
                    INSERT INTO {self.messages_table} (session_id, message_data) VALUES (?, ?)
                """,
                    message_data,
                )

                # Update session timestamp
                conn.execute(
                    f"""
                    UPDATE {self.sessions_table}
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                """,
                    (self.session_id,),
                )

                conn.commit()

        await asyncio.to_thread(_add_items_sync)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """

        def _pop_item_sync():
            conn = self._get_connection()
            with self._lock:
                # Use DELETE with RETURNING to atomically delete and return the most recent item
                cursor = conn.execute(
                    f"""
                    DELETE FROM {self.messages_table}
                    WHERE id = (
                        SELECT id FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY created_at DESC
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
            conn = self._get_connection()
            with self._lock:
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
        if hasattr(self, "_shared_connection"):
            self._shared_connection.close()

    def __del__(self) -> None:
        """Ensure connection is closed when the session is garbage collected."""
        try:
            self.close()
        except Exception:
            pass  # Ignore errors during finalization

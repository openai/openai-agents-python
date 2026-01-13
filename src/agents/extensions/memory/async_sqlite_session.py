from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import aiosqlite

from ...items import TResponseInputItem
from ...memory import SessionABC


class AsyncSQLiteSession(SessionABC):
    """Async SQLite-based implementation of session storage.

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
        """Initialize the async SQLite session.

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
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()

    async def _init_db_for_connection(self, conn: aiosqlite.Connection) -> None:
        """Initialize the database schema for a specific connection."""
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.sessions_table} (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        await conn.execute(
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

        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{self.messages_table}_session_id
            ON {self.messages_table} (session_id, id)
        """
        )

        await conn.commit()

    async def _get_connection(self) -> aiosqlite.Connection:
        """Get or create a database connection."""
        if self._connection is not None:
            return self._connection

        async with self._init_lock:
            if self._connection is None:
                self._connection = await aiosqlite.connect(str(self.db_path))
                await self._connection.execute("PRAGMA journal_mode=WAL")
                await self._init_db_for_connection(self._connection)

        return self._connection

    @asynccontextmanager
    async def _locked_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Provide a connection under the session lock."""
        async with self._lock:
            conn = await self._get_connection()
            yield conn

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """

        async with self._locked_connection() as conn:
            if limit is None:
                cursor = await conn.execute(
                    f"""
                    SELECT message_data FROM {self.messages_table}
                    WHERE session_id = ?
                    ORDER BY id ASC
                """,
                    (self.session_id,),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    SELECT message_data FROM {self.messages_table}
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (self.session_id, limit),
                )

            rows = list(await cursor.fetchall())
            await cursor.close()

        if limit is not None:
            rows = rows[::-1]

        items: list[TResponseInputItem] = []
        for (message_data,) in rows:
            try:
                item = json.loads(message_data)
                items.append(item)
            except json.JSONDecodeError:
                continue

        return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        async with self._locked_connection() as conn:
            await conn.execute(
                f"""
                INSERT OR IGNORE INTO {self.sessions_table} (session_id) VALUES (?)
            """,
                (self.session_id,),
            )

            message_data = [(self.session_id, json.dumps(item)) for item in items]
            await conn.executemany(
                f"""
                INSERT INTO {self.messages_table} (session_id, message_data) VALUES (?, ?)
            """,
                message_data,
            )

            await conn.execute(
                f"""
                UPDATE {self.sessions_table}
                SET updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
            """,
                (self.session_id,),
            )

            await conn.commit()

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        async with self._locked_connection() as conn:
            cursor = await conn.execute(
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

            result = await cursor.fetchone()
            await cursor.close()
            await conn.commit()

        if result:
            message_data = result[0]
            try:
                return cast(TResponseInputItem, json.loads(message_data))
            except json.JSONDecodeError:
                return None

        return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._locked_connection() as conn:
            await conn.execute(
                f"DELETE FROM {self.messages_table} WHERE session_id = ?",
                (self.session_id,),
            )
            await conn.execute(
                f"DELETE FROM {self.sessions_table} WHERE session_id = ?",
                (self.session_id,),
            )
            await conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._connection is None:
            return
        async with self._lock:
            await self._connection.close()
            self._connection = None

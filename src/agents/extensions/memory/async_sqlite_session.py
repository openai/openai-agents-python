from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import aiosqlite

from ...items import TResponseInputItem
from ...memory import SessionABC
from ...memory.session_settings import (
    SessionSettings,
    coerce_session_settings,
    resolve_session_limit,
)
from ...memory.sqlite_session import _await_mutation


class AsyncSQLiteSession(SessionABC):
    """Async SQLite-based implementation of session storage.

    This implementation stores conversation history in a SQLite database.
    By default, uses an in-memory database that is lost when the process ends.
    For persistent storage, provide a file path.
    """

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        db_path: str | Path = ":memory:",
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ):
        """Initialize the async SQLite session.

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
        self._connection: aiosqlite.Connection | None = None
        self._quarantined_connections: set[aiosqlite.Connection] = set()
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._closed = False

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
                connect_task = asyncio.ensure_future(aiosqlite.connect(str(self.db_path)))
                try:
                    connection = await asyncio.shield(connect_task)
                except BaseException as acquisition_error:
                    connection = None
                    cleanup_cancellation: asyncio.CancelledError | None = None
                    try:
                        connection = await _await_mutation(connect_task)
                    except asyncio.CancelledError as exc:
                        cleanup_cancellation = exc
                        try:
                            connection = connect_task.result()
                        except BaseException:
                            pass
                    except BaseException:
                        pass
                    close_error = (
                        await self._close_owned_connection(connection)
                        if connection is not None
                        else None
                    )
                    if isinstance(acquisition_error, asyncio.CancelledError):
                        raise
                    if cleanup_cancellation is not None:
                        raise cleanup_cancellation from None
                    if isinstance(close_error, asyncio.CancelledError):
                        raise close_error from None
                    raise
                assert connection is not None
                try:
                    await connection.execute("PRAGMA journal_mode=WAL")
                    await self._init_db_for_connection(connection)
                except BaseException as initialization_error:
                    close_error = await self._close_owned_connection(connection)
                    if isinstance(initialization_error, asyncio.CancelledError):
                        raise
                    if isinstance(close_error, asyncio.CancelledError):
                        raise close_error from None
                    raise
                self._connection = connection

        return self._connection

    def _check_not_closed(self) -> None:
        """Raise if the session has already been closed."""
        if self._closed:
            raise RuntimeError("AsyncSQLiteSession is closed")

    @asynccontextmanager
    async def _locked_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Provide a connection under the session lock."""
        async with self._lock:
            self._check_not_closed()
            conn = await self._get_connection()
            yield conn

    @asynccontextmanager
    async def _write_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Provide a connection that cannot retain a failed write transaction."""
        async with self._locked_connection() as conn:
            try:
                yield conn
            except BaseException as operation_error:
                rollback_task = asyncio.create_task(conn.rollback())
                rollback_error: BaseException | None = None
                rollback_cancellation: asyncio.CancelledError | None = None
                try:
                    await _await_mutation(rollback_task)
                except asyncio.CancelledError as exc:
                    rollback_cancellation = exc
                    try:
                        rollback_task.result()
                    except BaseException as outcome_error:
                        rollback_error = outcome_error
                except BaseException as exc:
                    rollback_error = exc

                invalidation_error = None
                if rollback_error is not None:
                    invalidation_error = await self._invalidate_connection(conn)

                if isinstance(operation_error, asyncio.CancelledError):
                    raise
                if rollback_cancellation is not None:
                    raise rollback_cancellation from None
                if isinstance(invalidation_error, asyncio.CancelledError):
                    raise invalidation_error from None
                raise

    async def _invalidate_connection(self, conn: aiosqlite.Connection) -> BaseException | None:
        """Close and evict a connection that could not roll back safely."""
        close_error = await self._close_owned_connection(conn)
        if self._connection is conn:
            self._connection = None
        if str(self.db_path) == ":memory:" or close_error is not None:
            self._closed = True
        return close_error

    async def _close_owned_connection(self, conn: aiosqlite.Connection) -> BaseException | None:
        """Close an owned connection or retain it for a later cleanup retry."""
        close_task = asyncio.create_task(conn.close())
        cancellation: asyncio.CancelledError | None = None
        close_error: BaseException | None = None
        try:
            await _await_mutation(close_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            try:
                close_task.result()
            except BaseException as outcome_error:
                close_error = outcome_error
        except BaseException as exc:
            close_error = exc

        if close_error is not None:
            self._quarantined_connections.add(conn)
            self._closed = True
        else:
            self._quarantined_connections.discard(conn)
        return cancellation or close_error

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
                except json.JSONDecodeError:
                    continue
            return items

        async with self._locked_connection() as conn:
            if session_limit is None:
                cursor = await conn.execute(
                    f"""
                    SELECT message_data FROM {self.messages_table}
                    WHERE session_id = ?
                    ORDER BY id ASC
                """,
                    (self.session_id,),
                )
                rows = list(await cursor.fetchall())
                await cursor.close()
                return _decode_rows(rows)

            if session_limit > 0:
                # Expand the fetch window when corrupt rows sit among the newest entries so
                # limit counts valid conversation items, matching EncryptedSession and pop_item.
                window = session_limit
                while True:
                    cursor = await conn.execute(
                        f"""
                        SELECT message_data FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (self.session_id, window),
                    )
                    rows = list(await cursor.fetchall())
                    await cursor.close()
                    items = _decode_rows(rows[::-1])
                    if len(items) >= session_limit:
                        return items[-session_limit:]
                    if len(rows) < window:
                        return items
                    window *= 2

            # Preserve historical non-positive LIMIT semantics (including SQLite's
            # unlimited behavior for negative values).
            cursor = await conn.execute(
                f"""
                SELECT message_data FROM {self.messages_table}
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.session_id, session_limit),
            )
            rows = list(await cursor.fetchall())
            await cursor.close()
            return _decode_rows(rows[::-1])

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        self._check_not_closed()
        if not items:
            return

        async with self._write_connection() as conn:
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

            await _await_mutation(conn.commit())

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """

        async with self._write_connection() as conn:
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
            await _await_mutation(conn.commit())

            while result:
                message_data = result[0]
                try:
                    return cast(TResponseInputItem, json.loads(message_data))
                except (json.JSONDecodeError, TypeError):
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
                    await _await_mutation(conn.commit())

        return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""

        async with self._write_connection() as conn:
            await conn.execute(
                f"DELETE FROM {self.messages_table} WHERE session_id = ?",
                (self.session_id,),
            )
            await conn.execute(
                f"DELETE FROM {self.sessions_table} WHERE session_id = ?",
                (self.session_id,),
            )
            await _await_mutation(conn.commit())

    async def close(self) -> None:
        """Close the database connection.

        The session becomes terminal from the first close attempt: subsequent
        operations raise RuntimeError rather than reopening the database. Repeated
        and concurrent calls are safe. A repeated call retries any owned
        connection whose previous close did not complete.
        """
        async with self._lock:
            self._closed = True
            connections = set(self._quarantined_connections)
            if self._connection is not None:
                connections.add(self._connection)

            first_error: BaseException | None = None
            cancellation: asyncio.CancelledError | None = None
            for connection in connections:
                close_task = asyncio.create_task(self._close_owned_connection(connection))
                try:
                    close_error = await asyncio.shield(close_task)
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
                    try:
                        close_error = await _await_mutation(close_task)
                    except asyncio.CancelledError:
                        close_error = close_task.result()
                if close_error is None:
                    if self._connection is connection:
                        self._connection = None
                elif first_error is None:
                    first_error = close_error

            if cancellation is not None:
                raise cancellation
            if first_error is not None:
                raise first_error

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import class_row
    from psycopg_pool import AsyncConnectionPool
except ImportError as _e:
    raise ImportError(
        "`psycopg` is required to use the PostgreSQLSession. You can install it via the optional "
        "dependency group: `pip install 'openai-agents[psycopg]'`."
    ) from _e

if TYPE_CHECKING:
    from agents.items import TResponseInputItem

from agents.memory.session import Session


@dataclass
class MessageRow:
    """Typed row for message queries."""

    message_data: TResponseInputItem


@dataclass
class MessageWithIdRow:
    """Typed row for message queries that include ID."""

    id: int
    message_data: TResponseInputItem


class PostgreSQLSession(Session):
    """PostgreSQL-based implementation of session storage.

    This implementation stores conversation history in a PostgreSQL database.
    Requires psycopg to be installed.
    """

    pool: AsyncConnectionPool

    def __init__(
        self,
        session_id: str,
        pool: AsyncConnectionPool,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
    ):
        """Initialize the PostgreSQL session.

        Args:
            session_id: Unique identifier for the conversation session
            pool: PostgreSQL connection pool instance.
                This should be opened before passing to this class.
            sessions_table: Name of the table to store session metadata. Defaults to
                'agent_sessions'
            messages_table: Name of the table to store message data. Defaults to 'agent_messages'
        """
        if psycopg is None:
            raise ImportError(
                "psycopg is required for PostgreSQL session storage. "
                "Install with: pip install psycopg"
            )

        self.session_id = session_id
        self.pool = pool
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self._initialized = False

    @classmethod
    async def from_connection_string(
        cls,
        session_id: str,
        connection_string: str,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
    ) -> PostgreSQLSession:
        """Create a PostgreSQL session from a connection string.

        Args:
            session_id: Unique identifier for the conversation session
            connection_string: PostgreSQL connection string (e.g., "postgresql://user:pass@host/db")
            sessions_table: Name of the table to store session metadata. Defaults to
                'agent_sessions'
            messages_table: Name of the table to store message data. Defaults to 'agent_messages'

        Returns:
            PostgreSQLSession instance with a connection pool created from the connection string
        """
        pool: AsyncConnectionPool = AsyncConnectionPool(connection_string, open=False)
        await pool.open()
        return cls(session_id, pool, sessions_table, messages_table)

    async def _ensure_initialized(self) -> None:
        """Ensure the database schema is initialized."""
        if not self._initialized:
            await self._init_db()

    async def _init_db(self) -> None:
        """Initialize the database schema."""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                # Create sessions table
                query = sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {sessions_table} (
                        session_id TEXT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """).format(sessions_table=sql.Identifier(self.sessions_table))
                await cur.execute(query)

                # Create messages table
                query = sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {messages_table} (
                        id SERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        message_data JSONB NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (session_id) REFERENCES {sessions_table} (session_id)
                            ON DELETE CASCADE
                    )
                """).format(
                    messages_table=sql.Identifier(self.messages_table),
                    sessions_table=sql.Identifier(self.sessions_table),
                )
                await cur.execute(query)

                # Create index for better performance
                query = sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {index_name}
                    ON {messages_table} (session_id, created_at)
                """).format(
                    index_name=sql.Identifier(f"idx_{self.messages_table}_session_id"),
                    messages_table=sql.Identifier(self.messages_table),
                )
                await cur.execute(query)

        self._initialized = True

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        await self._ensure_initialized()

        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=class_row(MessageRow)) as cur:
                if limit is None:
                    # Fetch all items in chronological order
                    query = sql.SQL("""
                        SELECT message_data FROM {messages_table}
                        WHERE session_id = %s
                        ORDER BY created_at ASC
                    """).format(messages_table=sql.Identifier(self.messages_table))
                    await cur.execute(query, (self.session_id,))
                else:
                    # Fetch the latest N items in chronological order
                    query = sql.SQL("""
                        SELECT message_data FROM {messages_table}
                        WHERE session_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """).format(messages_table=sql.Identifier(self.messages_table))
                    await cur.execute(query, (self.session_id, limit))

                rows = await cur.fetchall()

                items = []
                for row in rows:
                    try:
                        # PostgreSQL JSONB automatically handles deserialization
                        item = row.message_data
                        items.append(item)
                    except (AttributeError, TypeError):
                        # Skip invalid entries
                        continue

                # If we used LIMIT, reverse the items to get chronological order
                if limit is not None:
                    items.reverse()

                return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        await self._ensure_initialized()

        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    # Ensure session exists
                    query = sql.SQL("""
                        INSERT INTO {sessions_table} (session_id)
                        VALUES (%s)
                        ON CONFLICT (session_id) DO NOTHING
                    """).format(sessions_table=sql.Identifier(self.sessions_table))
                    await cur.execute(query, (self.session_id,))

                    # Add items
                    message_data = [(self.session_id, json.dumps(item)) for item in items]
                    query = sql.SQL("""
                        INSERT INTO {messages_table} (session_id, message_data)
                        VALUES (%s, %s)
                    """).format(messages_table=sql.Identifier(self.messages_table))
                    await cur.executemany(query, message_data)

                    # Update session timestamp
                    query = sql.SQL("""
                        UPDATE {sessions_table}
                        SET updated_at = CURRENT_TIMESTAMP
                        WHERE session_id = %s
                    """).format(sessions_table=sql.Identifier(self.sessions_table))
                    await cur.execute(query, (self.session_id,))

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        await self._ensure_initialized()

        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=class_row(MessageRow)) as cur:
                    # Delete and return the most recent item in one query
                    query = sql.SQL("""
                        DELETE FROM {messages_table}
                        WHERE id = (
                            SELECT id FROM {messages_table}
                            WHERE session_id = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                        )
                        RETURNING message_data
                    """).format(messages_table=sql.Identifier(self.messages_table))
                    await cur.execute(query, (self.session_id,))

                    row = await cur.fetchone()

                    if row is None:
                        return None

                    try:
                        # PostgreSQL JSONB automatically handles deserialization
                        item = row.message_data
                        return item
                    except (AttributeError, TypeError):
                        # Return None for corrupted entries (already deleted)
                        return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        await self._ensure_initialized()

        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    query = sql.SQL("""
                        DELETE FROM {messages_table} WHERE session_id = %s
                    """).format(messages_table=sql.Identifier(self.messages_table))
                    await cur.execute(query, (self.session_id,))

                    query = sql.SQL("""
                        DELETE FROM {sessions_table} WHERE session_id = %s
                    """).format(sessions_table=sql.Identifier(self.sessions_table))
                    await cur.execute(query, (self.session_id,))

    async def close(self) -> None:
        """Close the database connection pool."""
        await self.pool.close()
        self._initialized = False

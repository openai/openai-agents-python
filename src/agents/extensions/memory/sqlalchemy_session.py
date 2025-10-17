"""SQLAlchemy-powered Session backend.

Usage::

    from agents.extensions.memory import SQLAlchemySession

    # Create from SQLAlchemy URL (uses asyncpg driver under the hood for Postgres)
    session = SQLAlchemySession.from_url(
        session_id="user-123",
        url="postgresql+asyncpg://app:secret@db.example.com/agents",
        create_tables=True, # If you want to auto-create tables, set to True.
    )

    # Or pass an existing AsyncEngine that your application already manages
    session = SQLAlchemySession(
        session_id="user-123",
        engine=my_async_engine,
        create_tables=True, # If you want to auto-create tables, set to True.
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    text as sql_text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from ...items import TResponseInputItem
from ...memory.session import SessionABC


class SQLAlchemySession(SessionABC):
    """SQLAlchemy implementation of :pyclass:`agents.memory.session.Session`."""

    _metadata: MetaData
    _sessions: Table
    _messages: Table

    def __init__(
        self,
        session_id: str,
        *,
        engine: AsyncEngine,
        create_tables: bool = False,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
    ):
        """Initializes a new SQLAlchemySession.

        Args:
            session_id (str): Unique identifier for the conversation.
            engine (AsyncEngine): A pre-configured SQLAlchemy async engine. The engine
                must be created with an async driver (e.g., 'postgresql+asyncpg://',
                'mysql+aiomysql://', or 'sqlite+aiosqlite://').
            create_tables (bool, optional): Whether to automatically create the required
                tables and indexes. Defaults to False for production use. Set to True for
                development and testing when migrations aren't used.
            sessions_table (str, optional): Override the default table name for sessions if needed.
            messages_table (str, optional): Override the default table name for messages if needed.
        """
        self.session_id = session_id
        self._engine = engine
        self._lock = asyncio.Lock()

        self._metadata = MetaData()
        self._sessions = Table(
            sessions_table,
            self._metadata,
            Column("session_id", String, primary_key=True),
            Column(
                "created_at",
                TIMESTAMP(timezone=False),
                server_default=sql_text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            Column(
                "updated_at",
                TIMESTAMP(timezone=False),
                server_default=sql_text("CURRENT_TIMESTAMP"),
                onupdate=sql_text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
        )

        self._messages = Table(
            messages_table,
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "session_id",
                String,
                ForeignKey(f"{sessions_table}.session_id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("message_data", Text, nullable=False),
            Column(
                "created_at",
                TIMESTAMP(timezone=False),
                server_default=sql_text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            Index(
                f"idx_{messages_table}_session_time",
                "session_id",
                "created_at",
            ),
            sqlite_autoincrement=True,
        )

        # Async session factory
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

        self._create_tables = create_tables

    # ---------------------------------------------------------------------
    # Convenience constructors
    # ---------------------------------------------------------------------
    @classmethod
    def from_url(
        cls,
        session_id: str,
        *,
        url: str,
        engine_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SQLAlchemySession:
        """Create a session from a database URL string.

        Args:
            session_id (str): Conversation ID.
            url (str): Any SQLAlchemy async URL, e.g. "postgresql+asyncpg://user:pass@host/db".
            engine_kwargs (dict[str, Any] | None): Additional keyword arguments forwarded to
                sqlalchemy.ext.asyncio.create_async_engine.
            **kwargs: Additional keyword arguments forwarded to the main constructor
                (e.g., create_tables, custom table names, etc.).

        Returns:
            SQLAlchemySession: An instance of SQLAlchemySession connected to the specified database.
        """
        engine_kwargs = engine_kwargs or {}
        engine = create_async_engine(url, **engine_kwargs)
        return cls(session_id, engine=engine, **kwargs)

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to JSON string. Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))

    async def _deserialize_item(self, item: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item. Can be overridden by subclasses."""
        return json.loads(item)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------
    async def _ensure_tables(self) -> None:
        """Ensure tables are created before any database operations."""
        if self._create_tables:
            async with self._engine.begin() as conn:
                await conn.run_sync(self._metadata.create_all)
            self._create_tables = False  # Only create once

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        await self._ensure_tables()
        async with self._session_factory() as sess:
            if limit is None:
                stmt = (
                    select(self._messages.c.message_data)
                    .where(self._messages.c.session_id == self.session_id)
                    .order_by(
                        self._messages.c.created_at.asc(),
                        self._messages.c.id.asc(),
                    )
                )
            else:
                stmt = (
                    select(self._messages.c.message_data)
                    .where(self._messages.c.session_id == self.session_id)
                    # Use DESC + LIMIT to get the latest N
                    # then reverse later for chronological order.
                    .order_by(
                        self._messages.c.created_at.desc(),
                        self._messages.c.id.desc(),
                    )
                    .limit(limit)
                )

            result = await sess.execute(stmt)
            rows: list[str] = [row[0] for row in result.all()]

            if limit is not None:
                rows.reverse()

            items: list[TResponseInputItem] = []
            for raw in rows:
                try:
                    items.append(await self._deserialize_item(raw))
                except json.JSONDecodeError:
                    # Skip corrupted rows
                    continue
            return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        await self._ensure_tables()
        payload = [
            {
                "session_id": self.session_id,
                "message_data": await self._serialize_item(item),
            }
            for item in items
        ]

        async with self._session_factory() as sess:
            async with sess.begin():
                # Ensure the parent session row exists - use merge for cross-DB compatibility
                # Check if session exists
                existing = await sess.execute(
                    select(self._sessions.c.session_id).where(
                        self._sessions.c.session_id == self.session_id
                    )
                )
                if not existing.scalar_one_or_none():
                    # Session doesn't exist, create it
                    await sess.execute(
                        insert(self._sessions).values({"session_id": self.session_id})
                    )

                # Insert messages in bulk
                await sess.execute(insert(self._messages), payload)

                # Touch updated_at column
                await sess.execute(
                    update(self._sessions)
                    .where(self._sessions.c.session_id == self.session_id)
                    .values(updated_at=sql_text("CURRENT_TIMESTAMP"))
                )

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        await self._ensure_tables()
        async with self._session_factory() as sess:
            async with sess.begin():
                # Fallback for all dialects - get ID first, then delete
                subq = (
                    select(self._messages.c.id)
                    .where(self._messages.c.session_id == self.session_id)
                    .order_by(
                        self._messages.c.created_at.desc(),
                        self._messages.c.id.desc(),
                    )
                    .limit(1)
                )
                res = await sess.execute(subq)
                row_id = res.scalar_one_or_none()
                if row_id is None:
                    return None
                # Fetch data before deleting
                res_data = await sess.execute(
                    select(self._messages.c.message_data).where(self._messages.c.id == row_id)
                )
                row = res_data.scalar_one_or_none()
                await sess.execute(delete(self._messages).where(self._messages.c.id == row_id))

                if row is None:
                    return None
                try:
                    return await self._deserialize_item(row)
                except json.JSONDecodeError:
                    return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        await self._ensure_tables()
        async with self._session_factory() as sess:
            async with sess.begin():
                await sess.execute(
                    delete(self._messages).where(self._messages.c.session_id == self.session_id)
                )
                await sess.execute(
                    delete(self._sessions).where(self._sessions.c.session_id == self.session_id)
                )

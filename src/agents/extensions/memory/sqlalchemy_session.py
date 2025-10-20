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

    # Set session metadata
    await session.set_metadata({
        "owner_id": "user_456",
        "title": "Customer Support Chat",
        "tags": ["support", "billing"]
    })

    # Get metadata
    metadata = await session.get_metadata(keys=["owner_id", "title"])

    # Find sessions by metadata
    user_sessions = await session.find_sessions_by_metadata("owner_id", "user_456")
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
    PrimaryKeyConstraint,
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
    _session_metadata: Table

    def __init__(
        self,
        session_id: str,
        *,
        engine: AsyncEngine,
        create_tables: bool = False,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
        session_metadata_table: str = "agent_sessions_metadata",
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
            session_metadata_table (str, optional): Override the default table name for session
                metadata if needed.
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

        self._session_metadata = Table(
            session_metadata_table,
            self._metadata,
            Column(
                "session_id",
                String,
                ForeignKey(f"{sessions_table}.session_id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("key", String(255), nullable=False),
            Column("value", Text, nullable=True),
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
            PrimaryKeyConstraint("session_id", "key", name="pk_session_metadata"),
            Index("idx_session_metadata_key_value", "key", "value"),
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

    def _serialize_metadata_value(self, value: Any) -> str:
        """Serialize metadata value to string (JSON for dicts/lists)."""
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    def _deserialize_metadata_value(self, value_str: str | None) -> Any:
        """Deserialize metadata value (auto-parse JSON)."""
        if value_str is None:
            return None
        try:
            return json.loads(value_str)
        except (json.JSONDecodeError, TypeError):
            return value_str

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
        """Clear all items and metadata for this session."""
        await self._ensure_tables()
        async with self._session_factory() as sess:
            async with sess.begin():
                # Delete metadata
                await sess.execute(
                    delete(self._session_metadata).where(
                        self._session_metadata.c.session_id == self.session_id
                    )
                )
                # Delete messages
                await sess.execute(
                    delete(self._messages).where(self._messages.c.session_id == self.session_id)
                )
                # Delete session
                await sess.execute(
                    delete(self._sessions).where(self._sessions.c.session_id == self.session_id)
                )

    # ------------------------------------------------------------------
    # Session metadata operations
    # ------------------------------------------------------------------
    async def set_metadata(self, metadata: dict[str, Any]) -> None:
        """Set metadata key-value pairs for this session (performs UPSERT).

        Args:
            metadata: Dictionary of key-value pairs to set. Values can be strings,
                      numbers, booleans, or JSON-serializable dicts/lists.

        Example:
            await session.set_metadata({
                "owner_id": "user_123",
                "title": "My Chat",
                "tags": ["work", "important"]
            })
        """
        if not metadata:
            return

        await self._ensure_tables()

        # Detect dialect and import correct insert function
        dialect_name = self._engine.dialect.name

        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect_name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert  # type: ignore[assignment]
        elif dialect_name == "mysql":
            from sqlalchemy.dialects.mysql import insert  # type: ignore[assignment]
        else:
            raise ValueError(f"Unsupported dialect: {dialect_name}")

        async with self._session_factory() as sess:
            async with sess.begin():
                # Auto-create session row if it doesn't exist
                existing = await sess.execute(
                    select(self._sessions.c.session_id).where(
                        self._sessions.c.session_id == self.session_id
                    )
                )
                if not existing.scalar_one_or_none():
                    await sess.execute(
                        insert(self._sessions).values({"session_id": self.session_id})
                    )

                # UPSERT each metadata key-value pair
                for key, value in metadata.items():
                    value_str = self._serialize_metadata_value(value)

                    stmt = insert(self._session_metadata).values(
                        {
                            "session_id": self.session_id,
                            "key": key,
                            "value": value_str,
                        }
                    )

                    # Use dialect-specific UPSERT
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["session_id", "key"],
                        set_={
                            "value": stmt.excluded.value,
                            "updated_at": sql_text("CURRENT_TIMESTAMP"),
                        },
                    )

                    await sess.execute(stmt)

    async def get_metadata(self, keys: list[str] | None = None) -> dict[str, Any]:
        """Get metadata for this session.

        Args:
            keys: Optional list of specific keys to retrieve. If None, returns all metadata.
                  Missing keys will have None as their value.

        Returns:
            Dictionary of metadata key-value pairs. Values are auto-deserialized from JSON.

        Example:
            # Get all metadata
            all_meta = await session.get_metadata()

            # Get specific keys (missing keys return None)
            meta = await session.get_metadata(keys=["owner_id", "title"])
            # Returns: {"owner_id": "user_123", "title": "My Chat"}
        """
        await self._ensure_tables()

        async with self._session_factory() as sess:
            if keys is None:
                # Get all metadata for this session
                stmt = select(self._session_metadata.c.key, self._session_metadata.c.value).where(
                    self._session_metadata.c.session_id == self.session_id
                )

                result = await sess.execute(stmt)
                rows = result.all()

                return {key: self._deserialize_metadata_value(value) for key, value in rows}
            else:
                # Get specific keys
                stmt = select(self._session_metadata.c.key, self._session_metadata.c.value).where(
                    self._session_metadata.c.session_id == self.session_id,
                    self._session_metadata.c.key.in_(keys),
                )

                result = await sess.execute(stmt)
                rows = result.all()

                # Build dict with None for missing keys
                found_keys = {key: self._deserialize_metadata_value(value) for key, value in rows}

                return {key: found_keys.get(key, None) for key in keys}

    async def delete_metadata(self, keys: list[str] | None = None) -> None:
        """Delete metadata for this session.

        Args:
            keys: Optional list of specific keys to delete. If None, deletes all metadata
                  for this session.

        Example:
            # Delete specific keys
            await session.delete_metadata(keys=["title", "tags"])

            # Delete all metadata
            await session.delete_metadata()
        """
        await self._ensure_tables()

        async with self._session_factory() as sess:
            async with sess.begin():
                if keys is None:
                    # Delete all metadata for this session
                    await sess.execute(
                        delete(self._session_metadata).where(
                            self._session_metadata.c.session_id == self.session_id
                        )
                    )
                else:
                    # Delete specific keys
                    await sess.execute(
                        delete(self._session_metadata).where(
                            self._session_metadata.c.session_id == self.session_id,
                            self._session_metadata.c.key.in_(keys),
                        )
                    )

    async def find_sessions_by_metadata(
        self, key: str, value: Any, limit: int | None = 100
    ) -> list[str]:
        """Find session IDs that have matching metadata (cross-session query).

        This is an instance method that queries across ALL sessions in the database,
        not just the current session_id.

        Args:
            key: Metadata key to search for
            value: Metadata value to match (supports simple types: str, int, bool)
            limit: Maximum number of session IDs to return. Pass None for unlimited results.

        Returns:
            List of session IDs matching the criteria

        Example:
            # Find all sessions for a specific user (limited to 100)
            session_ids = await session.find_sessions_by_metadata("owner_id", "user_123")
            # Returns: ["chat_1", "chat_2", "chat_3"]

            # Find all sessions without limit
            all_sessions = await session.find_sessions_by_metadata(
                "owner_id", "user_123", limit=None
            )
        """
        await self._ensure_tables()

        # Serialize value for comparison
        value_str = self._serialize_metadata_value(value)

        async with self._session_factory() as sess:
            stmt = (
                select(self._session_metadata.c.session_id)
                .where(
                    self._session_metadata.c.key == key, self._session_metadata.c.value == value_str
                )
                .distinct()
            )
            if limit is not None:
                stmt = stmt.limit(limit)

            result = await sess.execute(stmt)
            return [row[0] for row in result.all()]

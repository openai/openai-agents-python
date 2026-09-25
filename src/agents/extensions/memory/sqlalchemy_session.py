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
import threading
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, ClassVar, TypeVar

from sqlalchemy import (
    TIMESTAMP,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Select,
    String,
    Table,
    Text,
    delete,
    event,
    insert,
    literal,
    select,
    text as sql_text,
    update,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from ...items import TResponseInputItem
from ...memory.session import SessionABC, _CompactionSnapshot
from ...memory.session_settings import (
    SessionSettings,
    coerce_session_settings,
    resolve_session_limit,
)
from ...memory.sqlite_session import _await_mutation

_T = TypeVar("_T")


class SQLAlchemySession(SessionABC):
    """SQLAlchemy implementation of [`Session`][agents.memory.session.Session]."""

    _table_init_locks: ClassVar[dict[tuple[str, str, str], threading.Lock]] = {}
    _table_init_locks_guard: ClassVar[threading.Lock] = threading.Lock()
    # Keyed on id(engine.sync_engine) so two distinct engines that happen to compare equal
    # never share a cache entry.  A weakref.finalize callback removes the entry when the sync
    # engine is garbage collected, preventing stale id() values from being reused by a future
    # engine that has not been configured yet.
    _sqlite_configured_engines: ClassVar[set[int]] = set()
    _sqlite_connection_locks: ClassVar[dict[int, threading.Lock]] = {}
    _sqlite_configured_engines_guard: ClassVar[threading.Lock] = threading.Lock()
    _SQLITE_BUSY_TIMEOUT_MS: ClassVar[int] = 5000
    _SQLITE_LOCK_RETRY_DELAYS: ClassVar[tuple[float, ...]] = (0.05, 0.1, 0.2, 0.4, 0.8)
    _metadata: MetaData
    _sessions: Table
    _messages: Table
    session_settings: SessionSettings | None = None

    @classmethod
    def _get_table_init_lock(
        cls, engine: AsyncEngine, sessions_table: str, messages_table: str
    ) -> threading.Lock:
        lock_key = (
            engine.url.render_as_string(hide_password=True),
            sessions_table,
            messages_table,
        )
        with cls._table_init_locks_guard:
            lock = cls._table_init_locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                cls._table_init_locks[lock_key] = lock
            return lock

    @classmethod
    def _configure_sqlite_engine(cls, engine: AsyncEngine) -> None:
        """Apply SQLite settings that reduce transient lock failures."""
        if engine.dialect.name != "sqlite":
            return

        engine_key = id(engine.sync_engine)
        with cls._sqlite_configured_engines_guard:
            if engine_key in cls._sqlite_configured_engines:
                return

            @event.listens_for(engine.sync_engine, "connect")
            def _configure_sqlite_connection(dbapi_connection: Any, _: Any) -> None:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute(f"PRAGMA busy_timeout = {cls._SQLITE_BUSY_TIMEOUT_MS}")
                    cursor.execute("PRAGMA journal_mode = WAL")
                finally:
                    cursor.close()

            if isinstance(engine.pool, StaticPool):
                # StaticPool shares one DBAPI connection across AsyncSessions. Its
                # transactions, including read-context rollback, need one SDK owner.
                cls._sqlite_connection_locks[engine_key] = threading.Lock()
                weakref.finalize(
                    engine.sync_engine, cls._sqlite_connection_locks.pop, engine_key, None
                )
            cls._sqlite_configured_engines.add(engine_key)
            # Drop the entry once the sync engine goes away so a later engine allocated at the
            # same address is still configured instead of being treated as already configured.
            weakref.finalize(engine.sync_engine, cls._sqlite_configured_engines.discard, engine_key)

    @staticmethod
    def _is_sqlite_lock_error(exc: OperationalError) -> bool:
        return "database is locked" in str(exc).lower()

    async def _run_sqlite_write_with_retry(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Retry transient SQLite write lock failures with bounded backoff."""
        if self._engine.dialect.name != "sqlite":
            return await operation()

        for attempt, delay in enumerate((0.0, *self._SQLITE_LOCK_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await operation()
            except OperationalError as exc:
                if not self._is_sqlite_lock_error(exc):
                    raise
                if attempt == len(self._SQLITE_LOCK_RETRY_DELAYS):
                    raise
        raise AssertionError("SQLite write retry loop exited unexpectedly")

    def __init__(
        self,
        session_id: str,
        *,
        engine: AsyncEngine,
        create_tables: bool = False,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
        session_settings: SessionSettings | dict[str, Any] | None = None,
        ensure_ascii: bool = True,
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
            session_settings (SessionSettings | None, optional): Session configuration settings
            ensure_ascii (bool, optional): Whether to escape non-ASCII characters when serializing
                session items to JSON. Defaults to True to preserve the historical storage format.
        """
        self.session_id = session_id
        self.session_settings = (
            coerce_session_settings(session_settings)
            if session_settings is not None
            else SessionSettings()
        )
        self._engine = engine
        self._ensure_ascii = ensure_ascii
        self._configure_sqlite_engine(engine)
        self._connection_lock = self._sqlite_connection_locks.get(id(engine.sync_engine))
        self._init_lock = (
            self._get_table_init_lock(engine, sessions_table, messages_table)
            if create_tables
            else None
        )

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
        session_settings: SessionSettings | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SQLAlchemySession:
        """Create a session from a database URL string.

        Args:
            session_id (str): Conversation ID.
            url (str): Any SQLAlchemy async URL, e.g. "postgresql+asyncpg://user:pass@host/db".
            engine_kwargs (dict[str, Any] | None): Additional keyword arguments forwarded to
                sqlalchemy.ext.asyncio.create_async_engine.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
            **kwargs: Additional keyword arguments forwarded to the main constructor
                (e.g., create_tables, custom table names, etc.).

        Returns:
            SQLAlchemySession: An instance of SQLAlchemySession connected to the specified database.
        """
        engine_kwargs = engine_kwargs or {}
        engine = create_async_engine(url, **engine_kwargs)
        return cls(session_id, engine=engine, session_settings=session_settings, **kwargs)

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to JSON string. Can be overridden by subclasses."""
        return json.dumps(item, ensure_ascii=self._ensure_ascii, separators=(",", ":"))

    async def _deserialize_item(self, item: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item. Can be overridden by subclasses."""
        return json.loads(item)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def _connection_guard(self) -> AsyncIterator[None]:
        lock = self._connection_lock
        if lock is None:
            yield
            return
        while not lock.acquire(blocking=False):  # noqa: ASYNC110
            # A threading lock also covers supported callers on different event loops.
            # Polling avoids a cancelled worker acquiring and stranding the lock later.
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        async with self._connection_guard():
            stack = AsyncExitStack()
            try:
                yield await stack.enter_async_context(self._session_factory())
            finally:
                # Read-context rollback also owns the shared connection. Repeated
                # cancellation must not release the guard while close still runs.
                await _await_mutation(stack.aclose())

    async def _ensure_tables(self) -> None:
        """Ensure tables are created before any database operations."""
        if not self._create_tables:
            return

        assert self._init_lock is not None
        while not self._init_lock.acquire(blocking=False):  # noqa: ASYNC110
            # Poll without handing lock acquisition to a background thread so
            # cancellation cannot strand the shared init lock in the acquired state.
            await asyncio.sleep(0.01)
        try:
            if not self._create_tables:
                return

            async def create_tables() -> None:
                async with self._connection_guard():
                    async with self._engine.begin() as conn:
                        await conn.run_sync(self._metadata.create_all)
                    self._create_tables = False  # Only create once

            await _await_mutation(create_tables())
        finally:
            self._init_lock.release()

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, uses session_settings.limit.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        await self._ensure_tables()

        session_limit = resolve_session_limit(limit, self.session_settings)

        async def _decode_rows(rows: list[str]) -> list[TResponseInputItem]:
            items: list[TResponseInputItem] = []
            for raw in rows:
                try:
                    items.append(await self._deserialize_item(raw))
                except json.JSONDecodeError:
                    # Skip corrupted rows
                    continue
            return items

        def _latest_first_stmt(row_limit: int) -> Select[tuple[str]]:
            # Use DESC + LIMIT to get the latest N
            # then reverse later for chronological order.
            return (
                select(self._messages.c.message_data)
                .where(self._messages.c.session_id == self.session_id)
                .order_by(
                    self._messages.c.created_at.desc(),
                    self._messages.c.id.desc(),
                )
                .limit(row_limit)
            )

        async with self._session() as sess:
            if session_limit is None:
                stmt = (
                    select(self._messages.c.message_data)
                    .where(self._messages.c.session_id == self.session_id)
                    .order_by(
                        self._messages.c.created_at.asc(),
                        self._messages.c.id.asc(),
                    )
                )
                result = await sess.execute(stmt)
                return await _decode_rows([row[0] for row in result.all()])

            if session_limit > 0:
                # Expand the fetch window when corrupt rows sit among the newest entries so
                # limit counts valid conversation items, matching pop_item and the SQLite
                # backends.
                window = session_limit
                while True:
                    result = await sess.execute(_latest_first_stmt(window))
                    rows: list[str] = [row[0] for row in result.all()]
                    items = await _decode_rows(rows[::-1])
                    if len(items) >= session_limit:
                        return items[-session_limit:]
                    if len(rows) < window:
                        return items
                    window *= 2

            # Preserve existing non-positive LIMIT semantics, which are dialect-defined.
            result = await sess.execute(_latest_first_stmt(session_limit))
            return await _decode_rows([row[0] for row in result.all()][::-1])

    async def _lock_session(self, sess: AsyncSession, *, create: bool = False) -> bool:
        """Serialize mutations across instances before reading or changing message rows."""
        if self._engine.dialect.name == "sqlite":
            # A write reserves SQLite's writer even when a caller-configured engine
            # already began the transaction; issuing BEGIN again would fail there.
            await sess.execute(
                update(self._sessions)
                .where(self._sessions.c.session_id == self.session_id)
                .values(updated_at=self._sessions.c.updated_at)
            )
        parent = select(self._sessions.c.session_id).where(
            self._sessions.c.session_id == self.session_id
        )
        if create:
            # Do not lock a missing MySQL key before inserting: concurrent gap locks
            # would deadlock first writers. A new parent is owned by its insert.
            existing = await sess.execute(parent)
            if existing.scalar_one_or_none() is not None:
                locked = await sess.execute(parent.with_for_update())
                if locked.scalar_one_or_none() is not None:
                    return True
            try:
                async with sess.begin_nested():
                    await sess.execute(insert(self._sessions).values(session_id=self.session_id))
            except IntegrityError:
                locked = await sess.execute(parent.with_for_update())
                if locked.scalar_one_or_none() is None:
                    # A constraint failure is not proof that a competing writer won.
                    raise
            return True
        locked = await sess.execute(parent.with_for_update())
        return locked.scalar_one_or_none() is not None

    async def _get_compaction_snapshot(
        self,
        limit: int,
        *,
        prune_prefix: Callable[[TResponseInputItem], bool] | None = None,
    ) -> _CompactionSnapshot | None:
        # Overrides may transform history or maintain additional indexes.
        if type(self) is not SQLAlchemySession or self._engine.dialect.name not in {
            "sqlite",
            "postgresql",
            "mysql",
            "mariadb",
        }:
            return None
        await self._ensure_tables()
        messages = self._messages
        tail = (
            select(messages.c.id, messages.c.message_data, messages.c.created_at)
            .where(messages.c.session_id == self.session_id)
            .order_by(messages.c.created_at.desc(), messages.c.id.desc())
        )
        async with self._session() as sess:
            # One extra row determines completeness without materializing the prefix.
            result = await sess.execute(tail.limit(limit + 1))
            fetched = list(result.all())
        complete = len(fetched) <= limit
        rows = fetched[:limit][::-1]
        try:
            items = [await self._deserialize_item(row.message_data) for row in rows]
        except (json.JSONDecodeError, TypeError):
            return None

        async def replace_suffix(start: int, output: list[TResponseInputItem]) -> bool:
            expected = rows[start:]
            if not expected:
                return False
            payload = [
                {
                    "session_id": self.session_id,
                    "message_data": await self._serialize_item(item),
                }
                for item in output
            ]

            async def replace() -> bool:
                async with self._session() as sess:
                    async with sess.begin():
                        if not await self._lock_session(sess):
                            return False
                        current = await sess.execute(tail.limit(len(expected)).with_for_update())
                        if list(current.all())[::-1] != expected:
                            return False
                        if prune_prefix is not None:
                            first = expected[0]
                            first_timestamp = (
                                select(messages.c.created_at)
                                .where(messages.c.id == first.id)
                                .scalar_subquery()
                            )
                            before = (messages.c.created_at < first_timestamp) | (
                                (messages.c.created_at == first_timestamp)
                                & (messages.c.id < first.id)
                            )
                            while True:
                                prefix = await sess.execute(
                                    select(messages.c.id, messages.c.message_data)
                                    .where(messages.c.session_id == self.session_id, before)
                                    .order_by(messages.c.created_at, messages.c.id)
                                    .limit(limit)
                                    .with_for_update()
                                )
                                batch = list(prefix.all())
                                expired_ids = []
                                for row in batch:
                                    try:
                                        item = await self._deserialize_item(row.message_data)
                                    except (json.JSONDecodeError, TypeError):
                                        break
                                    if not prune_prefix(item):
                                        break
                                    expired_ids.append(row.id)
                                if not expired_ids:
                                    break
                                await sess.execute(
                                    delete(messages).where(
                                        messages.c.session_id == self.session_id,
                                        messages.c.id.in_(expired_ids),
                                    )
                                )
                                if len(expired_ids) < limit:
                                    break
                        # Copy the timestamp in SQL before deleting the source row.
                        # Python datetime rebinding changes SQLite's stored precision,
                        # which would sort a later same-second append before its summary.
                        for output_row in payload:
                            await sess.execute(
                                insert(messages).from_select(
                                    ["session_id", "message_data", "created_at"],
                                    select(
                                        literal(self.session_id),
                                        literal(output_row["message_data"]),
                                        messages.c.created_at,
                                    ).where(messages.c.id == expected[-1].id),
                                )
                            )
                        await sess.execute(
                            delete(messages).where(
                                messages.c.session_id == self.session_id,
                                messages.c.id.in_([row.id for row in expected]),
                            )
                        )
                        await sess.execute(
                            update(self._sessions)
                            .where(self._sessions.c.session_id == self.session_id)
                            .values(updated_at=sql_text("CURRENT_TIMESTAMP"))
                        )
                        return True

            return await _await_mutation(self._run_sqlite_write_with_retry(replace))

        return _CompactionSnapshot(items, complete, replace_suffix)

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

        async def _write_items() -> None:
            async with self._session() as sess:
                async with sess.begin():
                    await self._lock_session(sess, create=True)

                    # Insert messages in bulk
                    await sess.execute(insert(self._messages), payload)

                    # Touch updated_at column
                    await sess.execute(
                        update(self._sessions)
                        .where(self._sessions.c.session_id == self.session_id)
                        .values(updated_at=sql_text("CURRENT_TIMESTAMP"))
                    )

        await _await_mutation(self._run_sqlite_write_with_retry(_write_items))

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove the most recent item after its transaction settles."""
        await self._ensure_tables()
        return await _await_mutation(self._run_sqlite_write_with_retry(self._pop_item))

    async def _pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        while True:
            retry_claim = False
            async with self._session() as sess:
                async with sess.begin():
                    if not await self._lock_session(sess):
                        return None
                    tail = (
                        select(self._messages.c.id, self._messages.c.message_data)
                        .where(self._messages.c.session_id == self.session_id)
                        .order_by(
                            self._messages.c.created_at.desc(),
                            self._messages.c.id.desc(),
                        )
                        .limit(1)
                    )

                    if self._engine.dialect.delete_returning:
                        # DELETE ... RETURNING is the claim: only the transaction that
                        # removes the current tail receives its payload. This avoids relying
                        # on DBAPI rowcount, which some dialects report as unknown.
                        result = await sess.execute(
                            delete(self._messages)
                            .where(
                                self._messages.c.id
                                == tail.with_only_columns(self._messages.c.id).scalar_subquery()
                            )
                            .returning(self._messages.c.message_data)
                        )
                        row = result.scalar_one_or_none()
                        if row is None:
                            # A concurrent DELETE can win the same tail between the
                            # subquery read and this claim. Distinguish that race from
                            # an empty session before retrying with a fresh transaction.
                            remaining = await sess.execute(
                                tail.with_only_columns(self._messages.c.id)
                            )
                            if remaining.scalar_one_or_none() is None:
                                return None
                            retry_claim = True
                    else:
                        # Dialects without DELETE ... RETURNING claim the row with a
                        # transaction-scoped lock before deleting it. The lock, rather than
                        # rowcount, establishes ownership of the returned payload.
                        result = await sess.execute(tail.with_for_update())
                        claimed = result.one_or_none()
                        if claimed is None:
                            return None
                        row_id, row = claimed
                        await sess.execute(
                            delete(self._messages).where(self._messages.c.id == row_id)
                        )

            if retry_claim:
                continue
            assert row is not None
            try:
                return await self._deserialize_item(row)
            except (json.JSONDecodeError, TypeError):
                continue

    async def clear_session(self) -> None:
        """Clear history after its transaction settles."""
        await self._ensure_tables()
        await _await_mutation(self._run_sqlite_write_with_retry(self._clear_session))

    async def _clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._session() as sess:
            async with sess.begin():
                if not await self._lock_session(sess):
                    return
                await sess.execute(
                    delete(self._messages).where(self._messages.c.session_id == self.session_id)
                )
                await sess.execute(
                    delete(self._sessions).where(self._sessions.c.session_id == self.session_id)
                )

    @property
    def engine(self) -> AsyncEngine:
        """Access the underlying SQLAlchemy AsyncEngine.

        This property provides direct access to the engine for advanced use cases,
        such as checking connection pool status, configuring engine settings,
        or manually disposing the engine when needed.

        Returns:
            AsyncEngine: The SQLAlchemy async engine instance.
        """
        return self._engine

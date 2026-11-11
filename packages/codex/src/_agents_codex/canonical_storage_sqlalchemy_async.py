"""Canonical session snapshots using a caller-owned SQLAlchemy async engine."""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import uuid
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakKeyDictionary

from ._lifecycle import _join
from ._public import preserve_module_identity as _preserve_module_identity
from ._storage_driver import _StorageDriver

if TYPE_CHECKING:
    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.pool import StaticPool


_pool_locks: WeakKeyDictionary[StaticPool, asyncio.Lock] = WeakKeyDictionary()
_pool_locks_guard = threading.Lock()


@dataclass
class _Lease:
    token: str
    stop: threading.Event
    renewal: threading.Thread | None = None
    valid: bool = True


class AsyncSQLAlchemySessionStore:
    """Persist native Sessions with a SQLAlchemy AsyncEngine on its owner loop.

    The store uses a canonical snapshot table with database-time writer leases.
    Create it on the event loop that owns the engine, close its Sessions before
    closing the store, and keep the engine available until cleanup completes.
    The caller owns a supplied engine. Use from_url to create an owned engine.
    The application owns database access control, encryption, and backups.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        table_name: str = "agents_canonical_threads",
        schema: str | None = None,
        create_tables: bool = False,
        lease_seconds: float = 30,
    ) -> None:
        from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, Text
        from sqlalchemy.ext.asyncio import AsyncEngine
        from sqlalchemy.pool import StaticPool

        if not isinstance(engine, AsyncEngine):
            raise TypeError("engine must be a SQLAlchemy AsyncEngine")
        if not table_name:
            raise ValueError("table_name must be nonempty")
        if not math.isfinite(lease_seconds) or lease_seconds < 3:
            raise ValueError("lease_seconds must be finite and at least 3")
        self.engine = engine
        self._driver = _StorageDriver(asynchronous=True, timeout=lease_seconds)
        self._lease_seconds = lease_seconds
        self._pid = os.getpid()
        self._closed = False
        self._owns_engine = False
        self._create_tables = create_tables
        self._mutex = threading.RLock()
        self._owners: dict[str, _Lease] = {}
        self._metadata = MetaData()
        self._table = Table(
            table_name,
            self._metadata,
            Column("id", String(255), primary_key=True),
            Column("owner", String(32)),
            Column("lease_until", DateTime(timezone=True)),
            Column("revision", BigInteger(), nullable=False),
            Column("write_id", String(255)),
            Column("payload", Text()),
            schema=schema,
        )
        self._database_lock: AbstractAsyncContextManager[Any] = nullcontext()
        pool = engine.pool
        if isinstance(pool, StaticPool):
            # Adapters borrowing this pool share one physical connection.
            with _pool_locks_guard:
                self._database_lock = _pool_locks.setdefault(pool, asyncio.Lock())

    @classmethod
    async def from_url(
        cls,
        url: str | URL,
        *,
        table_name: str = "agents_canonical_threads",
        schema: str | None = None,
        create_tables: bool = False,
        lease_seconds: float = 30,
        **engine_options: Any,
    ) -> AsyncSQLAlchemySessionStore:
        """Create a store that owns the async engine constructed from the URL."""
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(url, **engine_options)
        try:
            store = cls(
                engine,
                table_name=table_name,
                schema=schema,
                create_tables=create_tables,
                lease_seconds=lease_seconds,
            )
        except BaseException:
            await engine.dispose()
            raise
        store._owns_engine = True
        return store

    async def read_legacy_history(
        self,
        session_id: str,
        *,
        messages_table: str = "agent_messages",
        schema: str | None = None,
        order_by: Literal["created_at", "id"] = "created_at",
        max_items: int = 10_000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> list[dict[str, Any]]:
        """Read one v0 session's items in the order used by its source.

        Finish writes to the v0 session before reading. Use the returned items
        with an empty native Session's `transact_history` and its current revision.
        The application can retain the mapping from its v0 ID to the native ID.
        By default, items follow Classic's timestamp order with ID as the tie
        breaker. Pass `order_by="id"` if the source orders items by append ID.
        Plain v0 messages are converted to typed messages with text parts.
        Item count and encoded bytes are checked in the database before any
        message text is returned. Exceeding either limit raises ValueError.
        """
        from sqlalchemy import LargeBinary, and_, case, cast, column, func, select, table

        for name, value in (("max_items", max_items), ("max_bytes", max_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if order_by not in ("created_at", "id"):
            raise ValueError('order_by must be "created_at" or "id"')
        messages = table(
            messages_table,
            column("id"),
            column("session_id"),
            column("message_data"),
            column("created_at"),
            schema=schema,
        )
        order_columns = (
            (messages.c.created_at, messages.c.id) if order_by == "created_at" else (messages.c.id,)
        )
        bounded = (
            select(messages.c.id, messages.c.created_at, messages.c.message_data)
            .where(messages.c.session_id == session_id)
            .order_by(*order_columns)
            .limit(max_items + 1)
            .cte("legacy_bounded")
        )
        encoded_bytes = (
            func.length(cast(bounded.c.message_data, LargeBinary))
            if self.engine.dialect.name == "sqlite"
            else func.octet_length(bounded.c.message_data)
        )
        item_count = func.count().over()
        byte_count = func.coalesce(func.sum(encoded_bytes).over(), 0)
        query = select(
            item_count,
            byte_count,
            case(
                (
                    and_(item_count <= max_items, byte_count <= max_bytes),
                    bounded.c.message_data,
                )
            ),
        ).order_by(
            *((bounded.c.created_at, bounded.c.id) if order_by == "created_at" else (bounded.c.id,))
        )

        async def fetch() -> list[dict[str, Any]]:
            async with self._database_lock, self.engine.connect() as connection:
                rows = (await connection.execute(query)).all()
            if rows and rows[0][0] > max_items:
                raise ValueError("Legacy SQLAlchemy history exceeds the item limit")
            if rows and rows[0][1] > max_bytes:
                raise ValueError("Legacy SQLAlchemy history exceeds the byte limit")
            items = []
            for _, _, row in rows:
                item = json.loads(row)
                if not isinstance(item, dict):
                    raise ValueError("A v0 SQLAlchemy session item must be a JSON object")
                role = item.get("role")
                if item.get("type", "message") == "message" and role in (
                    "user",
                    "assistant",
                    "developer",
                    "system",
                ):
                    item["type"] = "message"
                    if isinstance(content := item.get("content"), str):
                        kind = "output_text" if role == "assistant" else "input_text"
                        item["content"] = [{"type": kind, "text": content}]
                items.append(item)
            return items

        def read() -> list[dict[str, Any]]:
            self._check_process()
            with self._mutex:
                self._check_process()
                return self._driver.call(fetch)

        return await _join(asyncio.create_task(asyncio.to_thread(read)))

    def _check_process(self) -> None:
        if self._closed:
            raise RuntimeError("AsyncSQLAlchemySessionStore is closed")
        if os.getpid() != self._pid:
            raise RuntimeError("AsyncSQLAlchemySessionStore cannot be reused after fork")
        self._driver.require_worker()

    def _owner(self, session_id: str, token: str) -> None:
        self._check_process()
        owner = self._owners.get(session_id)
        if owner is None or owner.token != token or not owner.valid:
            raise RuntimeError("Canonical storage writer is stale or closed")

    def _match_writer(self, session_id: str, token: str) -> Any:
        from sqlalchemy import and_, func

        columns = self._table.c
        return and_(
            columns.id == session_id,
            columns.owner == token,
            columns.lease_until > func.current_timestamp(),
        )

    async def _claim(self, session_id: str, token: str) -> None:
        from sqlalchemy import func, or_, select
        from sqlalchemy.exc import IntegrityError

        table = self._table
        async with self._database_lock:
            if self._create_tables:
                async with self.engine.begin() as connection:
                    await connection.run_sync(self._metadata.create_all)
                self._create_tables = False
            try:
                async with self.engine.begin() as connection:
                    now = await connection.scalar(select(func.current_timestamp()))
                    assert now is not None
                    deadline = now + timedelta(seconds=self._lease_seconds)
                    claimed = await connection.execute(
                        table.update()
                        .where(
                            table.c.id == session_id,
                            or_(table.c.owner.is_(None), table.c.lease_until <= now),
                        )
                        .values(owner=token, lease_until=deadline)
                    )
                    if claimed.rowcount == 1:
                        return
                    await connection.execute(
                        table.insert().values(
                            id=session_id, owner=token, lease_until=deadline, revision=0
                        )
                    )
            except IntegrityError as error:
                raise RuntimeError("Canonical storage already has a writer") from error

    def acquire(self, session_id: str) -> str:
        self._check_process()
        with self._mutex:
            self._check_process()
            if session_id in self._owners:
                raise RuntimeError("Canonical storage already has a writer")
            token = uuid.uuid4().hex
            self._driver.call(self._claim, session_id, token)
            owner = _Lease(token, threading.Event())
            owner.renewal = threading.Thread(
                target=self._renew, args=(session_id, owner), daemon=True
            )
            self._owners[session_id] = owner
            try:
                owner.renewal.start()
            except BaseException:
                self._owners.pop(session_id)
                self._driver.call(self._release, session_id, token)
                raise
            return token

    async def _extend(self, session_id: str, token: str) -> bool:
        from sqlalchemy import func, select

        async with self._database_lock, self.engine.begin() as connection:
            now = await connection.scalar(select(func.current_timestamp()))
            assert now is not None
            result = await connection.execute(
                self._table.update()
                .where(self._match_writer(session_id, token))
                .values(lease_until=now + timedelta(seconds=self._lease_seconds))
            )
            return result.rowcount == 1

    def _renew(self, session_id: str, owner: _Lease) -> None:
        while not owner.stop.wait(self._lease_seconds / 3):
            try:
                if self._driver.call(self._extend, session_id, owner.token):
                    continue
            except Exception:
                pass
            owner.valid = False
            return

    async def _read(self, session_id: str, token: str) -> str | None:
        from sqlalchemy import select

        async with self._database_lock, self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(self._table.c.payload).where(self._match_writer(session_id, token))
                )
            ).one_or_none()
            if row is None:
                raise RuntimeError("Canonical storage writer was fenced")
            return row[0]

    def read(self, session_id: str, token: str) -> str | None:
        self._check_process()
        with self._mutex:
            self._owner(session_id, token)
            return self._driver.call(self._read, session_id, token)

    async def _commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        from sqlalchemy import select

        table = self._table
        writer = self._match_writer(session_id, token)
        async with self._database_lock, self.engine.begin() as connection:
            row = (
                await connection.execute(
                    select(table.c.revision, table.c.write_id, table.c.payload).where(writer)
                )
            ).one_or_none()
            if row is None:
                raise RuntimeError("Canonical storage writer was fenced")
            revision, saved_write_id, saved_payload = row
            if (
                revision == expected_revision + 1
                and saved_write_id == write_id
                and saved_payload == payload
            ):
                return
            if revision != expected_revision:
                raise RuntimeError("Canonical storage revision conflict")
            updated = await connection.execute(
                table.update()
                .where(writer, table.c.revision == revision)
                .values(revision=expected_revision + 1, write_id=write_id, payload=payload)
            )
            if updated.rowcount != 1:
                raise RuntimeError("Canonical storage revision conflict or writer was fenced")

    def commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        self._check_process()
        document = json.loads(payload)
        if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
            raise RuntimeError("Invalid canonical revision")
        with self._mutex:
            self._owner(session_id, token)
            self._driver.call(self._commit, session_id, token, expected_revision, write_id, payload)

    def flush(self, session_id: str, token: str) -> None:
        self.read(session_id, token)

    async def _release(self, session_id: str, token: str) -> None:
        table = self._table
        async with self._database_lock, self.engine.begin() as connection:
            await connection.execute(
                table.update()
                .where(table.c.id == session_id, table.c.owner == token)
                .values(owner=None, lease_until=None)
            )

    def release(self, session_id: str, token: str) -> None:
        if os.getpid() != self._pid:
            return
        self._driver.require_worker()
        with self._mutex:
            owner = self._owners.get(session_id)
            if owner is None or owner.token != token:
                return
            owner.valid = False
            owner.stop.set()
            assert owner.renewal is not None
            owner.renewal.join()
            self._driver.call(self._release, session_id, token)
            self._owners.pop(session_id)

    def close(self) -> None:
        """Close the store after its Sessions have released their writers."""
        if os.getpid() != self._pid:
            return
        self._driver.require_worker()
        with self._mutex:
            if self._owners:
                raise RuntimeError("Close Sessions before closing AsyncSQLAlchemySessionStore")
            if self._owns_engine and not self._closed:
                self._driver.call(self.engine.dispose)
            self._closed = True

    async def aclose(self) -> None:
        """Close the store on the application loop."""
        await _join(asyncio.create_task(asyncio.to_thread(self.close)))


_preserve_module_identity(globals(), "agents.canonical_storage")

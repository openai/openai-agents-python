"""Preview application-owned canonical native thread storage.

Backends execute synchronous methods on native worker threads. They must not
reenter the SDK. Payloads are trusted opaque native records, not portable state.
"""

from __future__ import annotations

import asyncio as asyncio
import json as json
import os as os
import sqlite3 as sqlite3
import stat as stat
import sys as sys
import threading as threading
import uuid as uuid
import weakref as weakref
from collections.abc import Iterator as Iterator
from contextlib import contextmanager as contextmanager
from dataclasses import dataclass as dataclass
from pathlib import Path as Path
from typing import Any as Any, Literal, Protocol as Protocol, runtime_checkable as runtime_checkable

from ._legacy_history import (
    check_legacy_history_size as _check_legacy_history_size,
    decode_legacy_history_array as _decode_legacy_history_array,
    decode_legacy_item as _decode_legacy_item,
    validate_legacy_history_limits as _validate_legacy_history_limits,
)
from ._lifecycle import _join as _join
from ._public import preserve_module_identity as _preserve_module_identity
from .tools import ToolOutput as ToolOutput

# Register one hook without retaining adapters after their application lifetime.
# Serializing registration and ownership changes with fork prevents a child
# inheriting a descriptor before it is recorded in an adapter's owner map.
_stores: weakref.WeakSet[SQLiteSessionStore] = weakref.WeakSet()
_stores_mutex = threading.RLock()
_forking_stores: list[SQLiteSessionStore] = []


def _before_fork() -> None:
    _stores_mutex.acquire()
    _forking_stores.extend(_stores)
    for store in _forking_stores:
        store._mutex.acquire()


def _after_fork_parent() -> None:
    for store in reversed(_forking_stores):
        store._mutex.release()
    _forking_stores.clear()
    _stores_mutex.release()


def _after_fork_child() -> None:
    for store in _forking_stores:
        # Close only: LOCK_UN would also unlock the parent's shared flock.
        for _, descriptor, _ in store._owners.values():
            os.close(descriptor)
        store._owners.clear()
        store._mutex = threading.RLock()
    _forking_stores.clear()
    _stores_mutex.release()


if sys.platform != "win32" and os.name == "posix":
    os.register_at_fork(
        before=_before_fork, after_in_parent=_after_fork_parent, after_in_child=_after_fork_child
    )


@runtime_checkable
class SessionStore(Protocol):
    """Exclusive, fenced storage for opaque native snapshots.

    Acquire ownership before reading. Commit must atomically compare the stored
    revision with expected_revision and durably replace the entire payload.
    Repeating an accepted write_id with the identical payload must succeed.
    Successful commit and flush acknowledge process-crash durability. Backends
    must document their host-failure boundary. Release must relinquish ownership
    even after a failed commit or flush, retaining committed history. Repeated
    release of the same token must succeed after ownership is relinquished.
    """

    def acquire(self, session_id: str) -> str: ...
    def read(self, session_id: str, token: str) -> str | None: ...
    def commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None: ...
    def flush(self, session_id: str, token: str) -> None: ...
    def release(self, session_id: str, token: str) -> None: ...


class SessionStoreCleanup:
    """Retry only an unfinished reconciliation writer release.

    Obtain this owner through get_cleanup(error). Await aclose() after the
    backend recovers. It never repeats reconciliation or writes another receipt.
    """

    def __init__(self, owner: Any) -> None:
        self._owner: Any = owner
        self._pending: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        """Join release of the original writer; retry after a transient failure."""
        if self._owner is None:
            return
        pending = self._pending
        if pending is None or (pending.done() and (pending.cancelled() or pending.exception())):

            async def release() -> None:
                await self._owner.aclose()
                self._owner = None

            pending = asyncio.create_task(release())
            self._pending = pending
        try:
            await _join(pending)
        except BaseException as error:
            if self._owner is not None:
                error.cleanup = self  # type: ignore[attr-defined]
            raise
        finally:
            if pending.done() and self._pending is pending:
                self._pending = None


@dataclass(frozen=True)
class CustomStorage:
    """Select an application-owned canonical store for a native Session.

    This preview supports root Sessions with automatic Python tools and
    provider-hosted search, file search, code interpreter and image generation.
    Standalone native tools, environments, goals, child agents, portable state
    and manual tools are unsupported. Keep credentials and tool bindings in current application
    code. Backend errors
    fail the Session; they never select another store.
    """

    backend: SessionStore

    def __post_init__(self) -> None:
        if not isinstance(self.backend, SessionStore):
            raise TypeError("backend must implement SessionStore")

    async def reconcile(
        self, session_id: str, *, turn_id: str, call_id: str, output: str | ToolOutput
    ) -> None:
        """Record an externally verified result for an unresolved original call.

        Close the Session first. Check authoritative external records, then pass
        the actual string or ToolOutput result, preserving its original identity.
        Structured content and explicit failure retain their original meaning.
        This operation acquires exclusive ownership and never runs the tool.
        A known receipt cannot be replaced. Reopen with current tools afterward.
        If cleanup fails, get_cleanup(error) returns an owner whose aclose()
        releases the original writer without repeating the reconciliation.
        """
        from agents import _native

        if not isinstance(output, (str, ToolOutput)):
            raise TypeError("output must be str or ToolOutput")
        response = ToolOutput.text(output) if isinstance(output, str) else output
        response._validate()
        operation = _native.reconcile_canonical_store(
            self.backend, session_id, turn_id, call_id, json.dumps(response.to_native())
        )
        cleanup = SessionStoreCleanup(operation)
        try:
            await operation.run()
        except BaseException as error:
            error.cleanup = cleanup  # type: ignore[attr-defined]
            raise


class SQLiteSessionStore:
    """Persistent reference adapter for one host and a local POSIX filesystem.

    SQLite synchronous=FULL commits survive process death. Host-loss durability
    depends on the filesystem honoring fsync. OS locks release on process death;
    database fencing tokens reject stale owners. Distinct Sessions may share
    a database. Symlinks resolve once to the same per-session lock path;
    hard-linked databases are unsupported. Do not replace database or lock files
    during live use. Network filesystems and cross-host access are unsupported.
    Database, sidecar and lock files must belong to the effective user and
    already have owner-only permissions. Keep the supplied path, its ancestor
    directories and every symlink target under trusted control and protected
    from untrusted replacement during initialization and use. File checks do
    not make attacker-controlled paths safe. Keep a trusted private file
    history: mode checks cannot detect prior exposure or revoke existing
    readers. The application owns encryption and backups of sensitive data.
    """

    def __init__(self, path: Path):
        if os.name != "posix":
            raise RuntimeError("SQLiteSessionStore requires POSIX file locking")
        if not isinstance(path, Path):
            raise TypeError("SQLiteSessionStore.path must be a pathlib.Path")
        self.path = path.resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._owners: dict[str, tuple[str, int, int]] = {}
        self._database_identity: tuple[int, int] | None = None
        self._check_database(create=True)
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported canonical storage schema")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS canonical_threads "
                "(id TEXT PRIMARY KEY, token TEXT, revision INTEGER NOT NULL DEFAULT 0, "
                "write_id TEXT, payload TEXT)"
            )
            connection.execute("PRAGMA user_version=1")
        with _stores_mutex:
            _stores.add(self)

    @staticmethod
    async def read_legacy_history(
        path: Path,
        session_id: str,
        *,
        messages_table: str = "agent_messages",
        max_items: int = 10_000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> list[dict[str, Any]]:
        """Read a v0 SQLite file in insertion order for native history import.

        Finish writes to the v0 session before reading. Import the items with
        the current revision of an empty native Session's `transact_history`.
        Histories above either limit raise ValueError before item decoding.
        """
        _validate_legacy_history_limits(max_items, max_bytes)
        table = '"' + messages_table.replace('"', '""') + '"'

        def read() -> list[dict[str, Any]]:
            uri = f"{path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            try:
                connection.execute("BEGIN")
                count, size = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(item_bytes), 0) FROM ("
                    f"SELECT LENGTH(CAST(message_data AS BLOB)) AS item_bytes FROM {table} "
                    "WHERE session_id = ? LIMIT ?)",
                    (session_id, max_items + 1),
                ).fetchone()
                _check_legacy_history_size(count, size, max_items, max_bytes)
                rows = connection.execute(
                    f"SELECT message_data FROM {table} WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
                return [_decode_legacy_item(row[0]) for row in rows]
            finally:
                connection.close()

        return await _join(asyncio.create_task(asyncio.to_thread(read)))

    def _check_database(self, *, create: bool = False) -> None:
        if sys.platform == "win32":
            raise RuntimeError("SQLiteSessionStore requires POSIX file locking")
        if create:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        files = [(self.path, self.path.lstat())]
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            try:
                files.append((sidecar, sidecar.lstat()))
            except FileNotFoundError:
                pass
        for _, info in files:
            self._check_private_file(info, os.geteuid())
        info = files[0][1]
        identity = (info.st_dev, info.st_ino)
        if self._database_identity is not None and identity != self._database_identity:
            raise RuntimeError("SQLiteSessionStore database file changed")
        self._database_identity = identity
        # Inspect metadata only. Closing a raw descriptor for an existing
        # SQLite inode would release another connection's POSIX locks; chmod
        # would not revoke readers that opened a previously permissive file.

    @staticmethod
    def _check_private_file(info: os.stat_result, effective_uid: int) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("SQLiteSessionStore requires regular files")
        if info.st_uid != effective_uid:
            raise PermissionError("SQLiteSessionStore files must be owned by the effective user")
        if info.st_nlink != 1:
            raise ValueError("SQLiteSessionStore does not support hard-linked files")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(
                "SQLiteSessionStore files must already have owner-only permissions"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._check_database()
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _check(self, session_id: str, token: str) -> None:
        owner = self._owners.get(session_id)
        if owner is None or owner[0] != token or owner[2] != os.getpid():
            raise RuntimeError("Canonical storage writer is stale or closed")

    def acquire(self, session_id: str) -> str:
        if sys.platform == "win32":
            raise RuntimeError("SQLiteSessionStore requires POSIX file locking")
        import fcntl
        import hashlib

        with self._mutex:
            if session_id in self._owners:
                raise RuntimeError("Canonical storage already has a writer")
            lock_path = self.path.with_name(
                self.path.name + "." + hashlib.sha256(session_id.encode()).hexdigest() + ".lock"
            )
            descriptor = None
            try:
                self._check_database()
                try:
                    info = lock_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    self._check_private_file(info, os.geteuid())
                try:
                    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                except FileExistsError:
                    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
                self._check_private_file(os.fstat(descriptor), os.geteuid())
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                token = uuid.uuid4().hex
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO canonical_threads(id, token) VALUES (?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET token=excluded.token",
                        (session_id, token),
                    )
                self._owners[session_id] = (token, descriptor, os.getpid())
                return token
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                raise

    def read(self, session_id: str, token: str) -> str | None:
        with self._mutex:
            self._check(session_id, token)
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM canonical_threads WHERE id=? AND token=?",
                    (session_id, token),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Canonical storage writer was fenced")
                return row[0]  # type: ignore[no-any-return]

    def commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        with self._mutex:
            self._check(session_id, token)
            document = json.loads(payload)
            if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
                raise RuntimeError("Invalid canonical revision")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT token, revision, write_id, payload FROM canonical_threads WHERE id=?",
                    (session_id,),
                ).fetchone()
                if row is None or row[0] != token:
                    raise RuntimeError("Canonical storage writer was fenced")
                if row[2] == write_id and row[3] == payload:
                    # Confirm durability with a new FULL transaction even when
                    # the previous commit acknowledgment was lost.
                    connection.execute(
                        "UPDATE canonical_threads SET payload=? WHERE id=? AND token=?",
                        (payload, session_id, token),
                    )
                    return
                if row[1] != expected_revision:
                    raise RuntimeError("Canonical storage revision conflict")
                connection.execute(
                    "UPDATE canonical_threads SET revision=?, write_id=?, payload=? "
                    "WHERE id=? AND token=?",
                    (expected_revision + 1, write_id, payload, session_id, token),
                )

    def flush(self, session_id: str, token: str) -> None:
        self.read(session_id, token)

    def release(self, session_id: str, token: str) -> None:
        if sys.platform == "win32":
            raise RuntimeError("SQLiteSessionStore requires POSIX file locking")
        with self._mutex:
            owner = self._owners.get(session_id)
            if owner is None or owner[0] != token:
                return
            self._check(session_id, token)
            _, descriptor, _ = self._owners.pop(session_id)
            try:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE canonical_threads SET token=NULL WHERE id=? AND token=?",
                        (session_id, token),
                    )
            finally:
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


class SQLAlchemySessionStore(SQLiteSessionStore):
    """Use a caller-owned synchronous SQLAlchemy SQLite Engine and its pool.

    This adapter preserves SQLiteSessionStore's canonical schema, fencing, file
    privacy checks and POSIX process-crash ownership release. It uses the supplied
    Engine for every database operation; closing Sessions returns connections to
    that pool. The application owns and disposes the Engine. SQLite connections
    must permit worker-thread use. File-backed SQLite is the supported dialect;
    AsyncEngine, relative paths, in-memory databases and SQLite URI URLs are rejected.

    This does not migrate Classic item tables or support custom table names.
    All SQLiteSessionStore filesystem and durability constraints still apply.
    """

    def __init__(self, engine: Any) -> None:
        from sqlalchemy.engine import Engine

        if not isinstance(engine, Engine) or engine.dialect.is_async:
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        if engine.url.get_backend_name() != "sqlite":
            raise ValueError("SQLAlchemySessionStore currently supports SQLite only")
        database = engine.url.database
        if not database or database == ":memory:" or database.startswith("file:"):
            raise ValueError("SQLAlchemySessionStore requires a file-backed SQLite database")
        if not Path(database).is_absolute():
            raise ValueError("SQLAlchemySessionStore requires an absolute SQLite database path")
        self.engine = engine
        super().__init__(Path(database))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._check_database()
        pooled = self.engine.raw_connection()
        try:
            connection = pooled.driver_connection
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError("SQLAlchemySessionStore requires the sqlite3 DBAPI driver")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            pooled.close()


async def read_dapr_legacy_history(
    dapr_client: Any,
    session_id: str,
    *,
    state_store_name: str,
    consistency: Literal["eventual", "strong"] = "eventual",
    max_items: int = 10_000,
    max_bytes: int = 16 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Read v0 Dapr items in insertion order with an application-owned async client.

    Finish writes to the v0 session before reading from its original Dapr
    application and state store. Import the items with the current revision
    of an empty native Session's `transact_history`.
    Dapr returns the stored value as one message. Configure the client's
    receive limit for transport and use these limits for history decoding.
    """
    _validate_legacy_history_limits(max_items, max_bytes)
    result = await dapr_client.get_state(
        store_name=state_store_name,
        key=f"{session_id}:messages",
        state_metadata={"consistency": consistency},
    )
    if not result.data:
        return []
    return _decode_legacy_history_array(result.data, max_items, max_bytes)


# Re-export adapters only after their shared protocols and SQLite base exist.
from .canonical_storage_dapr import DaprSessionStore as DaprSessionStore  # noqa: E402
from .canonical_storage_encrypt import EncryptedSessionStore as EncryptedSessionStore  # noqa: E402
from .canonical_storage_mongodb import MongoDBSessionStore as MongoDBSessionStore  # noqa: E402
from .canonical_storage_redis import RedisSessionStore as RedisSessionStore  # noqa: E402
from .canonical_storage_sqlalchemy_async import (  # noqa: E402
    AsyncSQLAlchemySessionStore as AsyncSQLAlchemySessionStore,
)

_preserve_module_identity(globals(), "agents.canonical_storage")

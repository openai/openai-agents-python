"""Preview application-owned canonical native thread storage.

Backends execute synchronous methods on native worker threads. They must not
reenter the SDK. Payloads are trusted opaque native records, not portable state.
"""

import json
import os
import sqlite3
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class CanonicalThreadStore(Protocol):
    """Exclusive, fenced storage for opaque native snapshots.

    Acquire ownership before reading. Commit must atomically compare the stored
    revision with expected_revision and durably replace the entire payload.
    Repeating an accepted write_id with the identical payload must succeed.
    Successful commit and flush acknowledge process-crash durability. Backends
    must document their host-failure boundary. Release must relinquish ownership
    even after a failed commit or flush, retaining committed history. Repeated
    release of the same token must succeed after ownership is relinquished.
    """

    def acquire(self, thread_id: str) -> str: ...
    def read(self, thread_id: str, token: str) -> str | None: ...
    def commit(
        self, thread_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None: ...
    def flush(self, thread_id: str, token: str) -> None: ...
    def release(self, thread_id: str, token: str) -> None: ...


@dataclass(frozen=True)
class CanonicalStorage:
    """Select an application-owned canonical store for a native Session.

    This preview supports root Sessions with automatic Python tools. Native
    tools, environments, goals, child agents, portable state and manual tools
    are unsupported. Keep
    credentials and tool bindings in current application code. Backend errors
    fail the Session; they never select another store.
    """

    backend: CanonicalThreadStore

    def __post_init__(self) -> None:
        if not isinstance(self.backend, CanonicalThreadStore):
            raise TypeError("backend must implement CanonicalThreadStore")

    async def reconcile(self, thread_id: str, *, turn_id: str, call_id: str, output: str) -> None:
        """Record an externally verified result for an unresolved original call.

        Close the Session first. Check authoritative external records, then pass
        the actual serialized function result, preserving its original identity.
        This operation acquires exclusive ownership and never runs the tool.
        A known receipt cannot be replaced. Reopen with current tools afterward.
        """
        from . import _native

        await _native.reconcile_canonical_store(self.backend, thread_id, turn_id, call_id, output)


class SQLiteThreadStore:
    """Persistent reference adapter for one host and a local POSIX filesystem.

    SQLite synchronous=FULL commits survive process death. Host-loss durability
    depends on the filesystem honoring fsync. OS locks release on process death;
    database fencing tokens reject stale owners. Network filesystems and shared
    cross-host access are unsupported. Snapshots retain sensitive conversation
    and tool data; the application owns permissions, encryption and backups.
    """

    def __init__(self, path: Path):
        if os.name != "posix":
            raise RuntimeError("SQLiteThreadStore requires POSIX file locking")
        if not isinstance(path, Path):
            raise TypeError("SQLiteThreadStore.path must be a pathlib.Path")
        self.path = path.absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._owners: dict[str, tuple[str, int, int]] = {}
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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _check(self, thread_id: str, token: str) -> None:
        owner = self._owners.get(thread_id)
        if owner is None or owner[0] != token or owner[2] != os.getpid():
            raise RuntimeError("Canonical storage writer is stale or closed")

    def acquire(self, thread_id: str) -> str:
        if sys.platform == "win32":
            raise RuntimeError("SQLiteThreadStore requires POSIX file locking")
        import fcntl
        import hashlib

        with self._mutex:
            if thread_id in self._owners:
                raise RuntimeError("Canonical storage already has a writer")
            lock_path = self.path.with_name(
                self.path.name + "." + hashlib.sha256(thread_id.encode()).hexdigest() + ".lock"
            )
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                token = uuid.uuid4().hex
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO canonical_threads(id, token) VALUES (?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET token=excluded.token",
                        (thread_id, token),
                    )
                self._owners[thread_id] = (token, descriptor, os.getpid())
                return token
            except BaseException:
                os.close(descriptor)
                raise

    def read(self, thread_id: str, token: str) -> str | None:
        with self._mutex:
            self._check(thread_id, token)
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM canonical_threads WHERE id=? AND token=?",
                    (thread_id, token),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Canonical storage writer was fenced")
                return row[0]  # type: ignore[no-any-return]

    def commit(
        self, thread_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        with self._mutex:
            self._check(thread_id, token)
            document = json.loads(payload)
            if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
                raise RuntimeError("Invalid canonical revision")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT token, revision, write_id, payload FROM canonical_threads WHERE id=?",
                    (thread_id,),
                ).fetchone()
                if row is None or row[0] != token:
                    raise RuntimeError("Canonical storage writer was fenced")
                if row[2] == write_id and row[3] == payload:
                    # Confirm durability with a new FULL transaction even when
                    # the previous commit acknowledgment was lost.
                    connection.execute(
                        "UPDATE canonical_threads SET payload=? WHERE id=? AND token=?",
                        (payload, thread_id, token),
                    )
                    return
                if row[1] != expected_revision:
                    raise RuntimeError("Canonical storage revision conflict")
                connection.execute(
                    "UPDATE canonical_threads SET revision=?, write_id=?, payload=? "
                    "WHERE id=? AND token=?",
                    (expected_revision + 1, write_id, payload, thread_id, token),
                )

    def flush(self, thread_id: str, token: str) -> None:
        self.read(thread_id, token)

    def release(self, thread_id: str, token: str) -> None:
        with self._mutex:
            if thread_id not in self._owners:
                return
            self._check(thread_id, token)
            _, descriptor, _ = self._owners.pop(thread_id)
            try:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE canonical_threads SET token=NULL WHERE id=? AND token=?",
                        (thread_id, token),
                    )
            finally:
                os.close(descriptor)

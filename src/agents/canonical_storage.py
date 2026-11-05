"""Preview application-owned canonical native thread storage.

Backends execute synchronous methods on native worker threads. They must not
reenter the SDK. Payloads are trusted opaque native records, not portable state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .tools import ToolOutput


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


@dataclass(frozen=True)
class CustomStorage:
    """Select an application-owned canonical store for a native Session.

    This preview supports root Sessions with automatic Python tools. Native
    tools, environments, goals, child agents, portable state and manual tools
    are unsupported. Keep credentials and tool bindings in current application
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
        """
        from . import _native

        if not isinstance(output, (str, ToolOutput)):
            raise TypeError("output must be str or ToolOutput")
        response = ToolOutput.text(output) if isinstance(output, str) else output
        response._validate()
        await _native.reconcile_canonical_store(
            self.backend, session_id, turn_id, call_id, json.dumps(response.to_native())
        )


class SQLiteSessionStore:
    """Persistent reference adapter for one host and a local POSIX filesystem.

    SQLite synchronous=FULL commits survive process death. Host-loss durability
    depends on the filesystem honoring fsync. OS locks release on process death;
    database fencing tokens reject stale owners. Distinct Sessions may share
    a database. Symlinks resolve once to the same per-session lock path;
    hard-linked databases are unsupported. Do not replace database or lock files
    during live use. Network filesystems and cross-host access are unsupported.
    Database, sidecar and lock files must belong to the effective user and
    already have owner-only permissions. Keep their directory under trusted
    application control, with a trusted private file history: mode checks cannot
    detect prior exposure or revoke existing readers. The application owns
    encryption and backups of sensitive conversation data.
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
        with self._mutex:
            if session_id not in self._owners:
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
                os.close(descriptor)

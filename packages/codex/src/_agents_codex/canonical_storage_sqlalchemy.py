"""Synchronous SQLAlchemy connection integration for canonical SQLite storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ._public import preserve_module_identity as _preserve_module_identity
from .canonical_storage import SQLiteSessionStore


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


_preserve_module_identity(globals(), "agents.canonical_storage")

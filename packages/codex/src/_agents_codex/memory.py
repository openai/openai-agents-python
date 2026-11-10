"""Explicit application facts in a caller-owned, scoped SQLite database."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import Annotated, Any

from pydantic import Field

from ._function_tools import FunctionTool, function_tool
from ._public import preserve_module_identity as _preserve_module_identity

_Key = Annotated[str, Field(min_length=1, max_length=256, strict=True)]
_Value = Annotated[str, Field(max_length=65_536, strict=True)]


def _unicode(value: str, name: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{name} must contain well-formed Unicode without surrogate code points")
    return value


def _key(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must contain 1 to 256 characters and cannot be blank")
    return _unicode(value, name)


class SQLiteMemory:
    """Store explicit string facts for one trusted application-selected scope.

    Keys and scopes contain 1–256 characters; values contain at most 65,536
    characters and may be empty. Text must contain well-formed Unicode;
    surrogate code points fail before database access. The application supplies
    a private database path and authorizes tool use. Recalled values are data, not instructions.
    No expiry, extraction, encryption, or identity provider is included.
    Deletion does not erase model history, checkpoints, or exported telemetry.

    Use this owner as a context manager and finish its native Sessions before
    closing it. Operations commit before returning. Threads sharing this owner
    serialize access; other SQLite connections use a five-second busy timeout.
    """

    def __init__(self, path: str | Path, *, scope: str) -> None:
        self._scope = _key(scope, "scope")
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            path, timeout=5, isolation_level=None, check_same_thread=False
        )
        try:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS agents_memory "
                "(scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                "PRIMARY KEY (scope, key))"
            )
        except BaseException:
            self._connection.close()
            self._connection = None
            raise

    @property
    def scope(self) -> str:
        """The application-selected identity, never a model tool argument."""
        return self._scope

    def _database(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteMemory is closed")
        return self._connection

    def get(self, key: str) -> str | None:
        """Read one scoped value, or None if the key does not exist."""
        key = _key(key, "key")
        with self._lock:
            row = (
                self._database()
                .execute(
                    "SELECT value FROM agents_memory WHERE scope = ? AND key = ?",
                    (self._scope, key),
                )
                .fetchone()
            )
        return None if row is None else str(row[0])

    def set(self, key: str, value: str) -> None:
        """Replace one scoped value and commit it before returning."""
        key = _key(key, "key")
        if not isinstance(value, str) or len(value) > 65_536:
            raise ValueError("value must be a string of at most 65536 characters")
        value = _unicode(value, "value")
        with self._lock:
            self._database().execute(
                "INSERT INTO agents_memory (scope, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value",
                (self._scope, key, value),
            )

    def delete(self, key: str) -> None:
        """Delete one scoped value; deleting an absent key succeeds."""
        key = _key(key, "key")
        with self._lock:
            self._database().execute(
                "DELETE FROM agents_memory WHERE scope = ? AND key = ?", (self._scope, key)
            )

    def tools(self) -> Sequence[FunctionTool[Any, Any, Any]]:
        """Capture native tools bound to this owner, without exposing its scope."""
        with self._lock:
            self._database()

        @function_tool
        def memory_get(key: _Key) -> str | None:
            """Read an explicit saved fact for the current application scope."""
            return self.get(key)

        @function_tool
        def memory_set(key: _Key, value: _Value) -> str:
            """Save an explicit fact for the current scope when authorized by the user."""
            self.set(key, value)
            return "Saved"

        @function_tool
        def memory_delete(key: _Key) -> str:
            """Delete a fact for the current scope when authorized by the user."""
            self.delete(key)
            return "Deleted"

        return (memory_get, memory_set, memory_delete)

    def close(self) -> None:
        """Close the connection after its native work has joined; safe to repeat."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> SQLiteMemory:
        with self._lock:
            self._database()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


_preserve_module_identity(globals(), "agents.memory")

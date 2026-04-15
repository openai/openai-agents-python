"""
TBCMemorySession — DuckDB-backed Session backend that closes Loop 19 to the WM.

This is the first compositor de memoria of the Caldero runtime. It implements
the openai-agents-python Session protocol backed by DuckDB, reusing the
existing TBC warehouse (`data/tbc-warehouse.duckdb`) as the single source of
truth for episodic session memory.

Two tables are touched:
  - `caldero_session_items` (new) — raw TResponseInputItem dicts as JSONL-in-column.
    Stores the full conversation history protocol-compliant with openai-agents-python.
    One row per item added.
  - `session_summaries` (existing, from scripts/observations_schema.py) — session
    metadata (started_at_epoch, ended_at_epoch, request/investigated/learned/...).
    Upserted on add_items and clear_session.

Loop 19 closure — Phase 1 (this file): persist session turns across instances.
Loop 19 closure — Phase 2 (future): nightly compositor that distills session
items into observations (facts/decisions/findings) for semantic memory.
See memory/project_session_memory_caldero.md.

Design decisions:
1. DuckDB connection NOT thread-safe → per-instance threading.Lock()
2. One connection opened per-operation (DuckDB is fast enough, avoids stale handles)
3. Async API via asyncio.to_thread (matches SQLiteSession pattern)
4. db_path parameter required — no default, explicit per environment
5. Table auto-created if not exists (idempotent, safe to re-run)

Migration source: staging/2026-04-15-hooks-migration-inventory.md Capa C.4
Related existing code: scripts/observations_schema.py, scripts/lib/observations.py
Canonical rule: .claude/rules/block-intelligence-lens.md (Loop 19 continuous)
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import duckdb

from agents.memory.session import SessionABC
from agents.memory.session_settings import SessionSettings, resolve_session_limit

if TYPE_CHECKING:
    from agents.items import TResponseInputItem


DDL_CALDERO_SESSION_ITEMS = """
CREATE TABLE IF NOT EXISTS caldero_session_items (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at_epoch BIGINT NOT NULL,
    seq BIGINT NOT NULL,
    item_json JSON NOT NULL
);
"""

DDL_CALDERO_SESSION_ITEMS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_caldero_items_session ON caldero_session_items(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_caldero_items_seq ON caldero_session_items(session_id, seq)",
]

# session_summaries is pre-existing from scripts/observations_schema.py.
# We create-if-not-exists defensively so the Caldero can bootstrap on a
# fresh DuckDB that hasn't run the vault's observations_schema migration yet.
DDL_SESSION_SUMMARIES_FALLBACK = """
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    started_at_epoch BIGINT NOT NULL,
    ended_at_epoch BIGINT,
    request TEXT,
    investigated TEXT,
    learned TEXT,
    completed TEXT,
    next_steps TEXT,
    project TEXT DEFAULT 'tbc'
);
"""


class TBCMemorySession(SessionABC):
    """
    DuckDB-backed Session that persists conversation history to the TBC warehouse.

    Usage:
        from tbc_caldero.sessions.tbc_memory import TBCMemorySession
        from agents import Agent, Runner

        session = TBCMemorySession(
            session_id="cockpit-jorge-2026-04-15",
            db_path="data/tbc-warehouse.duckdb",
        )
        result = await Runner.run(agent, input="...", session=session)
        # Items persisted to DuckDB. Next process loads same session_id, sees items.
    """

    # Per-db-path locks to serialize DuckDB writes within a process
    _db_locks: ClassVar[dict[Path, threading.RLock]] = {}
    _db_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        session_id: str,
        db_path: str | Path,
        session_settings: SessionSettings | None = None,
    ):
        if not session_id:
            raise ValueError("session_id is required")
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Per-db-path lock for write serialization
        self._lock = self._acquire_db_lock(self.db_path)

        # Bootstrap schema (idempotent)
        with self._lock:
            with self._connect() as con:
                con.execute(DDL_CALDERO_SESSION_ITEMS)
                for idx_ddl in DDL_CALDERO_SESSION_ITEMS_INDEXES:
                    con.execute(idx_ddl)
                con.execute(DDL_SESSION_SUMMARIES_FALLBACK)
                # Upsert session_summaries row marking session as started
                now = int(time.time())
                con.execute(
                    """
                    INSERT INTO session_summaries (session_id, started_at_epoch, project)
                    VALUES (?, ?, 'tbc')
                    ON CONFLICT (session_id) DO NOTHING
                    """,
                    [self.session_id, now],
                )

    @classmethod
    def _acquire_db_lock(cls, db_path: Path) -> threading.RLock:
        key = db_path.resolve()
        with cls._db_locks_guard:
            lock = cls._db_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._db_locks[key] = lock
            return lock

    def _connect(self):
        """Fresh connection per operation. Caller must use `with`."""
        return duckdb.connect(str(self.db_path))

    # ─── Session protocol methods ─────────────────────────────────────────

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        session_limit = resolve_session_limit(limit, self.session_settings)

        def _sync() -> list[Any]:
            with self._lock:
                with self._connect() as con:
                    if session_limit is None:
                        rows = con.execute(
                            """
                            SELECT item_json FROM caldero_session_items
                            WHERE session_id = ?
                            ORDER BY seq ASC
                            """,
                            [self.session_id],
                        ).fetchall()
                    else:
                        # Latest N in chronological order
                        rows = con.execute(
                            """
                            SELECT item_json FROM (
                                SELECT item_json, seq FROM caldero_session_items
                                WHERE session_id = ?
                                ORDER BY seq DESC
                                LIMIT ?
                            )
                            ORDER BY seq ASC
                            """,
                            [self.session_id, session_limit],
                        ).fetchall()
                items: list[Any] = []
                for (item_json,) in rows:
                    try:
                        # DuckDB JSON type may return str or parsed dict depending on version
                        if isinstance(item_json, str):
                            items.append(json.loads(item_json))
                        else:
                            items.append(item_json)
                    except json.JSONDecodeError:
                        continue
                return items

        return await asyncio.to_thread(_sync)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if not items:
            return

        def _sync() -> None:
            now = int(time.time())
            with self._lock:
                with self._connect() as con:
                    # Get current max seq for this session to append correctly
                    result = con.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM caldero_session_items WHERE session_id = ?",
                        [self.session_id],
                    ).fetchone()
                    current_max = result[0] if result else 0

                    for offset, item in enumerate(items, start=1):
                        item_id = str(uuid.uuid4())
                        item_json = json.dumps(item, default=str)
                        con.execute(
                            """
                            INSERT INTO caldero_session_items
                            (id, session_id, created_at_epoch, seq, item_json)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            [
                                item_id,
                                self.session_id,
                                now,
                                current_max + offset,
                                item_json,
                            ],
                        )

                    # Loop 19 closure (Phase 1): touch session_summaries
                    # so the session is auditable from the WM side.
                    con.execute(
                        """
                        UPDATE session_summaries
                        SET ended_at_epoch = ?
                        WHERE session_id = ?
                        """,
                        [now, self.session_id],
                    )

        await asyncio.to_thread(_sync)

    async def pop_item(self) -> TResponseInputItem | None:
        def _sync() -> Any:
            with self._lock:
                with self._connect() as con:
                    # DuckDB supports DELETE ... RETURNING
                    rows = con.execute(
                        """
                        DELETE FROM caldero_session_items
                        WHERE id = (
                            SELECT id FROM caldero_session_items
                            WHERE session_id = ?
                            ORDER BY seq DESC
                            LIMIT 1
                        )
                        RETURNING item_json
                        """,
                        [self.session_id],
                    ).fetchall()
                    if not rows:
                        return None
                    item_json = rows[0][0]
                    try:
                        if isinstance(item_json, str):
                            return json.loads(item_json)
                        return item_json
                    except json.JSONDecodeError:
                        return None

        return await asyncio.to_thread(_sync)

    async def clear_session(self) -> None:
        def _sync() -> None:
            with self._lock:
                with self._connect() as con:
                    con.execute(
                        "DELETE FROM caldero_session_items WHERE session_id = ?",
                        [self.session_id],
                    )
                    # Don't delete session_summaries row — it's audit trail.
                    # Instead, mark ended_at.
                    now = int(time.time())
                    con.execute(
                        "UPDATE session_summaries SET ended_at_epoch = ? WHERE session_id = ?",
                        [now, self.session_id],
                    )

        await asyncio.to_thread(_sync)

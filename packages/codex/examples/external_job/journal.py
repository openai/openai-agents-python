"""Private application journal for one thumbnail per business order."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Literal

from agents.experimental.deferred import JobBinding, JobClaim, JobUpdate

from .store import ThumbnailStore


class Busy(Exception):
    """Another host owns this order."""


class Journal:
    def __init__(self, store: ThumbnailStore, order: str) -> None:
        self.store = store
        self.order = order
        self.scope = (store.tenant, order)
        self.key = hashlib.sha256(json.dumps(self.scope).encode()).hexdigest()
        self.generation = 0
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS orders (
                tenant TEXT, order_id TEXT, allowed INTEGER DEFAULT 1, generation INTEGER DEFAULT 0,
                binding TEXT, checkpoint TEXT, phase TEXT DEFAULT 'new', receipt_update TEXT,
                result TEXT, finished TEXT, PRIMARY KEY (tenant, order_id))""")

    @contextmanager
    def owner(self) -> Iterator[None]:
        """A process death releases the POSIX lock. The next owner gets a new fence."""
        import fcntl

        with self.store.private_lock(self.key) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy from None
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO orders (tenant,order_id) VALUES (?,?)", self.scope
                )
                changed = db.execute(
                    """UPDATE orders SET generation=generation+1
                       WHERE tenant=? AND order_id=? AND allowed=1""",
                    self.scope,
                )
                if changed.rowcount != 1:
                    raise PermissionError("Access to this order was revoked.")
                self.generation = db.execute(
                    "SELECT generation FROM orders WHERE tenant=? AND order_id=?", self.scope
                ).fetchone()[0]
            try:
                yield
            finally:
                self.generation = 0

    def _row(self, db: sqlite3.Connection, claim: JobClaim | None = None) -> sqlite3.Row:
        db.row_factory = sqlite3.Row
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM orders WHERE tenant=? AND order_id=? AND allowed=1 AND generation=?",
            (*self.scope, self.generation),
        ).fetchone()
        if row is None or self.generation == 0:
            raise PermissionError("Access to this order has expired.")
        if claim and (
            claim.generation != self.generation
            or claim.key != self.key
            or row["binding"] != json.dumps(asdict(claim.binding), sort_keys=True)
        ):
            raise PermissionError("This claim belongs to another order or owner.")
        return row

    @contextmanager
    def authorized(self, claim: JobClaim) -> Iterator[sqlite3.Connection]:
        with self.store.transaction() as db:
            self._row(db, claim)
            yield db

    def load(self) -> tuple[str | None, bool]:
        with self.store.transaction() as db:
            row = self._row(db)
            return row["checkpoint"], row["finished"] is not None

    async def prepare(self, state_json: str, bindings: tuple[JobBinding, ...]) -> None:
        if len(bindings) != 1 or (bindings[0].tenant, bindings[0].run_id) != self.scope:
            raise ValueError("This application accepts one thumbnail per order.")
        binding = json.dumps(asdict(bindings[0]), sort_keys=True)
        with self.store.transaction() as db:
            row = self._row(db)
            if row["phase"] != "new":
                if row["binding"] != binding or row["checkpoint"] != state_json:
                    raise ValueError("This order is bound to a different call.")
                return
            db.execute(
                "UPDATE orders SET phase='prepared', binding=?, checkpoint=? WHERE tenant=? AND order_id=?",
                (binding, state_json, *self.scope),
            )

    async def claim(
        self, binding: JobBinding, operation: Literal["advance", "respond", "cancel"]
    ) -> JobClaim:
        with self.store.transaction() as db:
            row = self._row(db)
            if row["binding"] != json.dumps(asdict(binding), sort_keys=True):
                raise PermissionError("This order is bound to a different call.")
            if operation != "advance":
                raise ValueError("The thumbnail worker only renders an image.")
            update = (
                JobUpdate(**json.loads(row["receipt_update"])) if row["receipt_update"] else None
            )
            action: Literal[
                "start", "reconcile_creation", "deliver", "project", "observe", "unknown"
            ]
            if row["phase"] == "prepared":
                db.execute(
                    "UPDATE orders SET phase='creating' WHERE tenant=? AND order_id=?", self.scope
                )
                action = "start"
            elif row["phase"] == "creating":
                action = "reconcile_creation"
            elif row["result"] is not None:
                action = "deliver"
            elif update and update.status == "completed":
                action = "project"
            elif update and update.receipt:
                action = "observe"
            else:
                action = "unknown"
            return JobClaim(binding, self.key, self.generation, action, update, row["result"])

    async def authorize(self, claim: JobClaim, operation: str) -> None:
        with self.store.transaction() as db:
            self._row(db, claim)

    async def record(self, claim: JobClaim, update: JobUpdate) -> None:
        with self.authorized(claim) as db:
            db.execute(
                "UPDATE orders SET phase='recorded', receipt_update=? WHERE tenant=? AND order_id=?",
                (json.dumps(asdict(update)), *self.scope),
            )

    async def save_result(self, claim: JobClaim, result_json: str) -> None:
        with self.authorized(claim) as db:
            db.execute(
                "UPDATE orders SET result=? WHERE tenant=? AND order_id=?",
                (result_json, *self.scope),
            )

    def finish(self, state_json: str) -> None:
        """Publish only a continuation for which this owner has a saved result."""
        with self.store.transaction() as db:
            if self._row(db)["result"] is None:
                raise RuntimeError("The final result has not been recorded.")
            db.execute(
                "UPDATE orders SET finished=? WHERE tenant=? AND order_id=?",
                (state_json, *self.scope),
            )

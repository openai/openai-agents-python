"""Private application state, not an SDK checkpoint or observation event log."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

TERMINAL = frozenset({"completed", "error", "cancelled", "expired", "unknown"})


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError("Use a private directory (mode 0700)")
        self.lock = (directory / "owner.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Another service owns this state directory") from None
        self.db = sqlite3.connect(directory / "application.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, key TEXT NOT NULL,
                payload_hash TEXT NOT NULL, snapshot TEXT NOT NULL,
                UNIQUE(owner,key));
            CREATE TABLE IF NOT EXISTS objects (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, receipt TEXT NOT NULL,
                kind TEXT NOT NULL, expires REAL NOT NULL, content BLOB NOT NULL);
        """)
        # A previous process may have performed an effect before losing its reply.
        # Retain the receipt and published objects; never infer that retry is safe.
        for row in self.db.execute("SELECT id,owner,snapshot FROM receipts").fetchall():
            state = json.loads(row["snapshot"])
            if state["status"] not in TERMINAL:
                self.update(row["owner"], row["id"], status="unknown", approval=None)

    def close(self):
        self.db.close()
        self.lock.close()

    def find(self, owner, key, prompt):
        row = self.db.execute(
            "SELECT id,payload_hash FROM receipts WHERE owner=? AND key=?", (owner, key)
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != hashlib.sha256(prompt.encode()).hexdigest():
            raise ValueError("This request key already names a different payload")
        return self.snapshot(owner, row["id"])

    def create(self, owner, key, payload, *, prompt, mode="scripted"):
        receipt = uuid4().hex
        state = {
            "id": receipt,
            "mode": mode,
            "prompt": prompt,
            "status": "queued",
            "approval": None,
            "card": None,
            "artifact": None,
            "final": None,
            "resync_required": False,
            "revision": 0,
        }
        with self.db:
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?)",
                (
                    receipt,
                    owner,
                    key,
                    hashlib.sha256(payload.encode()).hexdigest(),
                    json.dumps(state),
                ),
            )
        return state

    def snapshot(self, owner, receipt):
        row = self.db.execute(
            "SELECT snapshot FROM receipts WHERE owner=? AND id=?", (owner, receipt)
        ).fetchone()
        if row is None:
            raise KeyError("Not found")
        return json.loads(row["snapshot"])

    def update(self, owner, receipt, **changes):
        state = self.snapshot(owner, receipt)
        state.update(changes)
        state["revision"] += 1
        with self.db:
            self.db.execute(
                "UPDATE receipts SET snapshot=? WHERE owner=? AND id=?",
                (json.dumps(state), owner, receipt),
            )
        return state

    def put(self, owner, receipt, kind, content, *, ttl=300):
        handle = uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO objects VALUES (?,?,?,?,?,?)",
                (handle, owner, receipt, kind, time.time() + ttl, content),
            )
        return handle

    def get(self, owner, handle, kind):
        row = self.db.execute(
            "SELECT content FROM objects WHERE id=? AND owner=? AND kind=? AND expires>?",
            (handle, owner, kind, time.time()),
        ).fetchone()
        if row is None:
            raise KeyError("Not found or expired")
        return bytes(row["content"])

"""Process-local canonical storage for native lifecycle tests, without crash durability."""

import json
import threading
from uuid import uuid4


class MemorySessionStore:
    """Retain snapshots across Session opens; leave durability to adapter tests."""

    def __init__(self):
        self._mutex = threading.RLock()
        self._owners: dict[str, str] = {}
        self._payloads: dict[str, str] = {}

    def acquire(self, session_id):
        with self._mutex:
            if session_id in self._owners:
                raise RuntimeError("Canonical storage already has a writer")
            token = uuid4().hex
            self._owners[session_id] = token
            return token

    def _check(self, session_id, token):
        if self._owners.get(session_id) != token:
            raise RuntimeError("Canonical storage writer was fenced")

    def read(self, session_id, token):
        with self._mutex:
            self._check(session_id, token)
            return self._payloads.get(session_id)

    def commit(self, session_id, token, expected_revision, write_id, payload):
        with self._mutex:
            self._check(session_id, token)
            document = json.loads(payload)
            if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
                raise RuntimeError("Invalid canonical revision")
            previous = self._payloads.get(session_id)
            if previous == payload:
                return
            revision = json.loads(previous)["revision"] if previous is not None else 0
            if revision != expected_revision:
                raise RuntimeError("Canonical storage revision conflict")
            self._payloads[session_id] = payload

    def flush(self, session_id, token):
        self.read(session_id, token)

    def release(self, session_id, token):
        with self._mutex:
            if self._owners.get(session_id) == token:
                del self._owners[session_id]

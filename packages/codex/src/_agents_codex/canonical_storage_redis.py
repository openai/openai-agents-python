"""Lease-fenced Redis storage for complete native canonical snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from ._public import preserve_module_identity as _preserve_module_identity

_READ = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return redis.error_reply('Canonical storage writer was fenced')
end
return redis.call('HGET', KEYS[2], 'payload')
"""
_COMMIT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return redis.error_reply('Canonical storage writer was fenced')
end
local revision = tonumber(redis.call('HGET', KEYS[2], 'revision') or '0')
local same = redis.call('HGET', KEYS[2], 'write_id') == ARGV[3]
  and redis.call('HGET', KEYS[2], 'payload') == ARGV[4]
if not same and revision ~= tonumber(ARGV[2]) then
  return redis.error_reply('Canonical storage revision conflict')
end
redis.call('HSET', KEYS[2], 'revision', tonumber(ARGV[2]) + 1,
  'write_id', ARGV[3], 'payload', ARGV[4])
if tonumber(ARGV[5]) > 0 then
  redis.call('EXPIRE', KEYS[2], ARGV[5])
else
  redis.call('PERSIST', KEYS[2])
end
return 1
"""
_RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('PEXPIRE', KEYS[1], ARGV[2])
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
return 1
"""


@dataclass
class _Lease:
    token: str
    pid: int
    stop: threading.Event
    valid: bool = True
    thread: threading.Thread | None = None


class RedisSessionStore:
    """Store native snapshots with atomic Lua CAS and renewable writer leases.

    Supply a synchronous redis.Redis or RedisCluster client with finite socket
    timeouts. The application owns that client. Distinct Sessions use separate
    cluster hash slots. A stopped process loses its writer lease automatically;
    every read and commit checks the current fencing token. A failed renewal
    permanently invalidates that local writer even if Redis later recovers.

    Snapshots remain in Redis after release; optional ttl expires whole snapshots
    after their last commit. Redis server/host-loss durability depends on its
    persistence and replication configuration. This adapter acknowledges Redis
    commands; it does not strengthen the selected server's fsync policy. Existing
    Classic item-list keys and asynchronous clients are unsupported. Create a
    fresh adapter in a forked child; inherited adapters reject operations.
    """

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "agents:canonical",
        lease_seconds: float = 30,
        ttl: int | None = None,
    ) -> None:
        from redis import Redis
        from redis.cluster import RedisCluster

        if not isinstance(client, (Redis, RedisCluster)):
            raise TypeError("client must be a synchronous Redis or RedisCluster client")
        if not isinstance(key_prefix, str) or not key_prefix:
            raise ValueError("key_prefix must be nonempty text")
        if not isinstance(lease_seconds, (int, float)) or lease_seconds < 1:
            raise ValueError("lease_seconds must be at least one second")
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1):
            raise ValueError("ttl must be a positive integer or None")
        self.client = client
        self._owns_client = False
        self._closed = False
        self.key_prefix = key_prefix
        self.lease_milliseconds = int(lease_seconds * 1000)
        self.ttl = ttl
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        self._owners: dict[str, _Lease] = {}

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_prefix: str = "agents:canonical",
        lease_seconds: float = 30,
        ttl: int | None = None,
        **client_options: Any,
    ) -> RedisSessionStore:
        """Create an owned Redis client with bounded socket timeouts by default."""
        from redis import Redis

        client_options.setdefault("socket_timeout", 5)
        client_options.setdefault("socket_connect_timeout", 5)
        client = Redis.from_url(url, **client_options)
        try:
            store = cls(client, key_prefix=key_prefix, lease_seconds=lease_seconds, ttl=ttl)
        except BaseException:
            client.close()
            raise
        store._owns_client = True
        return store

    def ping(self) -> bool:
        """Check the selected server without changing canonical history."""
        self._check_process()
        return bool(self.client.ping())

    def close(self) -> None:
        """Close an owned client after all Session writers have released it."""
        if os.getpid() != self._pid:
            return
        with self._mutex:
            if self._owners:
                raise RuntimeError("Close Sessions before closing RedisSessionStore")
            if self._owns_client:
                self.client.close()
            self._closed = True

    def _keys(self, session_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        base = f"{self.key_prefix}:{{{digest}}}"
        return base + ":owner", base + ":snapshot"

    def _owner(self, session_id: str, token: str) -> _Lease:
        self._check_process()
        owner = self._owners.get(session_id)
        if owner is None or owner.token != token or owner.pid != os.getpid() or not owner.valid:
            raise RuntimeError("Canonical storage writer is stale or closed")
        return owner

    def _check_process(self) -> None:
        if self._closed:
            raise RuntimeError("RedisSessionStore is closed")
        if os.getpid() != self._pid:
            raise RuntimeError("RedisSessionStore cannot be reused after fork")

    def acquire(self, session_id: str) -> str:
        self._check_process()
        with self._mutex:
            self._check_process()
            if session_id in self._owners:
                raise RuntimeError("Canonical storage already has a writer")
            key, _ = self._keys(session_id)
            token = uuid.uuid4().hex
            # NX excludes live writers. A lost acknowledgement leaves a bounded
            # lease behind; do not retry with a different token in this call.
            if not self.client.set(key, token, nx=True, px=self.lease_milliseconds):
                raise RuntimeError("Canonical storage already has a writer")
            owner = _Lease(token, os.getpid(), threading.Event())

            def renew() -> None:
                while not owner.stop.wait(self.lease_milliseconds / 3000):
                    try:
                        if not self.client.eval(_RENEW, 1, key, token, self.lease_milliseconds):
                            owner.valid = False
                            return
                    except Exception:
                        owner.valid = False
                        return

            owner.thread = threading.Thread(target=renew, daemon=True, name="canonical-redis-lease")
            self._owners[session_id] = owner
            try:
                owner.thread.start()
            except BaseException:
                self._owners.pop(session_id)
                self.client.eval(_RELEASE, 1, key, token)
                raise
            return token

    def read(self, session_id: str, token: str) -> str | None:
        self._check_process()
        with self._mutex:
            self._owner(session_id, token)
            value = self.client.eval(_READ, 2, *self._keys(session_id), token)
            return value.decode("utf-8") if isinstance(value, bytes) else value

    def commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        self._check_process()
        document = json.loads(payload)
        if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
            raise RuntimeError("Invalid canonical revision")
        with self._mutex:
            self._owner(session_id, token)
            self.client.eval(
                _COMMIT,
                2,
                *self._keys(session_id),
                token,
                expected_revision,
                write_id,
                payload,
                self.ttl or 0,
            )

    def flush(self, session_id: str, token: str) -> None:
        self.read(session_id, token)

    def release(self, session_id: str, token: str) -> None:
        if os.getpid() != self._pid:
            return
        with self._mutex:
            owner = self._owners.get(session_id)
            if owner is None or owner.token != token or owner.pid != os.getpid():
                return
            owner.valid = False
            owner.stop.set()
            assert owner.thread is not None
            owner.thread.join()
            self.client.eval(_RELEASE, 1, self._keys(session_id)[0], token)
            self._owners.pop(session_id)


_preserve_module_identity(globals(), "agents.canonical_storage")

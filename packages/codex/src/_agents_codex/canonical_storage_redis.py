"""Lease-fenced Redis storage for complete native canonical snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any, cast

from ._legacy_history import (
    check_legacy_history_size,
    decode_legacy_item,
    validate_legacy_history_limits,
)
from ._lifecycle import _join
from ._public import preserve_module_identity as _preserve_module_identity
from ._storage_driver import _StorageDriver

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
_READ_LEGACY = """
local count = redis.call('LLEN', KEYS[1])
if count > tonumber(ARGV[1]) then return {count, 0, {}} end
local size = 0
local items = {}
for start = 0, count - 1, 64 do
  local page = redis.call('LRANGE', KEYS[1], start, math.min(start + 63, count - 1))
  for _, item in ipairs(page) do
    size = size + #item
    if size > tonumber(ARGV[2]) then return {count, size, {}} end
    items[#items + 1] = item
  end
end
return {count, size, items}
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

    Supply a redis.Redis or RedisCluster client with finite socket timeouts.
    For a redis.asyncio client, construct the store on its event loop and keep
    that loop running through Session cleanup. Await aclose() after the Sessions
    have closed. The application owns a supplied client. Distinct Sessions use separate
    cluster hash slots. A stopped process loses its writer lease automatically;
    every read and commit checks the current fencing token. A failed renewal
    permanently invalidates that local writer even if Redis later recovers.

    Snapshots remain in Redis after release; optional ttl expires whole snapshots
    after their last commit. Redis server/host-loss durability depends on its
    persistence and replication configuration. This adapter acknowledges Redis
    commands; it does not strengthen the selected server's fsync policy. Existing
    Classic item-list keys require migration. Create a fresh adapter in a
    forked child; inherited adapters reject operations.
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

        asynchronous = not isinstance(client, (Redis, RedisCluster))
        if asynchronous:
            from redis.asyncio import Redis as AsyncRedis, RedisCluster as AsyncRedisCluster

            if not isinstance(client, (AsyncRedis, AsyncRedisCluster)):
                raise TypeError("client must be a Redis or RedisCluster client")
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
        self._driver = _StorageDriver(asynchronous=asynchronous, timeout=lease_seconds)
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
        return bool(self._driver.call(self.client.ping))

    def close(self) -> None:
        """Close an owned client after all Session writers have released it."""
        if os.getpid() != self._pid:
            return
        self._driver.require_worker()
        with self._mutex:
            if self._owners:
                raise RuntimeError("Close Sessions before closing RedisSessionStore")
            if self._owns_client:
                self.client.close()
            self._closed = True

    async def aclose(self) -> None:
        """Close the store after its Sessions have released it."""
        await _join(asyncio.create_task(asyncio.to_thread(self.close)))

    async def read_legacy_history(
        self,
        session_id: str,
        *,
        key_prefix: str = "agents:session",
        max_items: int = 10_000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> list[dict[str, Any]]:
        """Read v0 Redis list items in insertion order for native history import.

        Finish writes to the v0 session before reading. Import the items with
        the current revision of an empty native Session's `transact_history`.
        Redis checks both limits before sending the history to the importer.
        """
        validate_legacy_history_limits(max_items, max_bytes)
        key = f"{key_prefix}:{session_id}:messages"

        def read() -> list[dict[str, Any]]:
            self._check_process()
            with self._mutex:
                self._check_process()
                count, size, rows = self._driver.call(
                    self.client.eval, _READ_LEGACY, 1, key, max_items, max_bytes
                )
                check_legacy_history_size(count, size, max_items, max_bytes)
                return [decode_legacy_item(row) for row in rows]

        return cast(list[dict[str, Any]], await _join(asyncio.create_task(asyncio.to_thread(read))))

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
        self._driver.require_worker()

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
            if not self._driver.call(
                self.client.set, key, token, nx=True, px=self.lease_milliseconds
            ):
                raise RuntimeError("Canonical storage already has a writer")
            owner = _Lease(token, os.getpid(), threading.Event())

            def renew() -> None:
                while not owner.stop.wait(self.lease_milliseconds / 3000):
                    try:
                        if not self._driver.call(
                            self.client.eval, _RENEW, 1, key, token, self.lease_milliseconds
                        ):
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
                self._driver.call(self.client.eval, _RELEASE, 1, key, token)
                raise
            return token

    def read(self, session_id: str, token: str) -> str | None:
        self._check_process()
        with self._mutex:
            self._owner(session_id, token)
            value = self._driver.call(self.client.eval, _READ, 2, *self._keys(session_id), token)
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
            self._driver.call(
                self.client.eval,
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
        self._driver.require_worker()
        with self._mutex:
            owner = self._owners.get(session_id)
            if owner is None or owner.token != token or owner.pid != os.getpid():
                return
            owner.valid = False
            owner.stop.set()
            assert owner.thread is not None
            owner.thread.join()
            self._driver.call(self.client.eval, _RELEASE, 1, self._keys(session_id)[0], token)
            self._owners.pop(session_id)


_preserve_module_identity(globals(), "agents.canonical_storage")

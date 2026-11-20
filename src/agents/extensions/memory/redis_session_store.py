from __future__ import annotations

from ...memory.resumable_session import ResumableSessionError
from ._optional_imports import raise_optional_dependency_error

try:
    from redis.asyncio import Redis
    from redis.exceptions import WatchError
except ImportError as e:
    raise_optional_dependency_error(
        "RedisSessionStore", dependency_name="redis", extra_name="redis", cause=e
    )


def _decode(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        decoded = None
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            pass
        if decoded is None:
            raise ResumableSessionError("malformed")
        return decoded
    return value


class RedisSessionStore:
    """Store complete resumable checkpoints using an application-owned Redis client.

    Use a tenant-scoped key prefix and a client with configured timeouts. The
    adapter never closes the injected client. Its keys are separate from
    RedisSession history keys. Existing key expiration is preserved on updates;
    new records have no expiration unless the application sets one.
    """

    def __init__(self, redis_client: Redis, *, key_prefix: str = "agents:resumable") -> None:
        self._redis = redis_client
        self._key_prefix = key_prefix

    def _key(self, session_id: str) -> str:
        return f"{self._key_prefix}:{session_id}"

    async def load(self, session_id: str) -> str | None:
        """Read one complete checkpoint without silently creating the session."""
        return _decode(await self._redis.get(self._key(session_id)))

    async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
        """Create or atomically replace the exact previously loaded checkpoint."""
        key = self._key(session_id)
        if expected is None:
            return bool(await self._redis.set(key, value, nx=True))
        async with self._redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            if _decode(await pipe.get(key)) != expected:
                return False
            pipe.multi()
            pipe.set(key, value, keepttl=True)
            try:
                await pipe.execute()
            except WatchError:
                return False
        return True

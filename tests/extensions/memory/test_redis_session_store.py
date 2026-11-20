from __future__ import annotations

import asyncio
from typing import cast

import pytest

pytest.importorskip("redis")
pytest.importorskip("fakeredis.aioredis")
import fakeredis.aioredis as fakeredis
from redis.asyncio import Redis

from agents import ResumableSession, ResumableSessionError
from agents.extensions.memory import RedisSessionStore
from agents.extensions.memory.redis_session_store import (
    RedisSessionStore as DirectRedisSessionStore,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("decode_responses", [False, True])
async def test_redis_checkpoint_creation_and_compare_replace_preserve_expiration(
    decode_responses: bool,
) -> None:
    redis = fakeredis.FakeRedis(decode_responses=decode_responses)
    try:
        store = RedisSessionStore(cast(Redis, redis), key_prefix="tenant:test")
        assert RedisSessionStore is DirectRedisSessionStore
        assert await store.load("missing") is None
        assert await store.save("test", "first", expected=None)
        assert not await store.save("test", "other", expected=None)
        await redis.expire("tenant:test:test", 60)
        assert await store.save("test", "second", expected="first")
        assert 0 < await redis.ttl("tenant:test:test") <= 60
        assert await store.load("test") == "second"
        assert not await store.save("test", "stale", expected="first")
        await redis.delete("tenant:test:test")
        assert not await store.save("test", "recreated", expected="second")
        assert await store.load("test") is None
        assert await redis.ping()
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_two_redis_clients_cannot_claim_the_same_checkpoint() -> None:
    server = fakeredis.FakeServer()
    first = fakeredis.FakeRedis(server=server)
    second = fakeredis.FakeRedis(server=server)
    try:
        stores = [RedisSessionStore(cast(Redis, client)) for client in [first, second]]
        assert await stores[0].save("test", "ready", expected=None)
        results = await asyncio.gather(
            stores[0].save("test", "attempt-a", expected="ready"),
            stores[1].save("test", "attempt-b", expected="ready"),
        )
        assert results.count(True) == 1
        assert await stores[0].load("test") in ("attempt-a", "attempt-b")
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_redis_handle_requires_existing_valid_state() -> None:
    redis = fakeredis.FakeRedis()
    try:
        store = RedisSessionStore(cast(Redis, redis))
        session = await ResumableSession.create(store)
        restored = await ResumableSession.resume(store, session.session_id)
        assert restored.session_id == session.session_id
        await redis.delete(f"agents:resumable:{session.session_id}")
        with pytest.raises(ResumableSessionError) as missing:
            await ResumableSession.resume(store, session.session_id)
        assert missing.value.reason == "missing"
        await redis.set("agents:resumable:invalid", b"\xff")
        with pytest.raises(ResumableSessionError) as malformed:
            await ResumableSession.resume(store, "invalid")
        assert malformed.value.reason == "malformed"
        assert malformed.value.__context__ is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "save"])
async def test_redis_checkpoint_errors_drop_sensitive_sdk_traceback_locals(operation: str) -> None:
    marker = "synthetic-redis-checkpoint-private-value"
    redis = fakeredis.FakeRedis()
    try:
        store = RedisSessionStore(cast(Redis, redis))
        await redis.set("agents:resumable:sensitive", marker.encode() + b"\xff")
        with pytest.raises(ResumableSessionError) as error:
            if operation == "load":
                await store.load("sensitive")
            else:
                await store.save("sensitive", marker, expected=marker)
        tb = error.value.__traceback__
        while tb is not None:
            if "/src/agents/" in tb.tb_frame.f_code.co_filename.replace("\\", "/"):
                assert marker not in repr(tb.tb_frame.f_locals)
            tb = tb.tb_next
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "save"])
async def test_redis_backend_error_does_not_expose_checkpoint_data(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import ResponseError

    marker = "synthetic-redis-server-private-value"
    redis = fakeredis.FakeRedis()

    async def fail(*args: object, **kwargs: object) -> None:
        raise ResponseError(marker)

    try:
        store = RedisSessionStore(cast(Redis, redis))
        monkeypatch.setattr(redis, "get" if operation == "load" else "set", fail)
        with pytest.raises(ResumableSessionError) as error:
            if operation == "load":
                await store.load("sensitive")
            else:
                await store.save("sensitive", marker, expected=None)
        assert error.value.reason == "unavailable"
        assert marker not in str(error.value)
        tb = error.value.__traceback__
        while tb is not None:
            if "/src/agents/" in tb.tb_frame.f_code.co_filename.replace("\\", "/"):
                assert marker not in repr(tb.tb_frame.f_locals)
            tb = tb.tb_next
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
    finally:
        await redis.aclose()

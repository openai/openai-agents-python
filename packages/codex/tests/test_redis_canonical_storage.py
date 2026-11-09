"""Lease ownership and snapshot CAS against an independent real Redis process."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from redis import Redis
from redis.exceptions import ConnectionError, ResponseError

from agents import Agent, CustomStorage, NativeProvider, RedisSessionStore
from tests.native_fixtures import assistant_output, responses as responses


@pytest.fixture
def redis_client(tmp_path: Path):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is required for real Redis adapter checks")
    socket = tmp_path / "redis.sock"
    process = subprocess.Popen(
        [
            executable,
            "--port",
            "0",
            "--unixsocket",
            str(socket),
            "--unixsocketperm",
            "700",
            "--save",
            "",
            "--appendonly",
            "yes",
            "--appendfsync",
            "always",
            "--dir",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = Redis(unix_socket_path=str(socket), socket_timeout=2, socket_connect_timeout=2)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                client.ping()
                break
            except ConnectionError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("Local Redis did not become ready")
                time.sleep(0.01)
        yield client
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def payload(revision=1, write_id="one"):
    return json.dumps({"revision": revision, "write_id": write_id, "items": ["saved"]})


def test_redis_snapshot_reopens_and_rejects_competing_or_stale_writers(redis_client) -> None:
    first = RedisSessionStore(redis_client)
    second = RedisSessionStore(redis_client)
    assert CustomStorage(first).backend is first
    token = first.acquire("thread")
    assert first.read("thread", token) is None
    try:
        with pytest.raises(RuntimeError, match="already has a writer"):
            second.acquire("thread")
        first.commit("thread", token, 0, "one", payload())
        first.commit("thread", token, 0, "one", payload())
        first.flush("thread", token)
        with pytest.raises(ResponseError, match="revision conflict"):
            first.commit("thread", token, 0, "other", payload(write_id="other"))
    finally:
        first.release("thread", token)
    first.release("thread", token)
    successor = second.acquire("thread")
    try:
        assert second.read("thread", successor) == payload()
        with pytest.raises(RuntimeError, match="stale or closed"):
            first.commit("thread", token, 1, "two", payload(2, "two"))
        # An obsolete release cannot remove the new owner's lease.
        first.release("thread", token)
        assert second.read("thread", successor) == payload()
    finally:
        second.release("thread", successor)


def test_redis_lost_acknowledgement_retries_identical_snapshot(redis_client, monkeypatch) -> None:
    store = RedisSessionStore(redis_client)
    token = store.acquire("thread")
    original = redis_client.eval
    lost = False

    def lose_ack(script, *args):
        nonlocal lost
        result = original(script, *args)
        if "'HSET'" in script and not lost:
            lost = True
            raise ConnectionError("synthetic lost acknowledgement")
        return result

    monkeypatch.setattr(redis_client, "eval", lose_ack)
    try:
        with pytest.raises(ConnectionError, match="lost acknowledgement"):
            store.commit("thread", token, 0, "one", payload())
        store.commit("thread", token, 0, "one", payload())
        assert store.read("thread", token) == payload()
    finally:
        store.release("thread", token)


def test_redis_expired_lease_fences_original_writer_without_deleting_snapshot(redis_client) -> None:
    first = RedisSessionStore(redis_client, lease_seconds=1)
    token = first.acquire("thread")
    first.commit("thread", token, 0, "one", payload())
    # Controlled expiry models an unavailable/stopped renewer without sleeping
    # through real model work. The successor acquires only after Redis expiry.
    redis_client.pexpire(first._keys("thread")[0], 0)
    second = RedisSessionStore(redis_client)
    successor = second.acquire("thread")
    try:
        with pytest.raises((ResponseError, RuntimeError), match="fenced|stale or closed"):
            first.read("thread", token)
        with pytest.raises((ResponseError, RuntimeError), match="fenced|stale or closed"):
            first.commit("thread", token, 1, "two", payload(2, "two"))
        first.release("thread", token)
        assert second.read("thread", successor) == payload()
    finally:
        first.release("thread", token)
        second.release("thread", successor)


def test_redis_background_renewal_preserves_owner_and_failure_invalidates_it(
    redis_client, monkeypatch
) -> None:
    import threading

    store = RedisSessionStore(redis_client, lease_seconds=1)
    original = redis_client.eval
    renewed = threading.Event()
    fail = threading.Event()

    def observe_renewal(script, *args):
        if "'PEXPIRE'" in script:
            if fail.is_set():
                raise ConnectionError("synthetic renewal failure")
            result = original(script, *args)
            renewed.set()
            return result
        return original(script, *args)

    monkeypatch.setattr(redis_client, "eval", observe_renewal)
    token = store.acquire("thread")
    try:
        assert renewed.wait(3), "background renewal did not run"
        store.commit("thread", token, 0, "one", payload())
        fail.set()
        owner = store._owners["thread"]
        assert owner.thread is not None
        owner.thread.join(timeout=3)
        assert not owner.thread.is_alive()
        with pytest.raises(RuntimeError, match="stale or closed"):
            store.read("thread", token)
    finally:
        store.release("thread", token)


def test_redis_owned_client_lifetime_and_prefix_ttl(redis_client) -> None:
    socket = redis_client.connection_pool.connection_kwargs["path"]
    store = RedisSessionStore.from_url(f"unix://{socket}", key_prefix="synthetic-prefix", ttl=30)
    assert store.ping()
    token = store.acquire("thread")
    try:
        store.commit("thread", token, 0, "one", payload())
        assert 0 < redis_client.ttl(store._keys("thread")[1]) <= 30
        with pytest.raises(RuntimeError, match="Close Sessions"):
            store.close()
    finally:
        store.release("thread", token)
    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.acquire("new")
    # An independently supplied client remains usable after adapter close.
    borrowed = RedisSessionStore(redis_client)
    borrowed.close()
    assert redis_client.ping()


async def test_redis_native_session_restores_canonical_context(redis_client, responses) -> None:
    store = RedisSessionStore(redis_client)
    responses.respond(assistant_output("first answer"))
    responses.respond(assistant_output("second answer"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-native-store")
    try:
        async with provider:
            agent = Agent(instructions="Answer.", provider=provider)
            async with agent.session(storage=CustomStorage(store)) as session:
                identity = session.id
                result = await (await session.submit("remember this input")).get_result()
                assert result.final_output == "first answer"
            async with agent.session(storage=CustomStorage(store), resume=identity) as restored:
                assert restored.id == identity
                result = await (await restored.submit("Continue.")).get_result()
                assert result.final_output == "second answer"
        assert len(responses.requests) == 2
        assert "remember this input" in json.dumps(responses.requests[1])
        assert "first answer" in json.dumps(responses.requests[1])
    finally:
        store.close()

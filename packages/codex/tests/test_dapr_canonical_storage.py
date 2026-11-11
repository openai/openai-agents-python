"""Exercise canonical Session storage against a real Dapr state component."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from agents import Agent, CustomStorage, DaprSessionStore, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses


@pytest_asyncio.fixture(loop_scope="function")
async def dapr_store_pair() -> AsyncIterator[tuple[DaprSessionStore, DaprSessionStore]]:
    pytest.importorskip("dapr")
    from dapr.aio.clients import DaprClient

    address = os.environ.get("AGENTS_TEST_DAPR_ADDRESS")
    if address is None:
        pytest.skip("requires a Dapr sidecar with a Redis state component")
    component = os.environ.get("AGENTS_TEST_DAPR_STORE", "parity-state")
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(DaprClient(address=address))
        prefix = f"native-canonical-{uuid4().hex}"
        first = DaprSessionStore(
            client, state_store_name=component, key_prefix=prefix, lease_seconds=1
        )
        second = await DaprSessionStore.from_address(
            address, state_store_name=component, key_prefix=prefix, lease_seconds=1
        )
        stack.push_async_callback(first.aclose)
        stack.push_async_callback(second.aclose)
        yield first, second


def snapshot(revision: int, write_id: str, text: str) -> str:
    return json.dumps({"revision": revision, "write_id": write_id, "content": text})


async def test_dapr_canonical_storage_fences_concurrent_writers(dapr_store_pair) -> None:
    first, second = dapr_store_pair
    thread_id = "fenced"
    attempts = await asyncio.gather(
        asyncio.to_thread(first.acquire, thread_id),
        asyncio.to_thread(second.acquire, thread_id),
        return_exceptions=True,
    )
    assert sum(isinstance(result, str) for result in attempts) == 1
    winner, follower = (first, second) if isinstance(attempts[0], str) else (second, first)
    token = next(result for result in attempts if isinstance(result, str))
    assert any(
        "already owned" in str(result) for result in attempts if isinstance(result, Exception)
    )
    try:
        assert await asyncio.to_thread(winner.read, thread_id, token) is None
        payload = snapshot(1, "initial-write", "synthetic Dapr state")
        for _ in range(2):
            await asyncio.to_thread(winner.commit, thread_id, token, 0, "initial-write", payload)
        with pytest.raises(RuntimeError, match="write ID has been reused"):
            await asyncio.to_thread(
                winner.commit,
                thread_id,
                token,
                0,
                "initial-write",
                snapshot(1, "initial-write", "different bytes"),
            )
        with pytest.raises(RuntimeError, match="revision conflict"):
            await asyncio.to_thread(
                winner.commit,
                thread_id,
                token,
                0,
                "new-write",
                snapshot(1, "new-write", "wrong revision"),
            )
        await asyncio.sleep(1.1)
        await asyncio.to_thread(winner.flush, thread_id, token)
        with pytest.raises(RuntimeError, match="already owned"):
            await asyncio.to_thread(follower.acquire, thread_id)
    finally:
        await asyncio.to_thread(winner.release, thread_id, token)

    replacement = await asyncio.to_thread(follower.acquire, thread_id)
    try:
        await asyncio.to_thread(winner.release, thread_id, token)
        assert await asyncio.to_thread(follower.read, thread_id, replacement) == payload
        with pytest.raises(RuntimeError, match="fenced"):
            await asyncio.to_thread(winner.read, thread_id, token)
    finally:
        await asyncio.to_thread(follower.release, thread_id, replacement)


async def test_dapr_renews_an_idle_session_during_unrelated_io(
    dapr_store_pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = dapr_store_pair
    slow_key, idle_key = first._key("slow"), first._key("idle")
    slow_entered = asyncio.Event()
    release_slow_read = asyncio.Event()
    idle_renewed = asyncio.Event()
    get_state = first.client.get_state
    save_state = first.client.save_state

    async def delay_read(store_name: str, key: str, **kwargs: Any) -> Any:
        if key == slow_key and not slow_entered.is_set():
            slow_entered.set()
            await release_slow_read.wait()
        return await get_state(store_name, key, **kwargs)

    async def observe_renewal(store_name: str, key: str, value: str, **kwargs: Any) -> Any:
        result = await save_state(store_name, key, value, **kwargs)
        if key == idle_key and slow_entered.is_set():
            idle_renewed.set()
        return result

    async with AsyncExitStack() as stack:
        slow_token = await asyncio.to_thread(first.acquire, "slow")
        stack.push_async_callback(asyncio.to_thread, first.release, "slow", slow_token)
        idle_token = await asyncio.to_thread(first.acquire, "idle")
        stack.push_async_callback(asyncio.to_thread, first.release, "idle", idle_token)
        monkeypatch.setattr(first.client, "get_state", delay_read)
        monkeypatch.setattr(first.client, "save_state", observe_renewal)
        slow_read = asyncio.create_task(asyncio.to_thread(first.read, "slow", slow_token))
        try:
            async with timeout(0.8):
                await slow_entered.wait()
                await idle_renewed.wait()
            assert not slow_read.done()
        finally:
            release_slow_read.set()
            await slow_read
        assert await asyncio.to_thread(first.read, "idle", idle_token) is None


async def test_dapr_rejects_incomplete_checkpoint_record(dapr_store_pair) -> None:
    first, _ = dapr_store_pair
    thread_id = "corrupt"
    key = first._key(thread_id)
    await first.client.save_state(first.state_store_name, key, "{}")
    with pytest.raises(RuntimeError, match="Invalid canonical Dapr storage record"):
        await asyncio.to_thread(first.acquire, thread_id)
    stored = await first.client.get_state(first.state_store_name, key)
    assert stored.data == b"{}"


async def test_native_session_reopens_dapr_history(dapr_store_pair, responses) -> None:
    first, second = dapr_store_pair
    responses.respond(assistant_output("first Dapr reply"))
    responses.respond(assistant_output("second Dapr reply"))
    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-dapr-store") as provider,
    ):
        agent = Agent(instructions="Answer.", provider=provider)
        async with agent.session(storage=CustomStorage(first)) as session:
            identity = session.id
            result = await (await session.submit("remember Dapr history")).get_result()
            assert result.final_output == "first Dapr reply"
        await first.aclose()
        async with agent.session(storage=CustomStorage(second), resume=identity) as session:
            assert session.id == identity
            result = await (await session.submit("Continue.")).get_result()
            assert result.final_output == "second Dapr reply"
    assert len(responses.requests) == 2
    request = json.dumps(responses.requests[1])
    assert "remember Dapr history" in request
    assert "first Dapr reply" in request


async def test_dapr_canonical_writer_expires_after_process_exit(dapr_store_pair) -> None:
    first, _ = dapr_store_pair
    thread_id = "crashed"
    payload = snapshot(1, "before-exit", "durable after process exit")
    worker = """
import asyncio
import os
import sys
from agents import DaprSessionStore

async def main():
    address, component, prefix, thread_id, payload = sys.argv[1:]
    store = await DaprSessionStore.from_address(
        address, state_store_name=component, key_prefix=prefix, lease_seconds=1
    )
    token = await asyncio.to_thread(store.acquire, thread_id)
    await asyncio.to_thread(store.commit, thread_id, token, 0, "before-exit", payload)
    os._exit(0)

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        worker,
        os.environ["AGENTS_TEST_DAPR_ADDRESS"],
        first.state_store_name,
        first.key_prefix,
        thread_id,
        payload,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with timeout(10):
            _, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    await asyncio.sleep(1.1)
    token = await asyncio.to_thread(first.acquire, thread_id)
    try:
        assert await asyncio.to_thread(first.read, thread_id, token) == payload
    finally:
        await asyncio.to_thread(first.release, thread_id, token)

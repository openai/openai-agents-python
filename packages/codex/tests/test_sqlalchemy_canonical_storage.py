"""A real SQLAlchemy SQLite pool uses the existing canonical ownership contract."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import create_async_engine

from agents import Agent, CustomStorage, NativeProvider, SQLAlchemySessionStore
from tests.native_fixtures import assistant_output, responses as responses

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SQLite adapter requires POSIX locks")


def test_sqlalchemy_pool_persists_canonical_snapshot_and_excludes_competing_writer(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.sqlite'}")
    checkouts = []
    event.listen(engine, "checkout", lambda *args: checkouts.append(True))
    try:
        first, second = SQLAlchemySessionStore(engine), SQLAlchemySessionStore(engine)
        assert CustomStorage(first).backend is first
        token = first.acquire("thread")
        payload = json.dumps({"revision": 1, "write_id": "one", "items": ["saved"]})
        try:
            with pytest.raises(BlockingIOError):
                second.acquire("thread")
            first.commit("thread", token, 0, "one", payload)
            first.commit("thread", token, 0, "one", payload)
            assert first.read("thread", token) == payload
            first.flush("thread", token)
        finally:
            first.release("thread", token)
        successor = second.acquire("thread")
        try:
            assert second.read("thread", successor) == payload
            with pytest.raises(RuntimeError, match="stale or closed"):
                first.read("thread", token)
        finally:
            second.release("thread", successor)
        assert len(checkouts) >= 10
        # Adapter cleanup returned, rather than disposed, the caller's pool.
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM canonical_threads").scalar() == 1
            )
    finally:
        engine.dispose()


def test_sqlalchemy_adapter_rejects_async_and_nonpersistent_engines(tmp_path: Path) -> None:
    memory = create_engine("sqlite:///:memory:")
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'async.sqlite'}")
    try:
        with pytest.raises(ValueError, match="file-backed"):
            SQLAlchemySessionStore(memory)
        with pytest.raises(TypeError, match="synchronous"):
            SQLAlchemySessionStore(async_engine)
        with pytest.raises(TypeError, match="synchronous"):
            SQLAlchemySessionStore(async_engine.sync_engine)
    finally:
        memory.dispose()


async def test_sqlalchemy_pool_is_usable_on_native_style_worker_threads(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'threaded.sqlite'}")
    try:
        store = SQLAlchemySessionStore(engine)
        token = await asyncio.to_thread(store.acquire, "thread")
        try:
            payload = json.dumps({"revision": 1, "write_id": "one", "items": ["saved"]})
            await asyncio.to_thread(store.commit, "thread", token, 0, "one", payload)
            assert await asyncio.to_thread(store.read, "thread", token) == payload
        finally:
            await asyncio.to_thread(store.release, "thread", token)
    finally:
        engine.dispose()


async def test_sqlalchemy_native_session_restores_canonical_context(
    tmp_path: Path, responses
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'native.sqlite'}")
    store = SQLAlchemySessionStore(engine)
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
        engine.dispose()

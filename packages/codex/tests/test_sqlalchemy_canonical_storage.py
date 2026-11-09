"""A real SQLAlchemy SQLite pool uses the existing canonical ownership contract."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import create_async_engine

from agents import CustomStorage, SQLAlchemySessionStore

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

"""Persistent adapter ownership and atomic revision contract."""

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from agents import CanonicalStorage, SQLiteThreadStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")


def _attempt(path: str, thread_id: str, result: multiprocessing.Queue) -> None:
    store = SQLiteThreadStore(Path(path))
    try:
        token = store.acquire(thread_id)
    except OSError:
        result.put("blocked")
    else:
        store.release(thread_id, token)
        result.put("acquired")


def test_sqlite_canonical_store_retains_committed_history(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    store = SQLiteThreadStore(path)
    assert CanonicalStorage(store).backend is store
    token = store.acquire("thread")
    assert store.read("thread", token) is None
    payload = json.dumps({"revision": 1, "write_id": "write-1", "items": ["first", "second"]})
    store.commit("thread", token, 0, "write-1", payload)
    store.commit("thread", token, 0, "write-1", payload)
    with pytest.raises(RuntimeError, match="revision conflict"):
        store.commit("thread", token, 0, "other", json.dumps({"revision": 1, "write_id": "other"}))
    store.flush("thread", token)
    store.release("thread", token)
    store.release("thread", token)
    reopened = SQLiteThreadStore(path)
    next_token = reopened.acquire("thread")
    assert next_token != token
    assert reopened.read("thread", next_token) == payload
    with pytest.raises(RuntimeError, match="stale or closed"):
        store.commit("thread", token, 1, "stale", "{}")
    reopened.release("thread", next_token)


def test_sqlite_excludes_competing_process_and_releases_writer(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    store = SQLiteThreadStore(path)
    token = store.acquire("thread")
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    contender = context.Process(target=_attempt, args=(str(path), "thread", result))
    contender.start()
    contender.join(10)
    assert contender.exitcode == 0
    assert result.get(timeout=5) == "blocked"
    store.release("thread", token)
    successor = context.Process(target=_attempt, args=(str(path), "thread", result))
    successor.start()
    successor.join(10)
    assert successor.exitcode == 0
    assert result.get(timeout=5) == "acquired"
    result.close()


def test_canonical_storage_rejects_incomplete_backend() -> None:
    with pytest.raises(TypeError, match="CanonicalThreadStore"):
        CanonicalStorage(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_custom_storage_rejects_goal_state_before_open(tmp_path: Path) -> None:
    from agents import Agent, NativeGoals

    backend = SQLiteThreadStore(tmp_path / "history.sqlite")
    agent = Agent(instructions="Use the provided tools.", model="fixture", tools=[NativeGoals()])
    with pytest.raises(ValueError, match="CanonicalStorage preview does not support"):
        async with agent.session(storage=CanonicalStorage(backend)):
            pytest.fail("Unsupported state must not open")
    assert not backend._owners


def test_reference_adapter_rejects_unknown_database_schema(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "history.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(RuntimeError, match="Unsupported canonical storage schema"):
        SQLiteThreadStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='canonical_threads'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_local_storage_rejects_code_mode_before_open(tmp_path: Path) -> None:
    from agents import Agent, LocalStorage

    agent = Agent(instructions="Use tools.", model="fixture", programmatic_tool_calling=True)
    path = tmp_path / "local"
    with pytest.raises(ValueError, match="nested tool identities"):
        async with agent.session(storage=LocalStorage(path)):
            pytest.fail("Unsupported persistent nested effects must not open")
    assert not path.exists()

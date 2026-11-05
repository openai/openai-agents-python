"""Persistent adapter ownership and atomic revision contract."""

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from agents import CustomStorage, SQLiteSessionStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")


def _attempt(path: str, session_id: str, result: multiprocessing.Queue) -> None:
    try:
        store = SQLiteSessionStore(Path(path))
        token = store.acquire(session_id)
    except OSError:
        result.put("blocked")
    else:
        store.release(session_id, token)
        result.put("acquired")


def test_sqlite_canonical_store_retains_committed_history(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    assert CustomStorage(store).backend is store
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
    reopened = SQLiteSessionStore(path)
    next_token = reopened.acquire("thread")
    assert next_token != token
    assert reopened.read("thread", next_token) == payload
    with pytest.raises(RuntimeError, match="stale or closed"):
        store.commit("thread", token, 1, "stale", "{}")
    reopened.release("thread", next_token)


@pytest.mark.parametrize("alias", ["same", "symlink"])
def test_sqlite_excludes_competing_process_and_releases_writer(tmp_path: Path, alias: str) -> None:
    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    token = store.acquire("thread")
    alias_path = path if alias == "same" else tmp_path / (alias + ".sqlite")
    if alias == "symlink":
        alias_path.symlink_to(path)
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    contender = context.Process(target=_attempt, args=(str(alias_path), "thread", result))
    contender.start()
    contender.join(10)
    assert contender.exitcode == 0
    assert result.get(timeout=5) == "blocked"
    assert store.read("thread", token) is None
    store.release("thread", token)
    successor = context.Process(target=_attempt, args=(str(alias_path), "thread", result))
    successor.start()
    successor.join(10)
    assert successor.exitcode == 0
    assert result.get(timeout=5) == "acquired"
    result.close()


def test_canonical_storage_rejects_incomplete_backend() -> None:
    with pytest.raises(TypeError, match="SessionStore"):
        CustomStorage(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_custom_storage_rejects_goal_state_before_open(tmp_path: Path) -> None:
    from agents import Agent, NativeGoals

    backend = SQLiteSessionStore(tmp_path / "history.sqlite")
    agent = Agent(instructions="Use the provided tools.", model="fixture", tools=[NativeGoals()])
    with pytest.raises(ValueError, match="CustomStorage preview does not support"):
        async with agent.session(storage=CustomStorage(backend)):
            pytest.fail("Unsupported state must not open")
    assert not backend._owners


def test_reference_adapter_rejects_unknown_database_schema(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "history.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(RuntimeError, match="Unsupported canonical storage schema"):
        SQLiteSessionStore(database)
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


def test_reference_adapter_keeps_database_and_journal_private(tmp_path: Path) -> None:
    import sqlite3
    import stat

    path = tmp_path / "private" / "history.sqlite"
    previous_umask = os.umask(0)
    connection = None
    try:
        SQLiteSessionStore(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        connection = sqlite3.connect(path)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO canonical_threads(id) VALUES ('synthetic')")
        journal = path.with_name(path.name + "-journal")
        assert journal.exists()
        assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        os.umask(previous_umask)


def test_reference_adapter_secures_existing_database(tmp_path: Path) -> None:
    import stat

    path = tmp_path / "history.sqlite"
    SQLiteSessionStore(path)
    path.chmod(0o666)
    SQLiteSessionStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reference_adapter_allows_distinct_sessions_in_one_database(tmp_path: Path) -> None:
    first = SQLiteSessionStore(tmp_path / "first.sqlite")
    same_database = SQLiteSessionStore(tmp_path / "first.sqlite")
    token = first.acquire(session_id="first-session")
    try:
        same_token = same_database.acquire(session_id="different-session")
        try:
            first_payload = json.dumps({"revision": 1, "write_id": "first", "items": ["one"]})
            other_payload = json.dumps({"revision": 1, "write_id": "other", "items": ["two"]})
            first.commit("first-session", token, 0, "first", first_payload)
            same_database.commit("different-session", same_token, 0, "other", other_payload)
            assert (
                same_database.read(session_id="different-session", token=same_token)
                == other_payload
            )
            assert first.read(session_id="first-session", token=token) == first_payload
        finally:
            same_database.release(session_id="different-session", token=same_token)
        other = SQLiteSessionStore(tmp_path / "other.sqlite")
        other_token = other.acquire(session_id="other-session")
        try:
            assert other.read(session_id="other-session", token=other_token) is None
            assert first.read(session_id="first-session", token=token) == first_payload
        finally:
            other.release(session_id="other-session", token=other_token)
    finally:
        first.release(session_id="first-session", token=token)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "error", "image", "mixed"])
async def test_reconcile_preserves_the_actual_tool_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from agents import ToolOutput, ToolOutputImage, ToolOutputText, _native

    image = "data:image/png;base64,c3ludGhldGlj"
    outputs = {
        "text": "plain result",
        "error": ToolOutput.error("verified external failure"),
        "image": ToolOutput.image(image),
        "mixed": ToolOutput([ToolOutputText("caption"), ToolOutputImage(image)], success=False),
    }
    expected = {
        "text": {"contentItems": [{"type": "inputText", "text": "plain result"}], "success": True},
        "error": {
            "contentItems": [{"type": "inputText", "text": "verified external failure"}],
            "success": False,
        },
        "image": {"contentItems": [{"type": "inputImage", "imageUrl": image}], "success": True},
        "mixed": {
            "contentItems": [
                {"type": "inputText", "text": "caption"},
                {"type": "inputImage", "imageUrl": image},
            ],
            "success": False,
        },
    }
    received = []

    async def reconcile(backend, session_id, turn_id, call_id, payload):
        received.append((backend, session_id, turn_id, call_id, json.loads(payload)))

    monkeypatch.setattr(_native, "reconcile_canonical_store", reconcile)
    store = SQLiteSessionStore(tmp_path / "history.sqlite")
    await CustomStorage(store).reconcile(
        session_id="session", turn_id="turn", call_id="call", output=outputs[kind]
    )
    assert received == [(store, "session", "turn", "call", expected[kind])]
    assert not store._owners


@pytest.mark.asyncio
async def test_reconcile_rejects_invalid_output_before_native_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    from agents import ToolOutput, _native

    calls = []

    async def reconcile(*args):
        calls.append(args)

    monkeypatch.setattr(_native, "reconcile_canonical_store", reconcile)
    store = SQLiteSessionStore(tmp_path / "history.sqlite")
    storage = CustomStorage(store)
    with pytest.raises(TypeError, match="str or ToolOutput"):
        await storage.reconcile(session_id="session", turn_id="turn", call_id="call", output={})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await storage.reconcile(
            session_id="session",
            turn_id="turn",
            call_id="call",
            output=ToolOutput([{"type": "unknown"}]),
        )
    assert calls == []
    assert not store._owners


def test_reference_adapter_secures_existing_wal_files(tmp_path: Path) -> None:
    import sqlite3
    import stat

    path = tmp_path / "history.sqlite"
    SQLiteSessionStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO canonical_threads(id) VALUES ('synthetic')")
        connection.commit()
        sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
        for sidecar in sidecars:
            assert sidecar.exists()
            sidecar.chmod(0o666)
        SQLiteSessionStore(path)
        assert all(stat.S_IMODE(sidecar.stat().st_mode) == 0o600 for sidecar in sidecars)
    finally:
        connection.close()


def test_reference_adapter_rejects_hardlinks_before_initialization(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "history.sqlite"
    path.touch(mode=0o600)
    alias = tmp_path / "hardlink.sqlite"
    os.link(path, alias)
    for candidate in (path, alias):
        with pytest.raises(ValueError, match="hard-linked"):
            SQLiteSessionStore(candidate)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_reference_adapter_rejects_new_hardlinks_before_fencing(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    token = store.acquire("session")
    os.link(path, tmp_path / "hardlink.sqlite")
    try:
        with pytest.raises(ValueError, match="hard-linked"):
            store.acquire("different-session")
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT id, token FROM canonical_threads").fetchall() == [
                ("session", token)
            ]
    finally:
        (tmp_path / "hardlink.sqlite").unlink()
        store.release("session", token)


def test_reference_adapter_rejects_database_replacement_before_fencing(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    old = tmp_path / "old.sqlite"
    path.rename(old)
    SQLiteSessionStore(path)
    with pytest.raises(RuntimeError, match="database file changed"):
        store.acquire("session")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM canonical_threads").fetchall() == []


def test_reference_adapter_allows_a_different_process_session_in_the_same_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    token = store.acquire("first-session")
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    contender = context.Process(target=_attempt, args=(str(path), "other-session", result))
    try:
        contender.start()
        contender.join(10)
        assert contender.exitcode == 0
        assert result.get(timeout=5) == "acquired"
        assert store.read("first-session", token) is None
    finally:
        if contender.is_alive():
            contender.kill()
            contender.join(5)
        store.release("first-session", token)
        result.close()

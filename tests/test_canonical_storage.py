"""Persistent adapter ownership and atomic revision contract."""

import json
import multiprocessing
import os
from collections.abc import Iterator
from contextlib import contextmanager
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
    database.chmod(0o600)
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


@pytest.mark.parametrize("mode", [0o644, 0o660])
@pytest.mark.parametrize("stage", ["initialization", "acquisition", "connection"])
def test_reference_adapter_rejects_permissive_database_before_writes(
    tmp_path: Path, mode: int, stage: str
) -> None:
    import stat

    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    token = store.acquire("session") if stage == "connection" else None
    path.chmod(mode)
    before = path.read_bytes()
    reader = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(PermissionError, match="owner-only"):
            if stage == "initialization":
                SQLiteSessionStore(path)
            elif stage == "acquisition":
                acquired = store.acquire("session")
                store.release("session", acquired)
            else:
                assert token is not None
                store.commit(
                    "session",
                    token,
                    0,
                    "write",
                    json.dumps({"revision": 1, "write_id": "write", "items": ["private-result"]}),
                )
        assert path.read_bytes() == before
        assert os.pread(reader, len(before), 0) == before
        assert stat.S_IMODE(path.stat().st_mode) == mode
    finally:
        os.close(reader)
        if token is not None:
            with pytest.raises(PermissionError, match="owner-only"):
                store.release("session", token)


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


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_reference_adapter_rejects_permissive_live_wal_files(tmp_path: Path, suffix: str) -> None:
    import sqlite3
    import stat

    path = tmp_path / "history.sqlite"
    SQLiteSessionStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO canonical_threads(id) VALUES ('synthetic')")
        connection.commit()
        sidecar = path.with_name(path.name + suffix)
        assert sidecar.exists()
        sidecar.chmod(0o644)
        before = path.read_bytes(), sidecar.read_bytes()
        with pytest.raises(PermissionError, match="owner-only"):
            SQLiteSessionStore(path)
        assert (path.read_bytes(), sidecar.read_bytes()) == before
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o644
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


@contextmanager
def _foreign_owner(path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use a real foreign UID as root; otherwise alter only stat metadata's UID."""
    current_uid = os.geteuid()
    if current_uid == 0:
        os.chown(path, current_uid + 1, -1)
        try:
            yield
        finally:
            os.chown(path, current_uid, -1)
        return
    # Unprivileged test hosts cannot chown a fixture to another UID. Keep real
    # files, access modes, I/O and SQLite; simulate just the ownership metadata.
    target = path.stat()
    real_fstat = os.fstat
    real_lstat = Path.lstat

    def foreign_uid(info: os.stat_result) -> os.stat_result:
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            fields = list(info)
            fields[4] = current_uid + 1
            return os.stat_result(fields)
        return info

    def fstat(descriptor: int) -> os.stat_result:
        return foreign_uid(real_fstat(descriptor))

    def lstat(candidate: Path) -> os.stat_result:
        return foreign_uid(real_lstat(candidate))

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", fstat)  # Dedicated session lock descriptors.
        patch.setattr(Path, "lstat", lstat)  # SQLite owns database and sidecar descriptors.
        yield


@pytest.mark.parametrize("stage", ["initialization", "acquisition", "connection"])
def test_reference_adapter_rejects_foreign_owner_database_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    import stat

    path = tmp_path / "history.sqlite"
    store = None if stage == "initialization" else SQLiteSessionStore(path)
    existing_token = (
        store.acquire("existing") if store is not None and stage == "connection" else None
    )
    path.touch(exist_ok=True)
    path.chmod(0o644)
    before = path.read_bytes()
    locks = list(tmp_path.glob("*.lock"))
    try:
        with _foreign_owner(path, monkeypatch):
            with pytest.raises(PermissionError, match="effective user"):
                if store is None:
                    SQLiteSessionStore(path)
                elif existing_token is not None:
                    store.commit(
                        "existing",
                        existing_token,
                        0,
                        "write",
                        json.dumps({"revision": 1, "write_id": "write", "items": ["synthetic"]}),
                    )
                else:
                    token = store.acquire("session")
                    store.release("session", token)
            assert path.read_bytes() == before
            assert stat.S_IMODE(path.stat().st_mode) == 0o644
            assert list(tmp_path.glob("*.lock")) == locks
    finally:
        if store is not None and existing_token is not None:
            with pytest.raises(PermissionError, match="owner-only"):
                store.release("existing", existing_token)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_reference_adapter_rejects_foreign_owner_sidecar_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    import stat

    path = tmp_path / "history.sqlite"
    SQLiteSessionStore(path)
    before = path.read_bytes()
    sidecar = path.with_name(path.name + suffix)
    sidecar.touch(mode=0o644)
    with _foreign_owner(sidecar, monkeypatch):
        with pytest.raises(PermissionError, match="effective user"):
            SQLiteSessionStore(path)
        assert path.read_bytes() == before
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o644
        assert sidecar.read_bytes() == b""


def test_reference_adapter_rejects_foreign_owner_lock_without_fencing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import stat

    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    before = path.read_bytes()
    lock = path.with_name(path.name + "." + hashlib.sha256(b"session").hexdigest() + ".lock")
    lock.touch(mode=0o644)
    with _foreign_owner(lock, monkeypatch):
        with pytest.raises(PermissionError, match="effective user"):
            token = store.acquire("session")
            store.release("session", token)
        assert path.read_bytes() == before
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(lock.stat().st_mode) == 0o644
        assert lock.read_bytes() == b""


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o602])
def test_reference_adapter_rejects_permissive_same_owner_lock(tmp_path: Path, mode: int) -> None:
    import hashlib
    import stat

    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    lock = path.with_name(path.name + "." + hashlib.sha256(b"session").hexdigest() + ".lock")
    lock.touch(mode=mode)
    lock.chmod(mode)
    before = path.read_bytes()
    with pytest.raises(PermissionError, match="owner-only"):
        token = store.acquire("session")
        store.release("session", token)
    assert stat.S_IMODE(lock.stat().st_mode) == mode
    assert path.read_bytes() == before


def _attempt_sqlite_writer(path: str, result: multiprocessing.Queue) -> None:
    import sqlite3

    connection = sqlite3.connect(path, timeout=0)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "locked" not in str(error):
                raise
            result.put("busy")
        else:
            result.put("acquired")
            connection.rollback()
    finally:
        connection.close()


def _sqlite_writer_result(path: Path) -> str:
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    contender = context.Process(target=_attempt_sqlite_writer, args=(str(path), result))
    try:
        contender.start()
        contender.join(10)
        assert contender.exitcode == 0
        return result.get(timeout=5)
    finally:
        if contender.is_alive():
            contender.kill()
            contender.join(5)
        result.close()


@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
def test_reference_adapter_preserves_another_connection_writer_lock(
    tmp_path: Path, journal_mode: str
) -> None:
    import sqlite3

    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    token = store.acquire("session")
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute("PRAGMA journal_mode=" + journal_mode).fetchone()[0].upper()
            == journal_mode
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE canonical_threads SET write_id='held' WHERE id='session'")
        assert _sqlite_writer_result(path) == "busy"
        # Public adapter reads include file ownership and permission admission.
        # They must not release another SQLite connection's process-owned locks.
        assert store.read("session", token) is None
        assert _sqlite_writer_result(path) == "busy"
        connection.rollback()
        assert _sqlite_writer_result(path) == "acquired"
    finally:
        connection.rollback()
        connection.close()
        store.release("session", token)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_reference_adapter_rejects_permissive_sidecar_before_initialization(
    tmp_path: Path, suffix: str
) -> None:
    import stat

    path = tmp_path / "history.sqlite"
    SQLiteSessionStore(path)
    before = path.read_bytes()
    sidecar = path.with_name(path.name + suffix)
    sidecar.touch(mode=0o644)
    with pytest.raises(PermissionError, match="owner-only"):
        SQLiteSessionStore(path)
    assert path.read_bytes() == before
    assert sidecar.read_bytes() == b""
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o644


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_reference_adapter_rejects_non_dedicated_lock_before_fencing(
    tmp_path: Path, kind: str
) -> None:
    import hashlib

    path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(path)
    lock = path.with_name(path.name + "." + hashlib.sha256(b"session").hexdigest() + ".lock")
    target = tmp_path / "target"
    target.touch(mode=0o600)
    if kind == "symlink":
        lock.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, lock)
    else:
        lock.mkdir()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="regular|hard-linked"):
        store.acquire("session")
    assert path.read_bytes() == before

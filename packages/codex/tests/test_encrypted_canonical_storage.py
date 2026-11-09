"""Encryption preserves the real canonical backend's ownership and CAS contract."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from agents import Agent, CustomStorage, EncryptedSessionStore, NativeProvider, SQLiteSessionStore
from tests.native_fixtures import assistant_output, responses as responses

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SQLite adapter requires POSIX locks")


def snapshot(revision: int = 1, write_id: str = "write-1") -> str:
    return json.dumps(
        {"revision": revision, "write_id": write_id, "items": ["SYNTHETIC_PRIVATE_秘密"]},
        ensure_ascii=False,
    )


def test_encrypted_store_reopens_without_exposing_native_contents(tmp_path: Path) -> None:
    backend = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    store = EncryptedSessionStore(backend, encryption_key="synthetic-test-key")
    assert CustomStorage(store).backend is store
    token = store.acquire("thread")
    assert store.read("thread", token) is None
    store.commit("thread", token, 0, "write-1", snapshot())
    stored = backend.read("thread", token)
    assert stored is not None and "SYNTHETIC_PRIVATE" not in stored
    store.flush("thread", token)
    store.release("thread", token)
    store.release("thread", token)
    reopened = EncryptedSessionStore(backend, encryption_key="synthetic-test-key")
    successor = reopened.acquire("thread")
    try:
        assert reopened.read("thread", successor) == snapshot()
        with pytest.raises(RuntimeError, match="stale or closed"):
            store.read("thread", token)
    finally:
        reopened.release("thread", successor)


def test_encrypted_retry_reuses_ciphertext_after_lost_acknowledgement(tmp_path: Path) -> None:
    class LostAckStore(SQLiteSessionStore):
        lost = False

        def commit(self, session_id, token, expected_revision, write_id, payload):
            super().commit(session_id, token, expected_revision, write_id, payload)
            if not self.lost:
                self.lost = True
                raise OSError("synthetic lost acknowledgement")

    backend = LostAckStore(tmp_path / "canonical.sqlite")
    store = EncryptedSessionStore(backend, encryption_key="synthetic-test-key")
    token = store.acquire("thread")
    try:
        with pytest.raises(OSError, match="lost acknowledgement"):
            store.commit("thread", token, 0, "write-1", snapshot())
        accepted = backend.read("thread", token)
        # A fresh wrapper must reconcile retries without an in-memory ciphertext cache.
        store = EncryptedSessionStore(backend, encryption_key="synthetic-test-key")
        store.commit("thread", token, 0, "write-1", snapshot())
        assert backend.read("thread", token) == accepted
        assert store.read("thread", token) == snapshot()
        with pytest.raises(RuntimeError, match="identifier conflict"):
            store.commit("thread", token, 0, "write-1", snapshot().replace("秘密", "changed"))
        with pytest.raises(RuntimeError, match="revision conflict"):
            store.commit("thread", token, 0, "different", snapshot(write_id="different"))
    finally:
        store.release("thread", token)


@pytest.mark.parametrize("tamper", ["ciphertext", "revision", "wrong-key", "wrong-session"])
def test_encrypted_store_authenticates_snapshot_before_returning(
    tmp_path: Path, tamper: str
) -> None:
    backend = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    store = EncryptedSessionStore(backend, encryption_key="synthetic-test-key")
    token = store.acquire("thread")
    store.commit("thread", token, 0, "write-1", snapshot())
    stored = backend.read("thread", token)
    assert stored is not None
    try:
        if tamper in {"ciphertext", "revision"}:
            envelope = json.loads(stored)
            if tamper == "ciphertext":
                envelope["ciphertext"] = "invalid"
            else:
                envelope["revision"] = 2
            with sqlite3.connect(tmp_path / "canonical.sqlite") as connection:
                connection.execute(
                    "UPDATE canonical_threads SET payload=? WHERE id=?",
                    (json.dumps(envelope), "thread"),
                )
            with pytest.raises(RuntimeError, match="authentication failed"):
                store.read("thread", token)
            with sqlite3.connect(tmp_path / "canonical.sqlite") as connection:
                connection.execute(
                    "UPDATE canonical_threads SET payload=? WHERE id=?", (stored, "thread")
                )
        elif tamper == "wrong-key":
            wrong = EncryptedSessionStore(backend, encryption_key="different-synthetic-key")
            with pytest.raises(RuntimeError, match="authentication failed"):
                wrong.read("thread", token)
        else:
            other_token = store.acquire("different-thread")
            try:
                backend.commit("different-thread", other_token, 0, "write-1", stored)
                with pytest.raises(RuntimeError, match="authentication failed"):
                    store.read("different-thread", other_token)
            finally:
                store.release("different-thread", other_token)
        assert store.read("thread", token) == snapshot()
    finally:
        store.release("thread", token)


async def test_encrypted_store_native_session_restores_canonical_context(
    tmp_path: Path, responses
) -> None:
    backend = SQLiteSessionStore(tmp_path / "canonical.sqlite")
    responses.respond(assistant_output("first answer"))
    responses.respond(assistant_output("second answer"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-encrypted-store")
    async with provider:
        agent = Agent(instructions="Answer.", provider=provider)
        storage = CustomStorage(EncryptedSessionStore(backend, encryption_key="synthetic-test-key"))
        async with agent.session(storage=storage) as session:
            identity = session.id
            result = await (await session.submit("SYNTHETIC_PRIVATE_秘密")).get_result()
            assert result.final_output == "first answer"
        reopened = CustomStorage(
            EncryptedSessionStore(backend, encryption_key="synthetic-test-key")
        )
        async with agent.session(storage=reopened, resume=identity) as restored:
            assert restored.id == identity
            result = await (await restored.submit("Continue.")).get_result()
            assert result.final_output == "second answer"
    assert len(responses.requests) == 2
    request = json.dumps(responses.requests[1])
    assert "SYNTHETIC_PRIVATE" in request and "first answer" in request
    token = backend.acquire(identity)
    try:
        encrypted = backend.read(identity, token)
        assert encrypted is not None
        assert "SYNTHETIC_PRIVATE" not in encrypted and "first answer" not in encrypted
    finally:
        backend.release(identity, token)

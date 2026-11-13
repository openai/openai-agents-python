"""Encryption preserves the real canonical backend's ownership and CAS contract."""

import asyncio
import json
import os
import sqlite3
import time
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


@pytest.mark.parametrize("cold", [False, True])
async def test_encrypted_ttl_excludes_old_items_from_native_model_context(
    tmp_path: Path, responses, cold: bool
) -> None:
    backend = SQLiteSessionStore(tmp_path / "canonical-ttl.sqlite")
    key = "synthetic-encrypted-ttl-key"
    storage = CustomStorage(EncryptedSessionStore(backend, encryption_key=key, ttl=0))
    responses.respond(assistant_output("OLD_ANSWER"))
    responses.respond(assistant_output("NEW_ANSWER"))

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-native-encrypted-ttl"
    ) as provider:
        agent = Agent(instructions="TTL controls model context.", provider=provider)

        async def check_expiry(session):
            assert "OLD_REQUEST" not in str((await session.model_context()).items)
            assert "OLD_ANSWER" not in str((await session.model_context()).items)
            assert "OLD_REQUEST" in str((await session.history_snapshot()).items)
            assert (await (await session.submit("NEW_REQUEST")).get_result()).final_output == (
                "NEW_ANSWER"
            )

        async with agent.session(storage=storage) as session:
            identity = session.id
            assert (await (await session.submit("OLD_REQUEST")).get_result()).final_output == (
                "OLD_ANSWER"
            )
            # Wait for the second containing the acknowledged turn to expire.
            until = int(time.time()) + 1
            while time.time() < until:
                await asyncio.sleep(0.01)
            if not cold:
                await check_expiry(session)
        if cold:
            reopened = CustomStorage(EncryptedSessionStore(backend, encryption_key=key, ttl=0))
            async with agent.session(storage=reopened, resume=identity) as session:
                await check_expiry(session)
    assert len(responses.requests) == 2
    input_json = json.dumps(responses.requests[-1]["input"])
    assert "OLD_REQUEST" not in input_json and "OLD_ANSWER" not in input_json
    assert "NEW_REQUEST" in input_json


@pytest.mark.parametrize(
    "selection", [{"type": "previous_response"}, {"type": "conversation", "id": "conv_fixture"}]
)
async def test_encrypted_ttl_rejects_provider_history_before_opening(
    tmp_path: Path, responses, selection
) -> None:
    path = tmp_path / "canonical-incompatible.sqlite"

    class UnopenedStore(SQLiteSessionStore):
        def acquire(self, session_id: str) -> str:
            pytest.fail("The incompatible Session acquired persistent storage")

    storage = CustomStorage(
        EncryptedSessionStore(UnopenedStore(path), encryption_key="synthetic-key", ttl=0)
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-history-retention", supports_websockets=False
    ) as provider:
        agent = Agent(
            instructions="Do not submit.", provider=provider, model_settings={"store": True}
        )
        with pytest.raises(ValueError, match="TTL cannot combine with provider-owned history"):
            async with agent.session(storage=storage, provider_history=selection):
                pytest.fail("An incompatible Session opened")
    assert responses.requests == []


async def test_encrypted_ttl_rejects_prepared_handoff_before_opening(tmp_path: Path, responses):
    path = tmp_path / "canonical-handoff-incompatible.sqlite"

    class UnopenedStore(SQLiteSessionStore):
        def acquire(self, session_id: str) -> str:
            pytest.fail("The incompatible Session acquired persistent storage")

    storage = CustomStorage(
        EncryptedSessionStore(UnopenedStore(path), encryption_key="synthetic-key", ttl=0)
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-history-retention", supports_websockets=False
    ) as provider:
        agent = Agent(
            instructions="Do not submit.",
            provider=provider,
            handoffs=[Agent(name="destination", instructions="Do not enter.")],
            prepare_handoff_input=lambda view: view,
        )
        with pytest.raises(ValueError, match="Handoff input preparation cannot combine"):
            async with agent.session(storage=storage):
                pytest.fail("An incompatible Session opened")
    assert responses.requests == []


@pytest.mark.parametrize(
    "operation,tamper", [("read", "revision"), ("read", "json"), ("commit", "revision")]
)
def test_encrypted_authentication_errors_discard_snapshot_frames_and_context(
    tmp_path: Path, operation: str, tamper: str
) -> None:
    path = tmp_path / "canonical.sqlite"
    backend = SQLiteSessionStore(path)
    store = EncryptedSessionStore(backend, encryption_key="synthetic-test-key")
    token = store.acquire("thread")
    try:
        store.commit("thread", token, 0, "write-1", snapshot())
        original = backend.read("thread", token)
        assert original is not None
        if tamper == "revision":
            envelope = json.loads(original)
            envelope["revision"] = 2
            corrupted = json.dumps(envelope)
        else:
            corrupted = "SYNTHETIC_PRIVATE_INVALID_JSON"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE canonical_threads SET payload=? WHERE id=?", (corrupted, "thread")
            )
        with pytest.raises(RuntimeError, match="authentication failed") as caught:
            if operation == "read":
                store.read("thread", token)
            else:
                store.commit("thread", token, 1, "write-2", snapshot(2, "write-2"))
        error = caught.value
        assert error.__context__ is None
        assert error.__cause__ is None
        trace = error.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            if frame.f_code.co_filename.endswith("canonical_storage_encrypt.py"):
                retained = repr(
                    {
                        name: value
                        for name, value in frame.f_locals.items()
                        if isinstance(value, (str, bytes, dict, list, tuple))
                    }
                )
                assert "SYNTHETIC_PRIVATE" not in retained
                assert "ciphertext" not in retained
            trace = trace.tb_next
        # Failed authentication keeps the writer and original bytes recoverable.
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE canonical_threads SET payload=? WHERE id=?", (original, "thread")
            )
        assert store.read("thread", token) == snapshot()
    finally:
        store.release("thread", token)

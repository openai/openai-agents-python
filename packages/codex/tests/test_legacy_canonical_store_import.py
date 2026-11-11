"""Migrate v0 database records using the native history and model path."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, closing
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agents import (
    Agent,
    CustomStorage,
    EncryptedSessionStore,
    MongoDBSessionStore,
    NativeProvider,
    RedisSessionStore,
    SessionStore,
    SQLiteSessionStore,
)
from agents.canonical_storage import read_dapr_legacy_history
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses

V0_ITEMS = [
    {"role": "user", "content": "v0 imported café"},
    {"type": "function_call", "name": "record", "call_id": "call_v0", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "call_v0", "output": "v0 imported tool result"},
]
NATIVE_ITEMS = [
    {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "v0 imported café"}],
    },
    *V0_ITEMS[1:],
]


async def verify_import(
    items: list[dict[str, Any]], peer: ResponsesFixture, *, storage: SessionStore | None = None
) -> None:
    assert items == NATIVE_ITEMS
    peer.respond(assistant_output("restored"))
    async with (
        timeout(20),
        NativeProvider(base_url=peer.url, api_key="synthetic-v0-import") as provider,
        Agent(instructions="Answer.", provider=provider).session(
            storage=CustomStorage(storage) if storage is not None else None
        ) as session,
    ):
        before = await session.history_snapshot()
        await session.transact_history(expected_revision=before.revision, keep=0, items=items)
        assert (await session.history_snapshot()).items[: len(items)] == tuple(items)
        turn = await session.submit("Continue.")
        assert (await turn.get_result()).final_output == "restored"
    request = json.dumps(peer.requests[0], ensure_ascii=False)
    assert "v0 imported café" in request
    assert "v0 imported tool result" in request


async def verify_import_limits(read: Callable[..., Awaitable[object]]) -> None:
    with pytest.raises(ValueError, match="exceeds max_items"):
        await read(max_items=2)
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        await read(max_bytes=1)


async def test_dapr_reader_uses_the_v0_key_and_rejects_a_non_list():
    class DaprClient:
        async def get_state(self, *, store_name, key, state_metadata):
            assert (store_name, key, state_metadata) == (
                "history",
                "legacy:messages",
                {"consistency": "strong"},
            )
            return SimpleNamespace(data=b'{"unexpected":true}')

    with pytest.raises(ValueError, match="must be a JSON list"):
        await read_dapr_legacy_history(
            DaprClient(), "legacy", state_store_name="history", consistency="strong"
        )


async def test_imports_v0_dapr_items(responses):
    address = os.environ.get("AGENTS_TEST_DAPR_ADDRESS")
    if not address:
        pytest.skip("requires AGENTS_TEST_DAPR_ADDRESS")
    pytest.importorskip("dapr")
    from dapr.aio.clients import DaprClient

    session_id = f"legacy-{uuid4()}"
    store_name = "parity-state"
    key = f"{session_id}:messages"
    wire_items = [json.dumps(V0_ITEMS[0], ensure_ascii=False), *V0_ITEMS[1:]]
    async with DaprClient(address=address) as client:
        await client.save_state(
            store_name=store_name,
            key=key,
            value=json.dumps(wire_items, ensure_ascii=False),
            state_metadata={"ttlInSeconds": "300"},
        )
        before = await client.get_state(store_name=store_name, key=key)
        items = await read_dapr_legacy_history(client, session_id, state_store_name=store_name)
        await verify_import_limits(
            partial(read_dapr_legacy_history, client, session_id, state_store_name=store_name)
        )
        after = await client.get_state(store_name=store_name, key=key)
        assert after.data == before.data
        assert after.etag == before.etag
        assert (
            await read_dapr_legacy_history(
                client, f"{session_id}-absent", state_store_name=store_name
            )
            == []
        )
    await verify_import(items, responses)


@pytest.mark.skipif(os.name != "posix", reason="encrypted SQLite fixture requires POSIX locking")
async def test_imports_authenticated_unexpired_v0_items_into_native_encrypted_storage(
    tmp_path, responses
):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    from agents.classic.extensions.memory.encrypt_session import (
        _derive_session_fernet_key,
        _ensure_fernet_key_bytes,
    )

    source_key = Fernet.generate_key().decode("ascii")
    key_bytes = _ensure_fernet_key_bytes(source_key)
    session_id = "v0-encrypted"
    cipher = _derive_session_fernet_key(key_bytes, session_id)
    other_cipher = _derive_session_fernet_key(key_bytes, "other-session")

    def envelope(item, item_cipher=cipher, *, age=0):
        payload = json.dumps(item, ensure_ascii=False).encode("utf-8")
        token = item_cipher.encrypt_at_time(payload, int(time.time()) - age).decode("ascii")
        return {"__enc__": 1, "v": 1, "kid": "v0-fixture", "payload": token}

    source = [
        envelope({"role": "user", "content": "outside limit"}),
        *(envelope(item) for item in V0_ITEMS),
        envelope({"role": "user", "content": "expired"}, age=900),
        envelope({"role": "user", "content": "wrong session"}, other_cipher),
        {"role": "user", "content": "plain text"},
    ]
    items = EncryptedSessionStore.read_legacy_history(
        source, session_id, encryption_key=source_key, ttl=600, limit=len(V0_ITEMS)
    )
    with pytest.raises(ValueError, match="exceeds max_items"):
        EncryptedSessionStore.read_legacy_history(
            source, session_id, encryption_key=source_key, max_items=1
        )
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        EncryptedSessionStore.read_legacy_history(
            source, session_id, encryption_key=source_key, max_bytes=1
        )
    assert (
        EncryptedSessionStore.read_legacy_history(
            source, "unknown-session", encryption_key=source_key, ttl=600
        )
        == []
    )
    target_file = tmp_path / "native-encrypted.sqlite"
    target = EncryptedSessionStore(
        SQLiteSessionStore(target_file), encryption_key=Fernet.generate_key().decode("ascii")
    )
    await verify_import(items, responses, storage=target)
    for database_file in tmp_path.glob("native-encrypted.sqlite*"):
        assert "v0 imported café".encode() not in database_file.read_bytes()


async def test_imports_v0_sqlite_items_without_changing_the_file(tmp_path, responses):
    path = tmp_path / "old SQLite history.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            'CREATE TABLE "old ""messages" (id INTEGER PRIMARY KEY, '
            "session_id TEXT NOT NULL, message_data TEXT NOT NULL)"
        )
        connection.executemany(
            'INSERT INTO "old ""messages" (id, session_id, message_data) VALUES (?, ?, ?)',
            [
                (1, "other", "{}"),
                (4, "legacy", json.dumps(V0_ITEMS[2])),
                (2, "legacy", json.dumps(V0_ITEMS[0])),
                (3, "legacy", json.dumps(V0_ITEMS[1])),
            ],
        )
    before = path.read_bytes()
    items = await SQLiteSessionStore.read_legacy_history(
        path, "legacy", messages_table='old "messages'
    )
    await verify_import_limits(
        partial(
            SQLiteSessionStore.read_legacy_history, path, "legacy", messages_table='old "messages'
        )
    )
    assert path.read_bytes() == before
    await verify_import(items, responses)


async def test_imports_v0_redis_list_items(responses):
    redis = pytest.importorskip("redis.asyncio")
    url = os.getenv("AGENTS_TEST_REDIS_URL")
    if url is None:
        pytest.skip("requires a Redis test server")
    prefix = f"legacy-history-{uuid4().hex}"
    key = f"{prefix}:old-session:messages"
    async with AsyncExitStack() as stack:
        client = redis.Redis.from_url(url)
        stack.push_async_callback(client.aclose)
        store = RedisSessionStore(client)
        stack.push_async_callback(store.aclose)
        stack.push_async_callback(client.delete, key)
        await client.rpush(key, *(json.dumps(item) for item in V0_ITEMS))
        await client.expire(key, 60)
        items = await store.read_legacy_history("old-session", key_prefix=prefix)
        await verify_import_limits(
            partial(store.read_legacy_history, "old-session", key_prefix=prefix)
        )
        assert await client.ttl(key) > 0
        assert await store.read_legacy_history("missing", key_prefix=prefix) == []
        await verify_import(items, responses)


@pytest.mark.parametrize("generation", [0, 3])
async def test_imports_v0_mongo_items_and_batches_from_active_generation(generation, responses):
    pymongo = pytest.importorskip("pymongo")
    uri = os.getenv("AGENTS_TEST_MONGODB_URI")
    if uri is None:
        pytest.skip("requires a MongoDB test server")
    database_name = f"legacy_history_{uuid4().hex}"
    async with AsyncExitStack() as stack:
        client = pymongo.AsyncMongoClient(uri)
        stack.push_async_callback(client.close)
        stack.push_async_callback(client.drop_database, database_name)
        database = client[database_name]
        store = MongoDBSessionStore(client, database=database_name)
        stack.push_async_callback(store.aclose)
        if generation:
            await database.heads.insert_one(
                {"session_id": "old-session", "_generation": generation}
            )
        legacy_generation = {"generation": generation} if generation else {}
        await database.messages.insert_many(
            [
                {
                    "session_id": "old-session",
                    "seq": 3,
                    "generation": generation,
                    "message_data": [json.dumps(item) for item in V0_ITEMS[1:]],
                },
                {
                    "session_id": "old-session",
                    "seq": 4,
                    "generation": generation + 1,
                    "message_data": json.dumps({"role": "user", "content": "retired generation"}),
                },
                {
                    "session_id": "old-session",
                    "seq": 1,
                    "message_data": json.dumps(V0_ITEMS[0]),
                    **legacy_generation,
                },
            ]
        )
        items = await store.read_legacy_history(
            "old-session", sessions_collection="heads", messages_collection="messages"
        )
        await verify_import_limits(
            partial(
                store.read_legacy_history,
                "old-session",
                sessions_collection="heads",
                messages_collection="messages",
            )
        )
        await verify_import(items, responses)

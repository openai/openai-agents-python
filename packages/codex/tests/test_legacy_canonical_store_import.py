"""Migrate v0 database records using the native history and model path."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AsyncExitStack, closing
from typing import Any
from uuid import uuid4

import pytest

from agents import Agent, MongoDBSessionStore, NativeProvider, RedisSessionStore, SQLiteSessionStore
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


async def verify_import(items: list[dict[str, Any]], peer: ResponsesFixture) -> None:
    assert items == NATIVE_ITEMS
    peer.respond(assistant_output("restored"))
    async with (
        timeout(20),
        NativeProvider(base_url=peer.url, api_key="synthetic-v0-import") as provider,
        Agent(instructions="Answer.", provider=provider).session() as session,
    ):
        before = await session.history_snapshot()
        await session.transact_history(expected_revision=before.revision, keep=0, items=items)
        assert (await session.history_snapshot()).items[: len(items)] == tuple(items)
        turn = await session.submit("Continue.")
        assert (await turn.get_result()).final_output == "restored"
    request = json.dumps(peer.requests[0], ensure_ascii=False)
    assert "v0 imported café" in request
    assert "v0 imported tool result" in request


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
        await verify_import(items, responses)

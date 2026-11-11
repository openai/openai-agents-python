from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import datetime
from uuid import uuid4

import pytest

from agents import Agent, AsyncSQLAlchemySessionStore, CustomStorage, NativeProvider
from tests.native_fixtures import assistant_output, responses as responses

pytest.importorskip("sqlalchemy")


@pytest.fixture(
    params=["sqlite", "postgres"] if os.getenv("AGENTS_TEST_POSTGRES_URL") else ["sqlite"]
)
def database_url(request, tmp_path):
    if request.param == "sqlite":
        pytest.importorskip("aiosqlite")
        return f"sqlite+aiosqlite:///{tmp_path / 'canonical.sqlite'}"
    return os.environ["AGENTS_TEST_POSTGRES_URL"]


@pytest.mark.asyncio
async def test_async_sqlalchemy_restores_native_session(responses, database_url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    table = f"agents_test_{uuid4().hex}"
    first_engine = create_async_engine(database_url)
    second_engine = create_async_engine(database_url)
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-native-store")
    responses.respond(assistant_output("first answer"))
    responses.respond(assistant_output("second answer"))
    async with AsyncExitStack() as stack:
        stack.push_async_callback(first_engine.dispose)
        stack.push_async_callback(second_engine.dispose)
        first = AsyncSQLAlchemySessionStore(first_engine, table_name=table, create_tables=True)
        second = AsyncSQLAlchemySessionStore(second_engine, table_name=table)
        stack.push_async_callback(first.aclose)
        stack.push_async_callback(second.aclose)
        async with provider:
            agent = Agent(instructions="Answer.", provider=provider)
            async with agent.session(storage=CustomStorage(first)) as session:
                identity = session.id
                result = await (await session.submit("remember this input")).get_result()
                assert result.final_output == "first answer"
                with pytest.raises(RuntimeError, match="already has a writer"):
                    await asyncio.to_thread(second.acquire, identity)
            async with agent.session(storage=CustomStorage(second), resume=identity) as restored:
                assert restored.id == identity
                result = await (await restored.submit("continue")).get_result()
                assert result.final_output == "second answer"
        assert "remember this input" in json.dumps(responses.requests[1])
        assert "first answer" in json.dumps(responses.requests[1])
        await first.aclose()
        async with first_engine.begin() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            await connection.execute(text(f"DROP TABLE {table}"))


@pytest.mark.asyncio
async def test_async_sqlalchemy_imports_v0_items_and_restores_native_history(
    responses, database_url
):
    from sqlalchemy import TIMESTAMP, Column, Integer, MetaData, String, Table, Text

    metadata = MetaData()
    messages = Table(
        f"legacy_messages_{uuid4().hex}",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("session_id", String, nullable=False),
        Column("message_data", Text, nullable=False),
        Column("created_at", TIMESTAMP(timezone=False), nullable=False),
    )
    items = [
        {"role": "user", "content": "legacy café"},
        {"type": "function_call", "name": "record", "call_id": "call_old", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_old", "output": "legacy tool reply"},
    ]
    earlier = datetime(2026, 1, 1)
    later = datetime(2026, 1, 2)
    legacy_rows = [
        (3, "v0", later, items[2]),
        (1, "other", earlier, {}),
        (2, "v0", later, items[1]),
        (4, "v0", earlier, items[0]),
    ]
    canonical_table = f"native_messages_{uuid4().hex}"
    async with AsyncExitStack() as stack:
        store = await AsyncSQLAlchemySessionStore.from_url(
            database_url, table_name=canonical_table, create_tables=True
        )
        stack.push_async_callback(store.aclose)
        async with store.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                messages.insert(),
                [
                    {
                        "id": row_id,
                        "session_id": source_id,
                        "created_at": created_at,
                        "message_data": json.dumps(item),
                    }
                    for row_id, source_id, created_at, item in legacy_rows
                ],
            )
        imported = await store.read_legacy_history("v0", messages_table=messages.name)
        assert imported == [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "legacy café"}],
            },
            *items[1:],
        ]
        assert await store.read_legacy_history(
            "v0", messages_table=messages.name, order_by="id"
        ) == [*imported[1:], imported[0]]
        assert await store.read_legacy_history("absent", messages_table=messages.name) == []
        async with NativeProvider(
            base_url=responses.url, api_key="synthetic-native-import"
        ) as provider:
            agent = Agent(instructions="Answer.", provider=provider)
            async with agent.session(storage=CustomStorage(store)) as session:
                identity = session.id
                snapshot = await session.history_snapshot()
                await session.transact_history(
                    expected_revision=snapshot.revision, keep=0, items=imported
                )
            async with agent.session(storage=CustomStorage(store), resume=identity) as restored:
                assert (await restored.history_snapshot()).items[: len(items)] == tuple(imported)
                responses.respond(assistant_output("restored"))
                assert (
                    await (await restored.submit("continue")).get_result()
                ).final_output == "restored"
        assert "legacy café" in json.dumps(responses.requests[0], ensure_ascii=False)
        assert "legacy tool reply" in json.dumps(responses.requests[0])
        async with store.engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)


@pytest.mark.asyncio
async def test_async_sqlalchemy_fences_expired_owner_and_retries(database_url):
    from sqlalchemy import text

    table = f"agents_test_{uuid4().hex}"
    async with AsyncExitStack() as stack:
        first = await AsyncSQLAlchemySessionStore.from_url(
            database_url, table_name=table, create_tables=True
        )
        second = await AsyncSQLAlchemySessionStore.from_url(database_url, table_name=table)
        stack.push_async_callback(first.aclose)
        stack.push_async_callback(second.aclose)
        with pytest.raises(RuntimeError, match="off the client's event loop"):
            first.acquire("thread")
        original = await asyncio.to_thread(first.acquire, "thread")
        stack.push_async_callback(asyncio.to_thread, first.release, "thread", original)
        payload = json.dumps({"revision": 1, "write_id": "first"})
        await asyncio.to_thread(first.commit, "thread", original, 0, "first", payload)
        await asyncio.to_thread(first.commit, "thread", original, 0, "first", payload)
        with pytest.raises(RuntimeError, match="revision conflict"):
            await asyncio.to_thread(
                first.commit,
                "thread",
                original,
                0,
                "first",
                json.dumps({"revision": 1, "write_id": "first", "different": True}),
            )
        async with second.engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE {table} SET lease_until = '2000-01-01 00:00:00'")
            )
        owner = await asyncio.to_thread(second.acquire, "thread")
        stack.push_async_callback(asyncio.to_thread, second.release, "thread", owner)
        with pytest.raises(RuntimeError, match="fenced|stale"):
            await asyncio.to_thread(first.read, "thread", original)
        await asyncio.to_thread(first.release, "thread", original)
        assert await asyncio.to_thread(second.read, "thread", owner) == payload
        await asyncio.to_thread(second.release, "thread", owner)
        async with second.engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE {table}"))


@pytest.mark.asyncio
async def test_async_sqlalchemy_serializes_stores_sharing_an_in_memory_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    pytest.importorskip("aiosqlite")
    engine = create_async_engine("sqlite+aiosqlite://")
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        stores = [
            AsyncSQLAlchemySessionStore(engine, create_tables=True),
            AsyncSQLAlchemySessionStore(engine.execution_options(), create_tables=True),
        ]
        for store in stores:
            stack.push_async_callback(store.aclose)

        async def save(store, session_id):
            token = await asyncio.to_thread(store.acquire, session_id)
            try:
                payload = json.dumps({"revision": 1, "write_id": session_id})
                await asyncio.to_thread(store.commit, session_id, token, 0, session_id, payload)
                assert await asyncio.to_thread(store.read, session_id, token) == payload
            finally:
                await asyncio.to_thread(store.release, session_id, token)

        await asyncio.gather(save(stores[0], "first"), save(stores[1], "second"))

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("cryptography")

from agents import Agent, RunConfig, Runner
from agents.extensions.memory.async_sqlite_session import AsyncSQLiteSession
from agents.extensions.memory.encrypt_session import EncryptedSession
from agents.items import TResponseInputItem
from agents.memory import OpenAIResponsesCompactionSession, SQLiteSession
from agents.memory.session import SessionABC
from agents.run_config import CallModelData, ModelInputData
from agents.testing import ScriptedModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=[False, True], ids=["sqlite", "async-sqlite"])
async def backend(request: pytest.FixtureRequest, tmp_path: Path):
    factory = AsyncSQLiteSession if request.param else SQLiteSession
    store = factory("suffix", tmp_path / "history.db", session_settings={"limit": 20})
    await store.get_items()
    yield store
    if isinstance(store, AsyncSQLiteSession):
        await store.close()
    else:
        store.close()


@pytest.mark.parametrize("outer", [False, True])
@pytest.mark.parametrize("expired_count", [1, 40])
async def test_expired_prefix_does_not_block_full_live_window(
    backend: SQLiteSession | AsyncSQLiteSession,
    outer: bool,
    expired_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: clock[0])
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session: SessionABC
    if outer:
        session = EncryptedSession(
            "suffix",
            OpenAIResponsesCompactionSession("suffix", backend, client=client),
            encryption_key="synthetic-key",
            ttl=10,
        )
    else:
        session = OpenAIResponsesCompactionSession(
            "suffix",
            EncryptedSession("suffix", backend, encryption_key="synthetic-key", ttl=10),
            client=client,
        )
    await session.add_items([{"role": "user", "content": "expired"}] * expired_count)
    clock[0] += 11
    await session.add_items(
        [{"role": "assistant" if i % 2 else "user", "content": f"live {i}"} for i in range(20)]
    )
    model = ScriptedModel(steps=[[get_text_message("done")]])
    agent = Agent(name="worker", model=model)
    config = RunConfig()
    if isinstance(backend, AsyncSQLiteSession):
        result = Runner.run_streamed(agent, "continue", session=session, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, "continue", session=session, run_config=config)
    client.responses.compact.assert_awaited_once()
    assert "expired" not in str(client.responses.compact.call_args.kwargs)
    if expired_count > 1:
        # Auto mode must scope the request to the suffix, not the whole response chain.
        assert len(client.responses.compact.call_args.kwargs["input"]) == 22
    remaining = await backend.get_items(limit=100)
    assert remaining == []


async def test_suffix_transaction_rolls_back_failed_insert(
    backend: SQLiteSession | AsyncSQLiteSession,
) -> None:
    original: list[TResponseInputItem] = [{"role": "user", "content": str(i)} for i in range(4)]
    await backend.add_items(original)
    with sqlite3.connect(backend.db_path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_summary BEFORE INSERT ON agent_messages "
            "WHEN NEW.message_data LIKE '%compaction%' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END"
        )
    client = MagicMock()
    client.responses.compact = AsyncMock(
        return_value=SimpleNamespace(
            output=[{"type": "compaction", "id": "cmp", "encrypted_content": "synthetic-summary"}]
        )
    )
    session = OpenAIResponsesCompactionSession(
        "suffix",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    reply = get_text_message("done")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic insert failure"):
        await Runner.run(
            Agent(name="worker", model=ScriptedModel(steps=[[reply]])),
            "continue",
            session=session,
            run_config=RunConfig(session_settings={"limit": 1}),
        )
    assert await backend.get_items(limit=100) == original + [
        {"role": "user", "content": "continue"},
        reply.model_dump(exclude_unset=True),
    ]


async def test_new_backend_write_invalidates_snapshot(
    backend: SQLiteSession | AsyncSQLiteSession,
) -> None:
    original: list[TResponseInputItem] = [{"role": "user", "content": str(i)} for i in range(4)]
    await backend.add_items(original)
    newer: TResponseInputItem = {"role": "user", "content": "concurrent backend append"}

    async def compact(**kwargs: Any):
        await backend.add_items([newer])
        return SimpleNamespace(output=[])

    client = MagicMock()
    client.responses.compact = AsyncMock(side_effect=compact)
    session = OpenAIResponsesCompactionSession(
        "suffix",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    reply = get_text_message("done")
    await Runner.run(
        Agent(name="worker", model=ScriptedModel(steps=[[reply]])),
        "continue",
        session=session,
        run_config=RunConfig(session_settings={"limit": 1}),
    )
    assert await backend.get_items(limit=100) == original + [
        {"role": "user", "content": "continue"},
        reply.model_dump(exclude_unset=True),
        newer,
    ]


async def test_partial_previous_response_mode_preserves_history(
    backend: SQLiteSession | AsyncSQLiteSession,
) -> None:
    await backend.add_items([{"role": "user", "content": str(i)} for i in range(4)])
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession(
        "suffix",
        backend,
        client=client,
        compaction_mode="previous_response_id",
        should_trigger_compaction=lambda _: True,
    )
    await Runner.run(
        Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]])),
        "continue",
        session=session,
        run_config=RunConfig(session_settings={"limit": 1}),
    )
    client.responses.compact.assert_not_awaited()
    assert len(await backend.get_items(limit=100)) == 6


async def test_cancelled_suffix_transaction_settles_before_newer_append(
    backend: SQLiteSession | AsyncSQLiteSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    original: list[TResponseInputItem] = [{"role": "user", "content": str(i)} for i in range(4)]
    await backend.add_items(original)
    started = asyncio.Event()
    release = threading.Event()
    async_release = asyncio.Event()
    loop = asyncio.get_running_loop()
    if isinstance(backend, SQLiteSession):
        insert_sync = backend._insert_items

        def gated_insert(conn: Any, items: Any) -> None:
            if items and items[0].get("type") == "compaction":
                loop.call_soon_threadsafe(started.set)
                if not release.wait(10):
                    raise RuntimeError("test release timeout")
            insert_sync(conn, items)

        monkeypatch.setattr(backend, "_insert_items", gated_insert)
    else:
        insert_async = backend._insert_items

        async def gated_async_insert(conn: Any, items: Any) -> None:
            if items and items[0].get("type") == "compaction":
                started.set()
                await async_release.wait()
            await insert_async(conn, items)

        monkeypatch.setattr(backend, "_insert_items", gated_async_insert)
    summary: TResponseInputItem = {
        "type": "compaction",
        "id": "cmp",
        "encrypted_content": "synthetic",
    }
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "suffix",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )
    work = asyncio.create_task(
        Runner.run(
            Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]])),
            "continue",
            session=session,
            run_config=RunConfig(session_settings={"limit": 1}),
        )
    )
    newer: TResponseInputItem = {"role": "user", "content": "newer"}
    append = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        work.cancel()
        append = asyncio.create_task(session.add_items([newer]))
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not append.done()
        release.set()
        async_release.set()
        results = await asyncio.gather(work, append, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert await backend.get_items(limit=100) == original[:-1] + [summary, newer]
    finally:
        release.set()
        async_release.set()
        if not work.done():
            work.cancel()
        await asyncio.gather(
            work, *([append] if append is not None else []), return_exceptions=True
        )


@pytest.mark.parametrize("outer", [False, True])
async def test_failed_encrypted_suffix_transaction_preserves_original_tokens(
    backend: SQLiteSession | AsyncSQLiteSession,
    outer: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: clock[0])
    client = MagicMock()
    session: SessionABC
    if outer:
        session = EncryptedSession(
            "suffix",
            OpenAIResponsesCompactionSession(
                "suffix",
                backend,
                client=client,
                compaction_mode="input",
                should_trigger_compaction=lambda _: True,
            ),
            encryption_key="synthetic-key",
            ttl=10,
        )
    else:
        session = OpenAIResponsesCompactionSession(
            "suffix",
            EncryptedSession("suffix", backend, encryption_key="synthetic-key", ttl=10),
            client=client,
            compaction_mode="input",
            should_trigger_compaction=lambda _: True,
        )
    await session.add_items([{"role": "user", "content": "expired"}] * 4)
    clock[0] += 11
    await session.add_items([{"role": "user", "content": str(i)} for i in range(4)])
    saved: list[TResponseInputItem] = []

    async def compact(**kwargs: Any):
        saved.extend(await backend.get_items(limit=100))
        with sqlite3.connect(backend.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_all BEFORE INSERT ON agent_messages "
                "BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END"
            )
        return SimpleNamespace(
            output=[{"type": "compaction", "id": "cmp", "encrypted_content": "synthetic-summary"}]
        )

    client.responses.compact = AsyncMock(side_effect=compact)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic insert failure"):
        await Runner.run(
            Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]])),
            "continue",
            session=session,
            run_config=RunConfig(session_settings={"limit": 1}),
        )
    assert len(saved) == 10
    assert await backend.get_items(limit=100) == saved


@pytest.mark.parametrize("outer", [False, True])
async def test_repeated_ttl_bursts_do_not_accumulate_expired_rows(
    backend: SQLiteSession | AsyncSQLiteSession, outer: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: clock[0])
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session: SessionABC
    if outer:
        session = EncryptedSession(
            "suffix",
            OpenAIResponsesCompactionSession("suffix", backend, client=client),
            encryption_key="synthetic-key",
            ttl=10,
        )
    else:
        session = OpenAIResponsesCompactionSession(
            "suffix",
            EncryptedSession("suffix", backend, encryption_key="synthetic-key", ttl=10),
            client=client,
        )
    model = ScriptedModel(steps=[[get_text_message("done")] for _ in range(20)])
    agent = Agent(name="worker", model=model)
    for cycle in range(2):
        for _ in range(3):
            for _ in range(3):
                await Runner.run(agent, "below threshold", session=session)
            clock[0] += 11
        assert client.responses.compact.await_count == cycle
        await session.add_items(
            [{"role": "assistant" if i % 2 else "user", "content": f"live {i}"} for i in range(20)]
        )
        await Runner.run(agent, "cross threshold", session=session)
        assert client.responses.compact.await_count == cycle + 1
        assert await backend.get_items(limit=100) == []


@pytest.mark.parametrize("untrusted", [False, True], ids=["live", "wrong-key"])
async def test_prefix_cleanup_stops_before_retained_history(
    backend: SQLiteSession | AsyncSQLiteSession, untrusted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: clock[0])
    encrypted = EncryptedSession("suffix", backend, encryption_key="synthetic-key", ttl=10)
    await encrypted.add_items([{"role": "user", "content": "expired"}] * 40)
    retained: list[TResponseInputItem] = []
    if untrusted:
        other = EncryptedSession("suffix", backend, encryption_key="other-synthetic-key", ttl=10)
        await other.add_items([{"role": "user", "content": "unknown key"}])
        retained.extend(await backend.get_items(limit=1))
    clock[0] += 11
    await encrypted.add_items([{"role": "user", "content": "retained live"}] * 2)
    retained.extend(await backend.get_items(limit=2))
    await encrypted.add_items(
        [{"role": "assistant" if i % 2 else "user", "content": f"live {i}"} for i in range(20)]
    )
    # Fernet accepts a token exactly at its TTL boundary; cleanup must do so too.
    clock[0] += 10
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession("suffix", encrypted, client=client)
    await Runner.run(
        Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]])),
        "cross threshold",
        session=session,
    )
    client.responses.compact.assert_awaited_once()
    assert "retained live" not in str(client.responses.compact.call_args.kwargs)
    assert await backend.get_items(limit=100) == retained


async def test_partial_compaction_does_not_split_tool_call_and_output(
    backend: SQLiteSession | AsyncSQLiteSession,
) -> None:
    history: list[TResponseInputItem] = [
        {"type": "function_call", "call_id": "call", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call", "output": "synthetic"},
    ]
    await backend.add_items(history)
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession(
        "suffix",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )

    def rewrite_call(data: CallModelData[Any]) -> ModelInputData:
        items = list(data.model_data.input)
        items[0] = {
            "type": "function_call",
            "call_id": "call",
            "name": "lookup",
            "arguments": '{"query":"model-visible"}',
        }
        return ModelInputData(input=items, instructions=data.model_data.instructions)

    model = ScriptedModel(steps=[[get_text_message("done")]])
    await Runner.run(
        Agent(name="worker", model=model),
        "continue",
        session=session,
        run_config=RunConfig(call_model_input_filter=rewrite_call),
    )
    assert "model-visible" in str(model.calls[0].input)
    client.responses.compact.assert_awaited_once()
    assert client.responses.compact.call_args.kwargs["input"][0] == {
        "role": "user",
        "content": "continue",
    }
    assert await backend.get_items(limit=100) == history


@pytest.mark.parametrize("outer", [False, True])
@pytest.mark.parametrize("interspersed", [False, True], ids=["prefix", "interspersed"])
async def test_unverifiable_rows_inside_snapshot_are_retained(
    backend: SQLiteSession | AsyncSQLiteSession, outer: bool, interspersed: bool
) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session: SessionABC
    if outer:
        session = EncryptedSession(
            "suffix",
            OpenAIResponsesCompactionSession(
                "suffix", backend, client=client, should_trigger_compaction=lambda _: True
            ),
            encryption_key="synthetic-key",
        )
    else:
        session = OpenAIResponsesCompactionSession(
            "suffix",
            EncryptedSession("suffix", backend, encryption_key="synthetic-key"),
            client=client,
            should_trigger_compaction=lambda _: True,
        )
    if interspersed:
        await session.add_items([{"role": "user", "content": "earlier visible history"}])
    other = EncryptedSession("suffix", backend, encryption_key="other-synthetic-key")
    await other.add_items([{"role": "user", "content": "retained under another key"}])
    retained = await backend.get_items(limit=100)
    reply = get_text_message("done")
    await Runner.run(
        Agent(name="worker", model=ScriptedModel(steps=[[reply]])),
        "continue",
        session=session,
    )
    client.responses.compact.assert_awaited_once()
    assert await backend.get_items(limit=100) == retained
    assert client.responses.compact.call_args.kwargs["input"] == [
        {"role": "user", "content": "continue"},
        reply.model_dump(exclude_unset=True),
    ]


@pytest.mark.parametrize("follower", ["tool", "message"])
@pytest.mark.parametrize("filtered", [False, True], ids=["limited", "filtered"])
async def test_partial_compaction_preserves_reasoning_and_its_follower(
    backend: SQLiteSession | AsyncSQLiteSession, follower: str, filtered: bool
) -> None:
    history: list[TResponseInputItem] = [{"type": "reasoning", "id": "rs_stored", "summary": []}]
    if follower == "tool":
        history.extend(
            [
                {"type": "function_call", "call_id": "call", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call", "output": "synthetic"},
            ]
        )
    else:
        history.append(
            cast(
                TResponseInputItem, get_text_message("stored reply").model_dump(exclude_unset=True)
            )
        )
    await backend.add_items(history)
    summary: TResponseInputItem = {
        "type": "compaction",
        "id": "cmp",
        "encrypted_content": "synthetic",
    }
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "suffix",
        backend,
        client=client,
        compaction_mode="input",
        should_trigger_compaction=lambda _: True,
    )

    def omit_reasoning(data: CallModelData[Any]) -> ModelInputData:
        return ModelInputData(
            input=[item for item in data.model_data.input if item.get("type") != "reasoning"],
            instructions=data.model_data.instructions,
        )

    reply = get_text_message("done")
    model = ScriptedModel(steps=[[reply], [get_text_message("replayed")]])
    agent = Agent(name="worker", model=model)
    config = (
        RunConfig(call_model_input_filter=omit_reasoning)
        if filtered
        else RunConfig(session_settings={"limit": len(history) - 1})
    )
    await Runner.run(agent, "continue", session=session, run_config=config)
    assert all(item.get("type") != "reasoning" for item in model.calls[0].input)
    client.responses.compact.assert_awaited_once()
    assert client.responses.compact.call_args.kwargs["input"] == [
        {"role": "user", "content": "continue"},
        reply.model_dump(exclude_unset=True),
    ]
    assert await backend.get_items(limit=100) == history + [summary]

    session.should_trigger_compaction = lambda _: False
    await Runner.run(
        agent, "replay", session=session, run_config=RunConfig(session_settings={"limit": 100})
    )
    assert model.calls[1].input[: len(history)] == history

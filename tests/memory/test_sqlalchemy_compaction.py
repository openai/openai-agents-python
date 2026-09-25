from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy import event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Delete

from agents import Agent, Runner
from agents.extensions.memory.encrypt_session import EncryptedSession
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession
from agents.items import TResponseInputItem
from agents.memory import OpenAIResponsesCompactionSession
from agents.memory.session import SessionABC
from agents.testing import ScriptedModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["file", "memory"])
async def backend(tmp_path: Path, request: pytest.FixtureRequest):
    store = SQLAlchemySession.from_url(
        "bounded",
        url=(
            "sqlite+aiosqlite:///:memory:"
            if request.param == "memory"
            else f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"
        ),
        create_tables=True,
        session_settings={"limit": 20},
        sessions_table="custom_sessions",
        messages_table="custom_messages",
    )
    yield store
    await store.engine.dispose()


@pytest.mark.parametrize("streamed", [False, True])
async def test_limited_sqlalchemy_compacts_visible_suffix(backend, streamed: bool) -> None:
    hidden = {"role": "user", "content": "older hidden history"}
    await backend.add_items(
        [hidden]
        + [{"role": "assistant" if i % 2 else "user", "content": f"visible {i}"} for i in range(20)]
    )
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession("bounded", backend, client=client)
    for turn in range(5):
        agent = Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]]))
        if streamed:
            result = Runner.run_streamed(agent, f"turn {turn}", session=session)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, f"turn {turn}", session=session)
    client.responses.compact.assert_awaited_once()
    assert len(client.responses.compact.call_args.kwargs["input"]) == 22
    assert "older hidden history" not in str(client.responses.compact.call_args.kwargs)
    retained = await backend.get_items(limit=100)
    assert retained[0] == hidden
    assert len(retained) == 9


@pytest.mark.parametrize("operation", ["append", "pop", "clear"])
async def test_other_instance_mutation_invalidates_suffix(backend, operation: str) -> None:
    original: list[TResponseInputItem] = [
        {"role": "user", "content": f"item {i}"} for i in range(24)
    ]
    await backend.add_items(original)
    other = SQLAlchemySession(
        backend.session_id,
        engine=backend.engine,
        sessions_table="custom_sessions",
        messages_table="custom_messages",
    )
    newer: TResponseInputItem = {"role": "user", "content": "concurrent"}
    reply = get_text_message("done")
    expected = original + [
        {"role": "user", "content": "continue"},
        reply.model_dump(exclude_unset=True),
    ]

    async def compact(**kwargs):
        if operation == "append":
            await other.add_items([newer])
            expected.append(newer)
        elif operation == "pop":
            assert await other.pop_item() == expected.pop()
        else:
            await other.clear_session()
            expected.clear()
        return SimpleNamespace(output=[])

    client = MagicMock()
    client.responses.compact = AsyncMock(side_effect=compact)
    session = OpenAIResponsesCompactionSession(
        "bounded",
        backend,
        client=client,
        should_trigger_compaction=lambda _: True,
    )
    await Runner.run(
        Agent(name="worker", model=ScriptedModel(steps=[[reply]])), "continue", session=session
    )
    client.responses.compact.assert_awaited_once()
    assert await backend.get_items(limit=100) == expected


@pytest.mark.parametrize("outer", [False, True])
@pytest.mark.parametrize("reject_insert", [False, True])
async def test_encrypted_prefix_cleanup_is_atomic(
    backend, monkeypatch, outer, reject_insert
) -> None:
    now = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: now[0])
    client = MagicMock()
    summary = {"type": "compaction", "id": "cmp", "encrypted_content": "synthetic-summary"}
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session: SessionABC
    if outer:
        session = EncryptedSession(
            "bounded",
            OpenAIResponsesCompactionSession("bounded", backend, client=client),
            encryption_key="synthetic-key",
            ttl=10,
        )
    else:
        session = OpenAIResponsesCompactionSession(
            "bounded",
            EncryptedSession("bounded", backend, encryption_key="synthetic-key", ttl=10),
            client=client,
        )
    await session.add_items([{"role": "user", "content": "expired"}] * 40)
    now[0] += 11
    await session.add_items(
        [{"role": "assistant" if i % 2 else "user", "content": f"live {i}"} for i in range(20)]
    )
    stored_before = await backend.get_items(limit=100)
    compacting = False

    async def compact(**kwargs):
        nonlocal compacting
        compacting = True
        return SimpleNamespace(output=[summary])

    client.responses.compact.side_effect = compact

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if reject_insert and compacting and statement.startswith("INSERT INTO custom_messages"):
            raise RuntimeError("synthetic summary insertion failure")

    event.listen(backend.engine.sync_engine, "before_cursor_execute", fail_insert)
    agent = Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]]))
    if reject_insert:
        with pytest.raises(RuntimeError, match="synthetic summary insertion failure"):
            await Runner.run(agent, "continue", session=session)
        stored_after = await backend.get_items(limit=100)
        assert stored_after[:60] == stored_before
        assert len(stored_after) == 62
    else:
        await Runner.run(agent, "continue", session=session)
        assert len(await backend.get_items(limit=100)) == 1
        assert await session.get_items() == [summary]
    client.responses.compact.assert_awaited_once()
    assert "expired" not in str(client.responses.compact.call_args.kwargs)


async def test_replacement_uses_timestamp_order_not_id_order(backend) -> None:
    visible: list[TResponseInputItem] = [
        {"role": "user", "content": f"visible {i}"} for i in range(20)
    ]
    hidden: TResponseInputItem = {"role": "user", "content": "old timestamp, newer ID"}
    await backend.add_items(visible)
    await backend.add_items([hidden])
    async with backend.engine.begin() as conn:
        latest = await conn.execute(
            select(backend._messages.c.id).order_by(backend._messages.c.id.desc()).limit(1)
        )
        await conn.execute(
            update(backend._messages)
            .where(backend._messages.c.id == latest.scalar_one())
            .values(created_at=datetime(2000, 1, 1))
        )
    summary = {"type": "compaction", "id": "cmp", "encrypted_content": "synthetic-summary"}
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "bounded", backend, client=client, should_trigger_compaction=lambda _: True
    )
    await Runner.run(
        Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]])),
        "continue",
        session=session,
    )
    assert await backend.get_items(limit=100) == [hidden, summary]


async def test_cancelled_replacement_settles_before_other_instance_append(
    backend, monkeypatch
) -> None:
    await backend.add_items([{"role": "user", "content": f"old {i}"} for i in range(24)])
    deleted = asyncio.Event()
    release = asyncio.Event()

    class PausedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if isinstance(statement, Delete):
                deleted.set()
                await release.wait()
            return result

    # The DB transaction boundary is needed to control an otherwise invisible
    # delete/insert interleaving; public Runner and Session methods own all mutations.
    monkeypatch.setattr(
        backend,
        "_session_factory",
        async_sessionmaker(backend.engine, class_=PausedSession, expire_on_commit=False),
    )
    other = SQLAlchemySession(
        backend.session_id,
        engine=backend.engine,
        sessions_table="custom_sessions",
        messages_table="custom_messages",
    )
    client = MagicMock()
    summary = {"type": "compaction", "id": "cmp", "encrypted_content": "synthetic-summary"}
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "bounded", backend, client=client, should_trigger_compaction=lambda _: True
    )
    work = asyncio.create_task(
        Runner.run(
            Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]])),
            "continue",
            session=session,
        )
    )
    append = None
    newer: TResponseInputItem = {"role": "user", "content": "surviving append"}
    try:
        await asyncio.wait_for(deleted.wait(), 5)
        work.cancel()
        append = asyncio.create_task(other.add_items([newer]))
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done()
        assert not append.done()
        release.set()
        results = await asyncio.gather(work, append, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
    finally:
        release.set()
        await asyncio.gather(
            work, *([append] if append is not None else []), return_exceptions=True
        )
    stored = await backend.get_items(limit=100)
    assert stored == [{"role": "user", "content": f"old {i}"} for i in range(4)] + [summary, newer]


async def test_compaction_with_engine_managed_sqlite_begin(backend) -> None:
    # SQLAlchemy's documented SQLite transaction-control configuration emits BEGIN
    # itself, so mutation serialization must not attempt a nested BEGIN IMMEDIATE.
    @event.listens_for(backend.engine.sync_engine, "begin")
    def begin(conn):
        conn.exec_driver_sql("BEGIN")

    await test_limited_sqlalchemy_compacts_visible_suffix(backend, False)


async def test_encrypted_unknown_row_is_retained(backend, monkeypatch) -> None:
    now = [1000]
    monkeypatch.setattr("cryptography.fernet.time.time", lambda: now[0])
    encrypted = EncryptedSession("bounded", backend, encryption_key="synthetic-key", ttl=10)
    wrong_key = EncryptedSession("bounded", backend, encryption_key="other-synthetic-key", ttl=10)
    await encrypted.add_items([{"role": "user", "content": "expired"}] * 40)
    await wrong_key.add_items([{"role": "user", "content": "unverifiable"}])
    retained = (await backend.get_items(limit=100))[-1]
    now[0] += 11
    await encrypted.add_items(
        [{"role": "assistant" if i % 2 else "user", "content": f"live {i}"} for i in range(20)]
    )
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = OpenAIResponsesCompactionSession("bounded", encrypted, client=client)
    await Runner.run(
        Agent(name="worker", model=ScriptedModel(steps=[[get_text_message("done")]])),
        "continue",
        session=session,
    )
    client.responses.compact.assert_awaited_once()
    assert await backend.get_items(limit=100) == [retained]


async def test_failed_replacement_preserves_history_during_other_instance_access(
    backend, monkeypatch
) -> None:
    original: list[TResponseInputItem] = [
        {"role": "user", "content": f"old {i}"} for i in range(24)
    ]
    await backend.add_items(original)
    deleted = asyncio.Event()
    release = asyncio.Event()

    class FailedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if isinstance(statement, Delete):
                deleted.set()
                await release.wait()
                raise RuntimeError("synthetic replacement failure")
            return result

    monkeypatch.setattr(
        backend,
        "_session_factory",
        async_sessionmaker(backend.engine, class_=FailedSession, expire_on_commit=False),
    )
    other = SQLAlchemySession(
        backend.session_id,
        engine=backend.engine,
        sessions_table="custom_sessions",
        messages_table="custom_messages",
    )
    reply = get_text_message("done")
    client = MagicMock()
    client.responses.compact = AsyncMock(
        return_value=SimpleNamespace(
            output=[{"type": "compaction", "id": "cmp", "encrypted_content": "synthetic-summary"}]
        )
    )
    session = OpenAIResponsesCompactionSession(
        "bounded",
        backend,
        client=client,
        should_trigger_compaction=lambda _: True,
    )
    work = asyncio.create_task(
        Runner.run(
            Agent(name="worker", model=ScriptedModel(steps=[[reply]])),
            "continue",
            session=session,
        )
    )
    append = read = None
    newer: TResponseInputItem = {"role": "user", "content": "surviving append"}
    try:
        await asyncio.wait_for(deleted.wait(), 5)
        append = asyncio.create_task(other.add_items([newer]))
        read = asyncio.create_task(other.get_items(limit=100))
        # Give both operations an opportunity to reach the held transaction. A read
        # on a shared connection must not roll it back, nor may an append commit it.
        done, _ = await asyncio.wait({append}, timeout=0.1)
        assert not done
        release.set()
        outcomes = await asyncio.gather(work, append, read, return_exceptions=True)
        assert isinstance(outcomes[0], RuntimeError)
        assert outcomes[1] is None
        assert isinstance(outcomes[2], list)
        assert outcomes[2][:24] == original
    finally:
        release.set()
        await asyncio.gather(
            work,
            *[task for task in (append, read) if task is not None],
            return_exceptions=True,
        )
    assert await other.get_items(limit=100) == original + [
        {"role": "user", "content": "continue"},
        reply.model_dump(exclude_unset=True),
        newer,
    ]


async def test_parent_constraint_rejection_propagates(backend) -> None:
    await backend.get_items()
    async with backend.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TRIGGER reject_parent BEFORE INSERT ON custom_sessions "
                "BEGIN SELECT RAISE(ABORT, 'synthetic parent constraint'); END"
            )
        )
    with pytest.raises(IntegrityError, match="synthetic parent constraint"):
        await backend.add_items([{"role": "user", "content": "rejected"}])
    assert await backend.get_items() == []
    async with backend.engine.begin() as conn:
        await conn.execute(text("DROP TRIGGER reject_parent"))
    await backend.add_items([{"role": "user", "content": "accepted"}])
    assert await backend.get_items() == [{"role": "user", "content": "accepted"}]

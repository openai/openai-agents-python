from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("cryptography")  # Skip tests if cryptography is not installed

from cryptography.fernet import Fernet

from agents import (
    Agent,
    RunConfig,
    RunContextWrapper,
    Runner,
    RunState,
    SessionSettings,
    SQLiteSession,
    TResponseInputItem,
)
from agents.decorators import tool
from agents.extensions.memory.encrypt_session import EncryptedSession
from agents.memory import OpenAIResponsesCompactionSession
from agents.memory.openai_responses_compaction_session import OpenAIResponsesCompactionMode
from agents.testing import ModelStep, ScriptedModel
from tests.test_responses import get_function_tool_call, get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio


def _invalid_encrypted_envelope() -> TResponseInputItem:
    return cast(
        TResponseInputItem,
        {"__enc__": 1, "v": 1, "kid": "hkdf-v1", "payload": "not-a-valid-token"},
    )


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a scripted model."""
    return Agent(name="test", model=ScriptedModel())


@pytest.fixture
def encryption_key() -> str:
    """Fixture for a valid Fernet encryption key."""
    return str(Fernet.generate_key().decode("utf-8"))


@pytest.fixture
def set_fernet_time(monkeypatch):
    """Freeze Fernet TTL checks so expiration tests avoid real waiting."""
    current_time = 1_000

    def _set_time(value: int) -> None:
        nonlocal current_time
        current_time = value

    monkeypatch.setattr("cryptography.fernet.time.time", lambda: current_time)
    return _set_time


@pytest.fixture
def underlying_session():
    """Fixture for an underlying SQLite session."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_encrypt.db"
    return SQLiteSession("test_session", db_path)


async def test_encrypted_session_basic_functionality(
    agent: Agent, encryption_key: str, underlying_session: SQLiteSession
):
    """Test basic encryption/decryption functionality."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=600,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"

    encrypted_items = await underlying_session.get_items()
    assert encrypted_items[0].get("__enc__") == 1
    assert "payload" in encrypted_items[0]
    assert encrypted_items[0].get("content") != "Hello"

    underlying_session.close()


async def test_encrypted_session_with_runner(
    agent: Agent, encryption_key: str, underlying_session: SQLiteSession
):
    """Test that EncryptedSession works with Runner."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    assert isinstance(agent.model, ScriptedModel)
    agent.model.enqueue([get_text_message("San Francisco")])
    result1 = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    assert result1.final_output == "San Francisco"

    agent.model.enqueue([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"

    last_input = agent.model.calls[-1].input
    assert len(last_input) > 1
    assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)

    underlying_session.close()


async def _run_encrypted_session(
    agent: Agent[Any], value: str | RunState[Any], session: EncryptedSession, streamed: bool
):
    config = RunConfig(tracing_disabled=True)
    if not streamed:
        return await Runner.run(agent, value, session=session, run_config=config)
    result = Runner.run_streamed(agent, value, session=session, run_config=config)
    async for _ in result.stream_events():
        pass
    return result


def _decrypt_stored_items(
    session: EncryptedSession, stored: list[TResponseInputItem]
) -> list[dict[str, Any]]:
    # Inspect the storage envelope and decrypt directly, independently of Session.get_items.
    envelopes = cast(list[dict[str, Any]], stored)
    assert all(set(item) == {"__enc__", "v", "kid", "payload"} for item in envelopes)
    assert all(item["__enc__"] == 1 for item in envelopes)
    return [json.loads(session.cipher.decrypt(item["payload"].encode())) for item in envelopes]


@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_encrypts_items_around_compaction(
    streamed: bool, encryption_key: str, tmp_path: Path
) -> None:
    backend = SQLiteSession("encrypted-compaction", tmp_path / "history.db")
    session = EncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(
            backend.session_id, backend, should_trigger_compaction=lambda _: False
        ),
        encryption_key,
    )
    output = get_text_message("private assistant answer")
    model = ScriptedModel([[output], [get_text_message("followup answer")]])
    agent = Agent(name="test", model=model)
    expected = [
        {"role": "user", "content": "private user input"},
        output.model_dump(exclude_unset=True),
    ]
    try:
        result = await _run_encrypted_session(agent, "private user input", session, streamed)
        assert result.final_output == "private assistant answer"
        assert _decrypt_stored_items(session, await backend.get_items()) == expected

        await _run_encrypted_session(agent, "followup", session, streamed)
        assert model.calls[-1].input == expected + [{"role": "user", "content": "followup"}]
    finally:
        backend.close()


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("mode", ["input", "previous_response_id"])
async def test_runner_compacts_encrypted_history(
    streamed: bool,
    mode: OpenAIResponsesCompactionMode,
    encryption_key: str,
    tmp_path: Path,
    set_fernet_time: Any,
) -> None:
    backend = SQLiteSession("encrypted-active-compaction", tmp_path / "history.db")
    compacted = {"type": "compaction", "id": "cmp-1", "encrypted_content": "summary"}
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[compacted]))
    decisions: list[dict[str, Any]] = []

    def should_compact(context: dict[str, Any]) -> bool:
        decisions.append(context)
        return True

    session = EncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(
            backend.session_id,
            backend,
            client=client,
            compaction_mode=mode,
            should_trigger_compaction=should_compact,
        ),
        encryption_key,
        ttl=10,
    )
    output = get_text_message("private answer")
    next_output = get_text_message("next answer")
    model = ScriptedModel(
        [
            ModelStep(output=[output], response_id="resp-1"),
            ModelStep(output=[next_output], response_id="resp-2"),
        ]
    )
    agent = Agent(name="test", model=model)
    try:
        set_fernet_time(1_000)
        await session.add_items([{"role": "assistant", "content": "expired private history"}])
        set_fernet_time(1_020)
        await session.add_items([{"role": "user", "content": "retained history"}])
        await _run_encrypted_session(agent, "private input", session, streamed)

        expected = [
            {"role": "user", "content": "retained history"},
            {"role": "user", "content": "private input"},
            output.model_dump(exclude_unset=True),
        ]
        if mode == "input":
            client.responses.compact.assert_awaited_once_with(model="gpt-4.1", input=expected)
        else:
            client.responses.compact.assert_awaited_once_with(
                model="gpt-4.1", previous_response_id="resp-1"
            )
        assert decisions[0]["session_items"] == expected
        assert decisions[0]["compaction_candidate_items"] == [expected[-1]]
        assert _decrypt_stored_items(session, await backend.get_items()) == [compacted]
        assert await session.get_items() == [compacted]

        await _run_encrypted_session(agent, "next", session, streamed)
        assert model.calls[1].input == [compacted, {"role": "user", "content": "next"}]
        assert decisions[1]["session_items"] == [
            compacted,
            {"role": "user", "content": "next"},
            next_output.model_dump(exclude_unset=True),
        ]
        assert decisions[1]["compaction_candidate_items"] == [
            next_output.model_dump(exclude_unset=True)
        ]
        assert _decrypt_stored_items(session, await backend.get_items()) == [compacted]

        # Expiration changes the logical snapshot without changing the mutation generation.
        set_fernet_time(1_040)
        await session.run_compaction({"compaction_mode": "input", "force": True})
        client.responses.compact.assert_awaited_with(model="gpt-4.1", input=[])
    finally:
        backend.close()


@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_decrypts_existing_compaction_history_with_ttl_and_limit(
    streamed: bool, encryption_key: str, tmp_path: Path, set_fernet_time: Any
) -> None:
    backend = SQLiteSession("encrypted-history", tmp_path / "history.db")
    session = EncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(
            backend.session_id, backend, should_trigger_compaction=lambda _: False
        ),
        encryption_key,
        ttl=10,
    )
    expected_history = [{"role": "user", "content": "retained history"}]
    try:
        set_fernet_time(1_000)
        await session.add_items([{"role": "assistant", "content": "expired history"}])
        expired = (await backend.get_items())[0]
        set_fernet_time(1_020)
        await session.add_items(cast(list[TResponseInputItem], expected_history))
        # An expired tail forces EncryptedSession to expand its one-item retrieval window.
        await backend.add_items([expired])
        session.session_settings = SessionSettings(limit=1)
        model = ScriptedModel([[get_text_message("answer")]])

        await _run_encrypted_session(Agent(name="test", model=model), "next", session, streamed)

        assert model.calls[0].input == expected_history + [{"role": "user", "content": "next"}]
    finally:
        backend.close()


async def test_runner_defers_compaction_using_decrypted_candidates(
    encryption_key: str, tmp_path: Path
) -> None:
    backend = SQLiteSession("encrypted-deferred", tmp_path / "history.db")
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    decisions: list[dict[str, Any]] = []

    def should_compact(context: dict[str, Any]) -> bool:
        decisions.append(context)
        return context["response_id"] == "resp-tool" and any(
            item.get("type") == "function_call_output"
            for item in context["compaction_candidate_items"]
        )

    session = EncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(
            backend.session_id,
            backend,
            client=client,
            compaction_mode="input",
            should_trigger_compaction=should_compact,
        ),
        encryption_key,
    )

    @tool
    async def lookup() -> str:
        return "private lookup result"

    call = get_function_tool_call("lookup", "{}", call_id="lookup-1")
    output = get_text_message("private answer")
    model = ScriptedModel(
        [
            ModelStep(output=[call], response_id="resp-tool"),
            ModelStep(output=[output], response_id="resp-final"),
        ]
    )
    try:
        await _run_encrypted_session(
            Agent(name="test", model=model, tools=[lookup]), "private request", session, False
        )
        expected = [
            {"role": "user", "content": "private request"},
            call.model_dump(exclude_unset=True),
            {
                "type": "function_call_output",
                "call_id": "lookup-1",
                "output": "private lookup result",
            },
        ]
        assert any(context["compaction_candidate_items"] == expected[1:] for context in decisions)
        client.responses.compact.assert_awaited_once_with(
            model="gpt-4.1", input=expected + [output.model_dump(exclude_unset=True)]
        )
        assert await backend.get_items() == []
    finally:
        backend.close()


@pytest.mark.parametrize("cancel", [False, True], ids=["failure", "cancellation"])
async def test_encrypted_compaction_restores_original_tokens_before_concurrent_append(
    cancel: bool, encryption_key: str, tmp_path: Path, set_fernet_time: Any
) -> None:
    replacement_written = asyncio.Event()
    release_replacement = asyncio.Event()
    append_started = asyncio.Event()

    class PausingSQLiteSession(SQLiteSession):
        """Control a committed replacement failure at the real storage boundary."""

        fail_replacement = False

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            await super().add_items(items)
            if self.fail_replacement:
                self.fail_replacement = False
                replacement_written.set()
                await release_replacement.wait()
                raise RuntimeError("replacement acknowledgement lost")

    backend = PausingSQLiteSession("encrypted-rollback", tmp_path / "history.db")
    compacted = {"type": "compaction", "id": "cmp-1", "encrypted_content": "summary"}
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[compacted]))
    session = EncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(backend.session_id, backend, client=client),
        encryption_key,
        ttl=10,
    )
    tasks: list[asyncio.Task[Any]] = []
    valid = {"role": "user", "content": "valid history"}
    survivor = {"role": "user", "content": "concurrent survivor"}

    async def append() -> None:
        append_started.set()
        await session.add_items([cast(TResponseInputItem, survivor)])

    try:
        set_fernet_time(1_000)
        await session.add_items([{"role": "user", "content": "expired history"}])
        set_fernet_time(1_020)
        await session.add_items([cast(TResponseInputItem, valid)])
        original_tokens = await backend.get_items()
        backend.fail_replacement = True
        compaction = asyncio.create_task(session.run_compaction({"force": True}))
        tasks.append(compaction)
        await asyncio.wait_for(replacement_written.wait(), timeout=2)
        assert _decrypt_stored_items(session, await backend.get_items()) == [compacted]
        writer = asyncio.create_task(append())
        tasks.append(writer)
        await asyncio.wait_for(append_started.wait(), timeout=2)
        assert not writer.done()

        if cancel:
            compaction.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(compaction, timeout=2)
        else:
            release_replacement.set()
            with pytest.raises(RuntimeError, match="replacement acknowledgement lost"):
                await asyncio.wait_for(compaction, timeout=2)
        await asyncio.wait_for(writer, timeout=2)

        stored = await backend.get_items()
        assert stored[:-1] == original_tokens
        assert _decrypt_stored_items(session, stored[-1:]) == [survivor]
        assert await session.get_items() == [valid, survivor]
        await session.run_compaction({"force": True})
        client.responses.compact.assert_awaited_with(model="gpt-4.1", input=[valid, survivor])
        assert _decrypt_stored_items(session, await backend.get_items()) == [compacted]
    finally:
        release_replacement.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        backend.close()


async def test_clear_revokes_encrypted_deferred_compaction(
    encryption_key: str, tmp_path: Path
) -> None:
    snapshot_read, release_snapshot, model_waiting, release_model = (
        asyncio.Event() for _ in range(4)
    )
    clear_started, backend_clear_started = asyncio.Event(), asyncio.Event()

    class ObservedSQLiteSession(SQLiteSession):
        async def clear_session(self) -> None:
            backend_clear_started.set()
            await super().clear_session()

    class PausingEncryptedSession(EncryptedSession):
        """Suspend a logical tool-output snapshot before its policy decision."""

        paused = False

        async def get_items(self, limit=None):
            # Preserve the public Session call shape without opting into run context.
            items = await super().get_items(limit)
            if not self.paused and any(
                item.get("type") == "function_call_output" for item in items
            ):
                self.paused = True
                snapshot_read.set()
                await release_snapshot.wait()
            return items

    backend = ObservedSQLiteSession("encrypted-deferred-clear", tmp_path / "history.db")
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = PausingEncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(
            backend.session_id,
            backend,
            client=client,
            compaction_mode="input",
            should_trigger_compaction=lambda context: context["response_id"] == "resp-tool",
        ),
        encryption_key,
    )

    @tool
    async def lookup() -> str:
        return "private tool output"

    async def held_model(_) -> ModelStep:
        model_waiting.set()
        await release_model.wait()
        return ModelStep(output=[get_text_message("answer-A")], response_id="resp-a")

    async def clear() -> None:
        clear_started.set()
        await session.clear_session()

    agent_a = Agent(
        name="A",
        tools=[lookup],
        model=ScriptedModel(
            [
                ModelStep(
                    output=[get_function_tool_call("lookup", "{}", call_id="lookup-a")],
                    response_id="resp-tool",
                ),
                ModelStep.respond(held_model),
            ]
        ),
    )
    task_a = asyncio.create_task(_run_encrypted_session(agent_a, "input-A", session, False))
    tasks: list[asyncio.Task[Any]] = [task_a]
    try:
        await asyncio.wait_for(snapshot_read.wait(), timeout=2)
        clearing = asyncio.create_task(clear())
        tasks.append(clearing)
        await asyncio.wait_for(clear_started.wait(), timeout=2)
        clear_entered_before_snapshot_settled = backend_clear_started.is_set()
        if clear_entered_before_snapshot_settled:
            # Force the stale-publication interleaving if clear can pass the snapshot.
            await asyncio.wait_for(clearing, timeout=2)
        release_snapshot.set()
        await asyncio.wait_for(model_waiting.wait(), timeout=2)
        await asyncio.wait_for(clearing, timeout=2)

        output_b = get_text_message("answer-B")
        agent_b = Agent(
            name="B", model=ScriptedModel([ModelStep(output=[output_b], response_id="resp-b")])
        )
        await _run_encrypted_session(agent_b, "input-B", session, False)

        client.responses.compact.assert_not_awaited()
        assert not clear_entered_before_snapshot_settled
        assert _decrypt_stored_items(session, await backend.get_items()) == [
            {"role": "user", "content": "input-B"},
            output_b.model_dump(exclude_unset=True),
        ]
    finally:
        release_snapshot.set()
        release_model.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        backend.close()


async def test_encrypted_session_preserves_optional_compaction_delegation(
    encryption_key: str, tmp_path: Path
) -> None:
    calls: list[Any] = []

    class CustomCompactionSession(SQLiteSession):
        async def run_compaction(self, args=None) -> None:
            calls.append(args)

    backend = CustomCompactionSession("encrypted-custom-compaction", tmp_path / "history.db")
    session = EncryptedSession(backend.session_id, backend, encryption_key)

    @tool
    async def lookup() -> str:
        return "private lookup result"

    model = ScriptedModel(
        [
            ModelStep(
                output=[get_function_tool_call("lookup", "{}", call_id="lookup-custom")],
                response_id="resp-tool",
            ),
            ModelStep(output=[get_text_message("private answer")], response_id="resp-final"),
        ]
    )
    try:
        result = await _run_encrypted_session(
            Agent(name="test", model=model, tools=[lookup]), "private input", session, False
        )
        assert result.final_output == "private answer"
        assert not hasattr(session, "_defer_compaction")
        assert [args["response_id"] for args in calls] == ["resp-final"]
        assert len(_decrypt_stored_items(session, await backend.get_items())) == 4
    finally:
        backend.close()


async def test_compaction_wrapping_encrypted_storage(encryption_key: str, tmp_path: Path) -> None:
    backend = SQLiteSession("inner-encryption", tmp_path / "history.db")
    encrypted = EncryptedSession(backend.session_id, backend, encryption_key)
    compacted = {"type": "compaction", "id": "cmp-1", "encrypted_content": "summary"}
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[compacted]))
    session = OpenAIResponsesCompactionSession(backend.session_id, encrypted, client=client)
    try:
        await session.add_items([{"role": "user", "content": "private history"}])
        await session.run_compaction({"force": True})
        client.responses.compact.assert_awaited_once_with(
            model="gpt-4.1", input=[{"role": "user", "content": "private history"}]
        )
        assert _decrypt_stored_items(encrypted, await backend.get_items()) == [compacted]
    finally:
        backend.close()


@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_recovers_encrypted_resumed_append(
    streamed: bool, encryption_key: str, tmp_path: Path
) -> None:
    class LostAckSQLiteSession(SQLiteSession):
        """Fail after a real committed batch to exercise public Runner recovery."""

        fail_after_commit = False

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            await super().add_items(items)
            if self.fail_after_commit:
                self.fail_after_commit = False
                raise RuntimeError("append acknowledgement lost")

    backend = LostAckSQLiteSession("encrypted-resume", tmp_path / "history.db")
    session = EncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(
            backend.session_id, backend, should_trigger_compaction=lambda _: False
        ),
        encryption_key,
    )
    effects: list[str] = []

    @tool(needs_approval=True)
    async def record() -> str:
        effects.append("recorded")
        return "private tool receipt"

    call = get_function_tool_call("record", "{}", call_id="record-1")
    output = get_text_message("private final answer")
    model = ScriptedModel([[call], [output]])
    agent = Agent(name="test", model=model, tools=[record])
    try:
        paused = await _run_encrypted_session(agent, "private request", session, streamed)
        state = paused.to_state()
        state.approve(state.get_interruptions()[0])
        backend.fail_after_commit = True
        with pytest.raises(RuntimeError, match="append acknowledgement lost"):
            await _run_encrypted_session(agent, state, session, streamed)
        assert effects == ["recorded"]
        assert len(model.calls) == 1
        state = await RunState.from_json(agent, state.to_json())

        result = await _run_encrypted_session(agent, state, session, streamed)

        assert result.final_output == "private final answer"
        assert effects == ["recorded"]
        assert "pending_session_write" not in result.to_state().to_json()
        assert _decrypt_stored_items(session, await backend.get_items()) == [
            {"role": "user", "content": "private request"},
            call.model_dump(exclude_unset=True),
            {
                "type": "function_call_output",
                "call_id": "record-1",
                "output": "private tool receipt",
            },
            output.model_dump(exclude_unset=True),
        ]
    finally:
        backend.close()


@pytest.mark.parametrize(
    "pause_before_append", [False, True], ids=["after-append", "before-append"]
)
async def test_encrypted_runner_skips_compaction_after_interleaved_outer_write(
    pause_before_append: bool, encryption_key: str, tmp_path: Path
) -> None:
    paused = asyncio.Event()
    release = asyncio.Event()

    class PausingEncryptedSession(EncryptedSession):
        """Suspend at the outer public append boundary while another Runner completes."""

        async def add_items(
            self,
            items: list[TResponseInputItem],
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> None:
            is_run_a_output = "answer-A" in json.dumps(items)
            if is_run_a_output and pause_before_append:
                paused.set()
                await release.wait()
            await super().add_items(items, wrapper=wrapper)
            if is_run_a_output and not pause_before_append:
                paused.set()
                await release.wait()

    backend = SQLiteSession("encrypted-concurrent", tmp_path / "history.db")
    client = MagicMock()
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[]))
    session = PausingEncryptedSession(
        backend.session_id,
        OpenAIResponsesCompactionSession(
            backend.session_id,
            backend,
            client=client,
            should_trigger_compaction=lambda context: context["response_id"] == "resp-a",
        ),
        encryption_key,
    )
    output_a = get_text_message("answer-A")
    output_b = get_text_message("answer-B")
    agent_a = Agent(
        name="A", model=ScriptedModel([ModelStep(output=[output_a], response_id="resp-a")])
    )
    agent_b = Agent(
        name="B", model=ScriptedModel([ModelStep(output=[output_b], response_id="resp-b")])
    )
    run_a = asyncio.create_task(_run_encrypted_session(agent_a, "input-A", session, False))
    try:
        await asyncio.wait_for(paused.wait(), timeout=2)
        result_b = await asyncio.wait_for(
            _run_encrypted_session(agent_b, "input-B", session, False), timeout=2
        )
        assert result_b.final_output == "answer-B"
        release.set()
        result_a = await asyncio.wait_for(run_a, timeout=2)
        assert result_a.final_output == "answer-A"
        client.responses.compact.assert_not_awaited()
        expected = [
            {"role": "user", "content": "input-A"},
            {"role": "user", "content": "input-B"},
            output_b.model_dump(exclude_unset=True),
        ]
        expected.insert(3 if pause_before_append else 1, output_a.model_dump(exclude_unset=True))
        assert _decrypt_stored_items(session, await backend.get_items()) == expected
    finally:
        release.set()
        if not run_a.done():
            run_a.cancel()
        await asyncio.gather(run_a, return_exceptions=True)
        backend.close()


async def test_encrypted_session_pop_item(encryption_key: str, underlying_session: SQLiteSession):
    """Test pop_item functionality."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Second"},
    ]
    await session.add_items(items)

    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "Second"

    remaining = await session.get_items()
    assert len(remaining) == 1
    assert remaining[0].get("content") == "First"

    underlying_session.close()


async def test_encrypted_session_clear(encryption_key: str, underlying_session: SQLiteSession):
    """Test clear_session functionality."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    await session.add_items([{"role": "user", "content": "Test"}])
    await session.clear_session()

    items = await session.get_items()
    assert len(items) == 0

    underlying_session.close()


async def test_encrypted_session_forwards_wrapper_to_all_underlying_operations(
    encryption_key: str,
):
    class ContextAwareUnderlying:
        def __init__(self) -> None:
            self.session_id = "test_session"
            self.session_settings = None
            self.items: list[TResponseInputItem] = []
            self.wrappers: list[RunContextWrapper[Any] | None] = []

        async def get_items(
            self,
            limit: int | None = None,
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> list[TResponseInputItem]:
            self.wrappers.append(wrapper)
            return list(self.items if limit is None else self.items[-limit:])

        async def add_items(
            self,
            items: list[TResponseInputItem],
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> None:
            self.wrappers.append(wrapper)
            self.items.extend(items)

        async def pop_item(
            self,
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> TResponseInputItem | None:
            self.wrappers.append(wrapper)
            return self.items.pop() if self.items else None

        async def clear_session(
            self,
            *,
            wrapper: RunContextWrapper[Any] | None = None,
        ) -> None:
            self.wrappers.append(wrapper)
            self.items.clear()

    underlying = ContextAwareUnderlying()
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=cast(Any, underlying),
        encryption_key=encryption_key,
    )
    wrapper = RunContextWrapper(context={"tenant": "a"})

    await session.add_items([{"role": "user", "content": "hello"}], wrapper=wrapper)
    assert await session.get_items(wrapper=wrapper) == [{"role": "user", "content": "hello"}]
    assert await session.pop_item(wrapper=wrapper) == {"role": "user", "content": "hello"}
    await session.clear_session(wrapper=wrapper)

    assert underlying.wrappers == [wrapper, wrapper, wrapper, wrapper]


async def test_encrypted_session_ttl_expiration(
    encryption_key: str, underlying_session: SQLiteSession, set_fernet_time
):
    """Test TTL expiration - expired items are silently skipped."""
    set_fernet_time(1_000)
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=1,  # 1 second TTL
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]
    await session.add_items(items)

    set_fernet_time(1_002)

    retrieved = await session.get_items()
    assert len(retrieved) == 0

    underlying_items = await underlying_session.get_items()
    assert len(underlying_items) == 2

    underlying_session.close()


async def test_encrypted_session_pop_expired(
    encryption_key: str, underlying_session: SQLiteSession, set_fernet_time
):
    """Test pop_item with expired data."""
    set_fernet_time(1_000)
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=1,
    )

    await session.add_items([{"role": "user", "content": "Test"}])
    set_fernet_time(1_002)

    popped = await session.pop_item()
    assert popped is None

    underlying_session.close()


async def test_encrypted_session_pop_mixed_expired_valid(
    encryption_key: str, underlying_session: SQLiteSession, set_fernet_time
):
    """Test pop_item auto-retry with mixed expired and valid items."""
    set_fernet_time(1_000)
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=2,  # 2 second TTL
    )

    await session.add_items(
        [
            {"role": "user", "content": "Old message 1"},
            {"role": "assistant", "content": "Old response 1"},
        ]
    )

    set_fernet_time(1_003)

    await session.add_items(
        [
            {"role": "user", "content": "New message"},
            {"role": "assistant", "content": "New response"},
        ]
    )

    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "New response"

    popped2 = await session.pop_item()
    assert popped2 is not None
    assert popped2.get("content") == "New message"

    popped3 = await session.pop_item()
    assert popped3 is None

    underlying_session.close()


async def test_encrypted_session_raw_string_key(underlying_session: SQLiteSession):
    """Test using raw string as encryption key (not base64)."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key="my-secret-password",  # Raw string, not Fernet key
    )

    await session.add_items([{"role": "user", "content": "Test"}])
    items = await session.get_items()
    assert len(items) == 1
    assert items[0].get("content") == "Test"

    underlying_session.close()


async def test_encrypted_session_get_items_limit(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test get_items with limit parameter."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": f"Message {i}"} for i in range(5)
    ]
    await session.add_items(items)

    limited = await session.get_items(limit=2)
    assert len(limited) == 2
    assert limited[0].get("content") == "Message 3"  # Latest 2
    assert limited[1].get("content") == "Message 4"

    underlying_session.close()


async def test_encrypted_session_get_items_limit_skips_invalid_latest_envelope(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test that limit counts valid decrypted items, not encrypted envelopes."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    await session.add_items([{"role": "user", "content": "older valid"}])
    await underlying_session.add_items([_invalid_encrypted_envelope()])

    all_items = await session.get_items()
    assert [item.get("content") for item in all_items] == ["older valid"]

    limited = await session.get_items(limit=1)
    assert [item.get("content") for item in limited] == ["older valid"]

    underlying_session.close()


async def test_encrypted_session_get_items_limit_returns_latest_valid_items_after_invalids(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test that invalid envelopes do not hide earlier valid items from limit."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    await session.add_items(
        [
            {"role": "user", "content": "valid 0"},
            {"role": "assistant", "content": "valid 1"},
        ]
    )
    await underlying_session.add_items([_invalid_encrypted_envelope()])
    await session.add_items([{"role": "user", "content": "valid 2"}])

    limited = await session.get_items(limit=2)
    assert [item.get("content") for item in limited] == ["valid 1", "valid 2"]

    underlying_session.close()


async def test_encrypted_session_get_items_session_settings_limit_skips_invalid_envelopes(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test that session settings limit counts valid decrypted items."""
    underlying_session.session_settings = SessionSettings(limit=3)
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    await session.add_items(
        [
            {"role": "user", "content": "valid 0"},
            {"role": "assistant", "content": "valid 1"},
            {"role": "user", "content": "valid 2"},
        ]
    )
    await underlying_session.add_items([_invalid_encrypted_envelope()])

    items = await session.get_items()
    assert [item.get("content") for item in items] == ["valid 0", "valid 1", "valid 2"]

    underlying_session.close()


async def test_encrypted_session_unicode_content(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test encryption of international text content."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello world"},
        {"role": "assistant", "content": "Special chars: áéíóú"},
        {"role": "user", "content": "Numbers and symbols: 123!@#"},
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert retrieved[0].get("content") == "Hello world"
    assert retrieved[1].get("content") == "Special chars: áéíóú"
    assert retrieved[2].get("content") == "Numbers and symbols: 123!@#"

    underlying_session.close()


class CustomSession(SQLiteSession):
    """Mock custom session with additional methods for testing delegation."""

    def get_stats(self) -> dict[str, int]:
        """Custom method that should be accessible through delegation."""
        return {"custom_method_calls": 42, "test_value": 123}

    async def custom_async_method(self) -> str:
        """Custom async method for testing delegation."""
        return "custom_async_result"


async def test_encrypted_session_delegation():
    """Test that custom methods on underlying session are accessible through delegation."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_delegation.db"
    underlying_session = CustomSession("test_session", db_path)

    encryption_key = str(Fernet.generate_key().decode("utf-8"))
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    stats = session.get_stats()
    assert stats == {"custom_method_calls": 42, "test_value": 123}

    result = await session.custom_async_method()
    assert result == "custom_async_result"

    await session.add_items([{"role": "user", "content": "Test delegation"}])
    items = await session.get_items()
    assert len(items) == 1
    assert items[0].get("content") == "Test delegation"

    underlying_session.close()


# ============================================================================
# SessionSettings Tests
# ============================================================================


async def test_session_settings_delegated_to_underlying(encryption_key: str):
    """Test that session_settings is correctly delegated to underlying session."""
    from agents.memory import SessionSettings

    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_settings.db"
    underlying = SQLiteSession("test_session", db_path, session_settings=SessionSettings(limit=5))

    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying,
        encryption_key=encryption_key,
    )

    # session_settings should be accessible through EncryptedSession
    assert session.session_settings is not None
    assert session.session_settings.limit == 5

    underlying.close()


async def test_session_settings_get_items_uses_underlying_limit(encryption_key: str):
    """Test that get_items uses underlying session's session_settings.limit."""
    from agents.memory import SessionSettings

    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_settings_limit.db"
    underlying = SQLiteSession("test_session", db_path, session_settings=SessionSettings(limit=3))

    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying,
        encryption_key=encryption_key,
    )

    # Add 5 items
    items: list[TResponseInputItem] = [
        {"role": "user", "content": f"Message {i}"} for i in range(5)
    ]
    await session.add_items(items)

    # get_items() with no limit should use underlying session_settings.limit=3
    retrieved = await session.get_items()
    assert len(retrieved) == 3
    # Should get the last 3 items
    assert retrieved[0].get("content") == "Message 2"
    assert retrieved[1].get("content") == "Message 3"
    assert retrieved[2].get("content") == "Message 4"

    underlying.close()


async def test_session_settings_explicit_limit_overrides_settings(encryption_key: str):
    """Test that explicit limit parameter overrides session_settings."""
    from agents.memory import SessionSettings

    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_override.db"
    underlying = SQLiteSession("test_session", db_path, session_settings=SessionSettings(limit=5))

    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying,
        encryption_key=encryption_key,
    )

    # Add 10 items
    items: list[TResponseInputItem] = [
        {"role": "user", "content": f"Message {i}"} for i in range(10)
    ]
    await session.add_items(items)

    # Explicit limit=2 should override session_settings.limit=5
    retrieved = await session.get_items(limit=2)
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Message 8"
    assert retrieved[1].get("content") == "Message 9"

    underlying.close()


async def test_session_settings_resolve():
    """Test SessionSettings.resolve() method."""
    from agents.memory import SessionSettings

    base = SessionSettings(limit=100)
    override = SessionSettings(limit=50)

    final = base.resolve(override)

    assert final.limit == 50  # Override wins
    assert base.limit == 100  # Original unchanged

    # Resolving with None returns self
    final_none = base.resolve(None)
    assert final_none.limit == 100


async def test_runner_with_session_settings_override(encryption_key: str):
    """Test that RunConfig can override session's default settings."""
    from agents import Agent, RunConfig, Runner
    from agents.memory import SessionSettings
    from agents.testing import ScriptedModel
    from tests.test_responses import get_text_message

    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_runner_override.db"
    underlying = SQLiteSession("test_session", db_path, session_settings=SessionSettings(limit=100))

    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying,
        encryption_key=encryption_key,
    )

    # Add some history
    items: list[TResponseInputItem] = [{"role": "user", "content": f"Turn {i}"} for i in range(10)]
    await session.add_items(items)

    model = ScriptedModel()
    agent = Agent(name="test", model=model)
    model.enqueue([get_text_message("Got it")])

    await Runner.run(
        agent,
        "New question",
        session=session,
        run_config=RunConfig(
            session_settings=SessionSettings(limit=2)  # Override to 2
        ),
    )

    # Verify the agent received only the last 2 history items + new question
    last_input = model.calls[-1].input
    # Filter out the new "New question" input
    history_items = [item for item in last_input if item.get("content") != "New question"]
    # Should have 2 history items (last two from the 10 we added)
    assert len(history_items) == 2

    underlying.close()

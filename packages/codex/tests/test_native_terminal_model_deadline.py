"""Real native terminal-result/deadline orderings on the composed SDK."""

from __future__ import annotations

import asyncio
import json

from agents import Agent, CustomStorage, SQLiteSessionStore, function_tool
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import function_call
from tests.test_native_model_invocation_deadline import (
    MODEL_MS,
    AdmissionObserver,
    Gate,
    deadline_error,
    deadline_peer as deadline_peer,
    provider_for,
)


def terminal_rows(state):
    return [
        row["payload"]["tool_completion"]
        for row in state.history
        if row.get("type") == "event_msg" and row.get("payload", {}).get("tool_completion")
    ]


class RecordingTerminalStore(SQLiteSessionStore):
    """Record real committed/flushed data and the backend read used on reopen."""

    committed = None
    flushed = None
    reopened = None

    def commit(self, thread_id, token, expected_revision, write_id, payload):
        super().commit(thread_id, token, expected_revision, write_id, payload)
        self.committed = json.loads(payload)

    def flush(self, thread_id, token):
        super().flush(thread_id, token)
        self.flushed = json.loads(super().read(thread_id, token))

    def read(self, thread_id, token):
        payload = super().read(thread_id, token)
        if payload is not None:
            self.reopened = json.loads(payload)
        return payload


def stored_terminal_rows(document):
    assert document is not None
    return [
        row["payload"]["tool_completion"]
        for row in document["items"]
        if row.get("type") == "event_msg" and row.get("payload", {}).get("tool_completion")
    ]


async def test_durable_terminal_success_survives_the_late_model_clock(deadline_peer, tmp_path):
    """Model-final disarm precedes terminal policy; durable terminal data stays final."""
    effects = []

    @function_tool(finish_turn=True)
    def finish() -> str:
        effects.append("once")
        return "terminal success"

    deadline_peer.respond(function_call("finish", {}, "terminal-first"))
    store = RecordingTerminalStore(tmp_path / "accepted.sqlite")
    async with timeout(30), provider_for(deadline_peer) as provider:
        agent = Agent(
            model="gpt-5.1-codex",
            instructions="Finish.",
            provider=provider,
            tools=[finish],
            model_settings={"invocation_timeout_ms": MODEL_MS},
        )
        async with agent.session(storage=CustomStorage(store)) as session:
            turn = await session.submit("one")
            result = await turn.get_result()
            assert result.final_output == "terminal success"
            assert result.tool_call is not None
            identity = session.id
            before = stored_terminal_rows(store.flushed)
            assert len(before) == 1
            assert before[0]["final_output"] == "terminal success"
            # This sleep is AFTER the actual public/durable acceptance evidence;
            # it does not guess which competing operation won.
            await asyncio.sleep(MODEL_MS / 1000 + 0.3)
            assert stored_terminal_rows(store.committed) == before
            assert stored_terminal_rows(store.flushed) == before
            assert (await turn.get_result()).final_output == "terminal success"
        # Persistent storage reopens by identity after the original lease retires.
        # It deliberately does not produce a portable SessionState.
        store.reopened = None
        async with agent.session(storage=CustomStorage(store), resume=identity) as restored:
            assert restored.id == identity
            assert stored_terminal_rows(store.reopened) == before
    assert effects == ["once"]
    assert len(deadline_peer.requests) == 1


async def test_reserved_expiry_cannot_be_revived_by_a_late_valid_terminal_result(
    deadline_peer, monkeypatch
):
    body, terminal = Gate(), Gate()
    effects = []

    @function_tool(finish_turn=True)
    async def finish() -> str:
        effects.append("once")
        body.entered.set()
        # A host callback may ignore cancellation. Its physical return still has
        # to retire, but its late valid value cannot regain publication authority.
        while not body.release.is_set():
            try:
                await body.release.wait()
            except asyncio.CancelledError:
                pass
        return "must not complete"

    deadline_peer.terminal_gate = terminal
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(function_call("finish", {}, "expiry-first"))
    async with (
        timeout(30),
        provider_for(deadline_peer) as provider,
        Agent(
            model="gpt-5.1-codex",
            instructions="Finish.",
            provider=provider,
            tools=[finish],
            model_settings={"invocation_timeout_ms": MODEL_MS},
        ).session() as session,
    ):
        observer = AdmissionObserver(session._native)
        monkeypatch.setattr(session, "_native", observer)
        turn = await session.submit("one")
        result = asyncio.create_task(turn.get_result())

        async def collect():
            return [event async for event in turn.events()]

        observations = asyncio.create_task(collect())
        try:
            await body.entered.wait()
            assert observer.requests
            for capability in list(observer.requests):
                await observer.expired(capability)
            assert not result.done(), "Physical callback retirement is still held"
        finally:
            body.release.set()
            terminal.release.set()
        await deadline_error(result)
        assert not any(
            isinstance(event, TurnFinished) and event.status == "completed"
            for event in await observations
        )
        assert terminal_rows(await session.save_state()) == []
    assert effects == ["once"]
    assert len(deadline_peer.requests) == 1

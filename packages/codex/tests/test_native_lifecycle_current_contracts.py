"""Keep current seeded-opening, fact privacy and deadlines on the shared atomic lifecycle path."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from agents import Agent, NativeError, NativeProvider, Session
from agents.observation import EventGap, ResponseFactReceived, TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_buffered_responses import json_peer, json_response
from tests.test_native_lifecycle_run import atomic_opening_placement, capture_lifecycle_sessions
from tests.test_native_model_invocation_deadline import (
    MODEL_MS,
    Gate,
    deadline_error,
    deadline_peer as deadline_peer,
    provider_for,
    wait_for_phase,
)
from tests.test_native_plain_event_collection import instrument_reader
from tests.test_native_response_facts import PRIVATE, fact_peer


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [
            {"role": "assistant", "content": "prior assistant"},
            {"role": "user", "content": "prior user"},
        ],
    ],
)
async def test_initial_messages_disable_atomic_opening_before_native_effects(monkeypatch, messages):
    readers = instrument_reader(monkeypatch)
    sessions, lifecycle_openings = capture_lifecycle_sessions(monkeypatch)
    original_open = Session._open.__func__
    openings = []

    async def observe_open(cls, *args, **kwargs):
        openings.append(kwargs.copy())
        return await original_open(cls, *args, **kwargs)

    monkeypatch.setattr(Session, "_open", classmethod(observe_open))
    async with timeout(30), json_peer() as peer:
        peer.json_replies.append(json_response("done", "seeded-answer", "seeded-item"))
        async with NativeProvider(
            base_url=peer.url, api_key="synthetic-owned-seeds", response_mode="buffered"
        ) as provider:
            result = await Agent(
                instructions="Continue the conversation.", model="gpt-5.2", provider=provider
            ).run("fresh input", initial_messages=messages)
            assert result.final_output == "done"
            assert len(peer.requests) == 1
            request = peer.requests[0]
            serialized = json.dumps(request["input"])
            assert "fresh input" in serialized
            if messages:
                assert serialized.index("prior assistant") < serialized.index("prior user")
                assert serialized.index("prior user") < serialized.index("fresh input")
    assert len(sessions) == len(readers) == 1
    assert lifecycle_openings[0]["shared"] is False
    assert len(openings) == 1 and openings[0]["initial_messages_json"] is not None
    assert openings[0].get("_atomic_first_turn") is not True


@pytest.mark.parametrize("mode", ["streaming", "buffered"])
@pytest.mark.parametrize("refused", [False, True])
async def test_lifecycle_response_facts_use_current_typed_privacy_projection(
    monkeypatch, mode, refused
):
    readers = instrument_reader(monkeypatch)
    sessions, lifecycle_openings = capture_lifecycle_sessions(monkeypatch)
    async with timeout(30), fact_peer() as peer:
        peer.replies.append((refused, False))
        async with NativeProvider(
            base_url=peer.url,
            api_key="synthetic-owned-current-facts",
            response_mode=mode,
            supports_websockets=False,
        ) as provider:
            agent = Agent(
                instructions="Answer.",
                model="gpt-5.2",
                provider=provider,
                model_settings={} if refused else {"top_logprobs": 0},
            )
            if refused:
                with pytest.raises(NativeError) as caught:
                    await agent.run("answer")
                assert caught.value.response_integrity_reason == "provider_refusal"
                assert PRIVATE not in str(caught.value)
                turn = sessions[0]._last_turn
            else:
                result = await agent.run("answer")
                assert result.final_output == "aa"
                turn = result.turn
                assert peer.requests[0]["top_logprobs"] == 0
                assert "message.output_text.logprobs" in peer.requests[0]["include"]
            assert turn is not None
            ordinary = [event async for event in turn.events()]
            trusted = [event async for event in turn.events(include_execution_details=True)]
            assert PRIVATE not in json.dumps([asdict(event) for event in ordinary])
            assert PRIVATE not in repr(trusted)
            facts = [event.record for event in trusted if isinstance(event, ResponseFactReceived)]
            assert facts and PRIVATE in json.dumps(facts)
            assert all(row["thread_id"] == sessions[0].id for row in facts)
            assert all(row["turn_id"] == turn.turn_id for row in facts)
            assert all(row["response_id"] == "response-1" for row in facts)
            assert all(row["upstream_request_id"] == "upstream-1" for row in facts)
            assert not any(isinstance(event, EventGap) for event in trusted)
            assert [event.sequence for event in trusted] == list(range(1, len(trusted) + 1))
            terminal = next(event for event in trusted if isinstance(event, TurnFinished))
            assert terminal.status == ("failed" if refused else "completed")
            # Each explicit reader owns its copy, including the trusted record.
            facts[0]["fact"].clear()
            again = [event async for event in turn.events(include_execution_details=True)]
            assert all(
                event.record["fact"] for event in again if isinstance(event, ResponseFactReceived)
            )
    assert len(readers) == len(sessions) == 1
    assert lifecycle_openings == [atomic_opening_placement()]
    assert sessions[0]._closed and sessions[0]._reader.done()
    assert sessions[0]._provider_lease is None


@pytest.mark.parametrize("buffered", [False, True])
async def test_lifecycle_current_model_deadline_remains_a_failed_native_error(
    deadline_peer, monkeypatch, buffered
):
    readers = instrument_reader(monkeypatch)
    sessions, lifecycle_openings = capture_lifecycle_sessions(monkeypatch)
    gate = Gate()
    deadline_peer.terminal_gate = gate
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(assistant_output("too late"))
    async with timeout(30), provider_for(deadline_peer, buffered=buffered) as provider:
        task = asyncio.create_task(
            Agent(
                instructions="Answer.",
                model="gpt-5.2",
                provider=provider,
                model_settings={"invocation_timeout_ms": MODEL_MS},
            ).run("deadline")
        )
        try:
            await wait_for_phase(gate.entered, task)
            error = await deadline_error(task)
            assert error.code == "model_invocation_deadline_exceeded"
            assert len(sessions) == 1 and sessions[0]._last_turn is not None
            with pytest.raises(NativeError) as owned_error:
                await sessions[0]._last_turn.get_result()
            assert owned_error.value is error
            events = [event async for event in sessions[0]._last_turn.events()]
            terminal = next(event for event in events if isinstance(event, TurnFinished))
            assert terminal.status == "failed" and terminal.error_type == "NativeError"
        finally:
            gate.release.set()
            await asyncio.gather(task, return_exceptions=True)
    assert len(readers) == 1 and sessions[0]._closed
    assert lifecycle_openings == [atomic_opening_placement()]
    assert sessions[0]._provider_lease is None

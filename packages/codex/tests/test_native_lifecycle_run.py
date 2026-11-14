"""Compare shared atomic runs with the ordinary dedicated lifecycle."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from contextlib import asynccontextmanager
from typing import Any

import pytest

from agents import Agent, NativeProvider, Session
from agents.observation import (
    EventGap,
    ModelRequestDispatching,
    ModelResponseLinked,
    OutputTextDelta,
    SessionEnded,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_buffered_responses import json_peer, json_response
from tests.test_native_plain_event_collection import instrument_reader, normalized_observations


@pytest.fixture(autouse=True)
def exercise_shared_atomic_opening(monkeypatch):
    """Keep coverage of atomic opening below the separately tested completed-run shortcut."""
    module = importlib.import_module("_agents_codex.agent")
    monkeypatch.setattr(module, "_completed_run_eligible", lambda *args: False)


def atomic_opening_placement() -> dict[str, bool]:
    """Core admits retained managed execution only on Linux."""
    shared = sys.platform == "linux"
    return {"atomic": True, "shared": shared, "no_home": shared}


def capture_lifecycle_sessions(monkeypatch):
    original = Session._open.__func__
    sessions, openings = [], []

    async def open_session(cls, *args, **kwargs):
        session = await original(cls, *args, **kwargs)
        sessions.append(session)
        openings.append(
            {
                "atomic": kwargs.get("_atomic_first_turn"),
                "shared": session._engine._runtime_is_shared,
                "no_home": session._home is None,
            }
        )
        return session

    monkeypatch.setattr(Session, "_open", classmethod(open_session))
    return sessions, openings


class OrdinaryAgent(Agent):
    @asynccontextmanager
    async def session(self, **kwargs: Any):
        async with super().session(**kwargs) as session:
            yield session


def http_provider(peer: ResponsesFixture) -> NativeProvider:
    return NativeProvider(
        base_url=peer.url,
        api_key="synthetic-owned-run-key",
        supports_websockets=False,
    )


async def test_native_lifecycle_buffered_run_matches_ordinary_results(monkeypatch):
    probes = instrument_reader(monkeypatch)
    async with timeout(10), json_peer() as peer:
        async with NativeProvider(
            base_url=peer.url, api_key="synthetic-owned-buffered-key", response_mode="buffered"
        ) as provider:
            results = []
            for agent_type in (Agent, OrdinaryAgent):
                peer.json_replies.append(json_response("answer 界", "response", "item"))
                completed = await agent_type(
                    model="gpt-5.2", instructions="Reply.", provider=provider
                ).run("fresh input")
                events = [event async for event in completed.turn.events()]
                assert completed.final_output == "answer 界"
                results.append((completed.thread_usage, events))
                if agent_type is Agent:
                    assert len(probes) == 1
            assert len(probes) == 2
            assert results[0][0] == results[1][0]
            for _, events in results:
                assert (
                    "".join(e.delta for e in events if isinstance(e, OutputTextDelta))
                    == "answer 界"
                )
                dispatches = [e for e in events if isinstance(e, ModelRequestDispatching)]
                assert len(dispatches) == 1 and dispatches[0].transport == "http_json"
            assert len(peer.requests) == 2
            assert all(request["stream"] is False for request in peer.requests)


@pytest.mark.parametrize(
    "chunks", [("one",), tuple(f"part-{i};" for i in range(80)), ("🦀é", "終")]
)
@pytest.mark.parametrize("annotated", [False, True])
async def test_native_lifecycle_run_preserves_result_and_retained_events(
    responses, monkeypatch, chunks, annotated
):
    probes = instrument_reader(monkeypatch)

    def reply():
        events = text_response("out", *chunks)
        if annotated:
            events[-1]["item"]["content"][0]["annotations"] = [
                {
                    "type": "url_citation",
                    "start_index": 0,
                    "end_index": 1,
                    "url": "https://example.com",
                    "title": "Source",
                }
            ]
        return events

    async with timeout(10), http_provider(responses) as provider:
        responses.replies.append(reply())
        completed = await Agent(instructions="Answer directly.", provider=provider).run("input")
        retained = [event async for event in completed.turn.events()]
        assert len(probes) == 1  # The atomic opening keeps the ordinary reader.
        assert completed.final_output == "".join(chunks)
        assert "".join(
            event.delta for event in retained if isinstance(event, OutputTextDelta)
        ) == "".join(chunks)

        responses.requests.clear()
        responses.replies.append(reply())
        control = await OrdinaryAgent(instructions="Answer directly.", provider=provider).run(
            "input"
        )
        ordinary = [event async for event in control.turn.events()]
        assert len(probes) == 2
        assert completed.final_output == control.final_output
        assert completed.thread_usage == control.thread_usage

        # The arbiter merges independent native queues. Preserve order within
        # each queue; their relative positions need not match across runs.
        metadata_types = (ModelRequestDispatching, ModelResponseLinked)
        for metadata in (False, True):
            projected = []
            for events in (retained, ordinary):
                selected = [
                    event for event in events if isinstance(event, metadata_types) == metadata
                ]
                rows = normalized_observations(selected)
                for row in rows:
                    del row["sequence"]
                projected.append(rows)
            assert projected[0] == projected[1]
        for events in (retained, ordinary):
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))


@pytest.mark.parametrize(
    "chunks", [tuple(f"{i:03d};" for i in range(300)), ("🦀" * 20_000, "x" * 65_536, "tail")]
)
async def test_native_lifecycle_retention_gaps_preserve_the_same_output_suffix(
    responses, monkeypatch, chunks
):
    probes = instrument_reader(monkeypatch)
    usages = []
    async with timeout(10), http_provider(responses) as provider:
        for agent_type in (Agent, OrdinaryAgent):
            responses.requests.clear()
            responses.replies.append(text_response("out", *chunks))
            completed = await agent_type(instructions="Reply.", provider=provider).run("input")
            assert completed.final_output == "".join(chunks)
            events = [event async for event in completed.turn.events()]
            session_events = [event async for event in completed.turn._session.events()]
            gap = session_events[0] if isinstance(session_events[0], EventGap) else None
            if len(chunks) != 300:
                assert gap is not None
            if gap is not None:
                assert gap.first_sequence == 1
            records = session_events[1:] if gap is not None else session_events
            assert records and len(records) <= 256
            if gap is not None and len(chunks) == 300:
                assert len(records) == 256
            assert isinstance(records[-1], SessionEnded)
            assert [event.sequence for event in records] == list(
                range(gap.sequence + 1 if gap is not None else 1, records[-1].sequence + 1)
            )
            # Turn events keep global sequence/gap positions but exclude SessionEnded.
            assert events == [
                event for event in session_events if not isinstance(event, SessionEnded)
            ]
            parts = [event.delta for event in events if isinstance(event, OutputTextDelta)]
            assert parts and "".join(chunks).endswith("".join(parts))
            if gap is None:
                assert "".join(parts) == "".join(chunks)
            if len(chunks) != 300:
                assert parts == list(chunks[-len(parts) :])
            usages.append(completed.thread_usage)
            if agent_type is Agent:
                assert len(probes) == 1
        assert len(probes) == 2
        # Metadata/Core queue interleavings can change which delta occupies the
        # oldest retained slot; each run must preserve its own exact suffix.
        assert usages[0] == usages[1]


async def test_native_lifecycle_runs_have_independent_fresh_histories(responses, monkeypatch):
    probes = instrument_reader(monkeypatch)
    sessions, openings = capture_lifecycle_sessions(monkeypatch)
    async with timeout(10), http_provider(responses) as provider:
        agent = Agent(instructions="Answer directly.", provider=provider)
        results = []
        for index in range(2):
            responses.replies.append(text_response(f"out-{index}", f"answer-{index}"))
            results.append(await agent.run(f"independent-input-{index}"))
        assert len(probes) == 2
        assert openings == [atomic_opening_placement()] * 2
        assert all(session._closed and session._reader.done() for session in sessions)
        assert results[0].turn.turn_id != results[1].turn.turn_id
        assert results[0].turn.submission_id != results[1].turn.submission_id
        assert len(responses.requests) == 2
        for index, request in enumerate(responses.requests):
            encoded = json.dumps(request["input"])
            assert f"independent-input-{index}" in encoded
            assert f"independent-input-{1 - index}" not in encoded


async def test_native_lifecycle_run_joins_repeated_cancellation(responses, monkeypatch):
    probes = instrument_reader(monkeypatch)
    emitted = asyncio.Event()
    responses.delta_gate = asyncio.Event()

    async def before_event(event):
        if event["type"] == "response.output_text.delta":
            emitted.set()

    responses.before_event = before_event
    responses.replies.append(text_response("out", "partial", "later"))
    async with timeout(10), http_provider(responses) as provider:
        agent = Agent(instructions="Answer directly.", provider=provider)
        running = asyncio.create_task(agent.run("cancel this fresh conversation"))
        try:
            await emitted.wait()
            assert len(probes) == 1
            running.cancel()
            await asyncio.sleep(0)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            responses.delta_gate.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

        responses.before_event = None
        responses.delta_gate = None
        responses.replies.append(text_response("next", "next conversation"))
        completed = await agent.run("independent after cancellation")
        assert completed.final_output == "next conversation"
        assert len(probes) == 2


async def test_profile_runs_use_fresh_ordinary_owners_and_current_authority(responses, monkeypatch):
    from agents import _native

    sessions, openings = capture_lifecycle_sessions(monkeypatch)

    def unexpected_atomic_open(*args, **kwargs):
        raise AssertionError("Captured profiles must use ordinary opening before native effects")

    async with timeout(30), http_provider(responses) as provider:
        responses.respond(assistant_output("plain warmup"))
        warmup = await Agent(instructions="Plain source.", provider=provider).run(
            "Warm plain input"
        )
        assert warmup.final_output == "plain warmup"
        assert openings[0] == atomic_opening_placement()
        monkeypatch.setattr(_native, "_open_conversation", unexpected_atomic_open)
        for index, effort in enumerate(("low", "high")):
            target = Agent(
                name="B",
                instructions=f"Destination graph {index}.",
                model_settings={"reasoning_effort": effort},
            )
            source = Agent(
                name="A",
                instructions=f"Source graph {index}.",
                model_settings={"reasoning_effort": "medium"},
                handoffs=[target],
                provider=provider,
            )
            responses.respond(function_call("transfer_to_b", {}, f"handoff-{index}"))
            responses.respond(assistant_output(f"destination answer {index}"))
            result = await source.run(f"Fresh graph input {index}")
            assert result.final_output == f"destination answer {index}"
            first, second = responses.requests[1 + index * 2 : 3 + index * 2]
            assert first["reasoning"]["effort"] == "medium"
            assert second["reasoning"]["effort"] == effort
            assert f"Source graph {index}." in json.dumps(first["input"])
            assert f"Destination graph {index}." in json.dumps(second["input"])
            for request in (first, second):
                encoded = json.dumps(request["input"])
                assert f"Fresh graph input {index}" in encoded
                assert f"graph {1 - index}." not in encoded
                assert f"Fresh graph input {1 - index}" not in encoded
                assert "Warm plain input" not in encoded
    assert len(sessions) == len(openings) == 3
    assert len({session.id for session in sessions}) == 3
    assert all(not row["shared"] and not row["no_home"] for row in openings[1:])
    assert all(session._closed and session._resources_retired for session in sessions)
    assert all(session._reader.done() and session._provider_lease is None for session in sessions)

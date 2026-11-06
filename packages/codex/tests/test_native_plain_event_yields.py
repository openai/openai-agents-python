"""Check actual reader scheduling at passive prefixes and terminal barriers."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from agents import Agent, NativeProvider, Session
from agents.observation import OutputTextDelta
from tests.native_fixtures import responses as responses
from tests.test_native_agent_observation import text_response
from tests.test_native_plain_event_collection import ReadProbe, instrument_reader

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="prototype retains the Windows single-reader route"
)


@pytest.mark.parametrize("decline_after_first", [False, True])
async def test_packet_replay_yields_at_barriers_and_after_guard_failure(
    responses, monkeypatch, decline_after_first
):
    probes = instrument_reader(monkeypatch)
    witnesses = []
    observed = []
    first_delta = False
    original_batch = ReadProbe._next_session_event_batch
    original_observe = Session._observe_mcp

    async def read_batch(self, turn_id):
        result = await original_batch(self, turn_id)
        marker = asyncio.Event()
        witnesses.append((self, json.loads(result[0]), marker))
        # An ordinary per-event sleep would run this before the first replayed
        # passive delta. The barrier must run it before terminal dispatch.
        asyncio.get_running_loop().call_soon(marker.set)
        return result

    def observe(self, event, state):
        nonlocal first_delta
        for probe, packet, marker in reversed(witnesses):
            if self._native is probe and event in packet:
                kind = event["msg"]["type"]
                if kind == "agent_message_content_delta":
                    observed.append((kind, marker.is_set()))
                    if decline_after_first and first_delta:
                        assert marker.is_set(), "failed guard suppressed the ordinary yield"
                    else:
                        assert not marker.is_set(), "passive replay retained a per-event yield"
                    first_delta = True
                    if decline_after_first:
                        self._plain_event_collection = False
                elif kind in {"turn_complete", "task_complete", "turn_aborted"}:
                    observed.append((kind, marker.is_set()))
                    assert marker.is_set(), "terminal dispatched before its scheduling barrier"
                break
        original_observe(self, event, state)

    monkeypatch.setattr(ReadProbe, "_next_session_event_batch", read_batch)
    monkeypatch.setattr(Session, "_observe_mcp", observe)
    chunks = tuple(f"part-{i};" for i in range(8))
    responses.replies.append(text_response("out", *chunks))
    completed = await Agent(
        instructions="Answer directly.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-batch-key"),
    ).run("input")
    retained = [event async for event in completed.turn.events()]
    assert completed.final_output == "".join(chunks)
    assert [event.delta for event in retained if isinstance(event, OutputTextDelta)] == list(chunks)
    assert observed and first_delta and any(probe.batches for probe in probes)
    if decline_after_first:
        assert sum(len(probe.batches) for probe in probes) == 1
        # At least one later event in the same packet proves that guard failure
        # returned pending replay, not only the next read, to ordinary yielding.
        assert any(kind == "agent_message_content_delta" and yielded for kind, yielded in observed)
    else:
        assert any(
            kind in {"turn_complete", "task_complete"} and yielded for kind, yielded in observed
        )

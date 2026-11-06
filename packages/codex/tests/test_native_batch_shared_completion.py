"""A pending real batch crosses the shared completion owner and keeps its join."""

from __future__ import annotations

import asyncio
import contextvars
import json
import sys

import pytest

from _agents_codex import _native_async as completion_runtime
from agents import Agent, NativeProvider
from agents.observation import OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses
from tests.test_native_agent_observation import text_response
from tests.test_native_plain_event_collection import ReadProbe, instrument_reader

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux shared completion channel")


@pytest.mark.parametrize("cancel_run", [False, True], ids=["complete", "cancel"])
async def test_pending_batch_uses_shared_completion_for_result_and_join(
    responses, monkeypatch, cancel_run
):
    probes = instrument_reader(monkeypatch)
    in_batch = contextvars.ContextVar("real_pending_batch", default=False)
    shared_started = asyncio.Event()
    peer_held = asyncio.Event()
    release_peer = asyncio.Event()
    responses.delta_gate = release_peer
    original_batch = ReadProbe._next_session_event_batch
    original_completion = completion_runtime._await_native_completion
    calls = []
    delivered_values = []

    class SharedBatchCall:
        def __init__(self, call):
            self.call = call
            self.accepted = False
            self.value = None
            self.returned = False
            self.joined_after_return = False

        def __getattr__(self, name):
            return getattr(self.call, name)

        def start(self, writer):
            pytest.fail("A pending batch silently used legacy socket completion")

        def start_shared(self, channel, identifier):
            accepted = self.call.start_shared(channel, identifier)
            if accepted:
                self.accepted = True
                shared_started.set()
            return accepted

    async def read_batch(self, turn_id):
        token = in_batch.set(True)
        try:
            value = await original_batch(self, turn_id)
            delivered_values.append(value)
            return value
        finally:
            in_batch.reset(token)

    async def completion(call):
        if not in_batch.get():
            return await original_completion(call)
        observed = SharedBatchCall(call)
        calls.append(observed)
        observed.value = await original_completion(observed)
        observed.joined_after_return = call.is_joined()
        observed.returned = True
        return observed.value

    async def before_event(event):
        if event["type"] == "response.output_text.delta":
            peer_held.set()

    monkeypatch.setattr(ReadProbe, "_next_session_event_batch", read_batch)
    # Patch the function's actual globals owner, not the public facade.
    monkeypatch.setattr(completion_runtime, "_await_native_completion", completion)
    responses.before_event = before_event
    chunks = tuple(f"shared-part-{i};" for i in range(8))
    responses.replies.append(text_response("shared-batch-output", *chunks))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-shared-batch-key", supports_websockets=False
    )
    agent = Agent(instructions="Answer directly.", provider=provider)
    running = None
    async with timeout(30), provider:
        try:
            running = asyncio.create_task(agent.run("shared batch input"))
            async with timeout(10):
                await peer_held.wait()
                while True:
                    # Subscribe before inspection. A newly accepted shared call
                    # cannot be missed between this scan and the wait below.
                    shared_started.clear()
                    pending = [
                        item for item in calls if item.accepted and not item.call.is_joined()
                    ]
                    if pending:
                        witness = pending[-1]
                        break
                    await shared_started.wait()
            assert probes and not release_peer.is_set()
            assert witness.accepted and not witness.call.is_joined()
            if cancel_run:
                # Cancel the public owner, never its native reader. Normal
                # abort publication and Session cleanup must join the read.
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
                assert all(probe.cancelled_reads == 0 for probe in probes)
                assert any(
                    event["msg"]["type"] == "turn_aborted"
                    for probe in probes
                    for packet in probe.batches
                    for event in packet
                )
            else:
                release_peer.set()
                completed = await running
                retained = [event async for event in completed.turn.events()]
                assert completed.final_output == "".join(chunks)
                assert completed.usage is not None
                deltas = [event.delta for event in retained if isinstance(event, OutputTextDelta)]
                assert deltas == list(chunks)
            # This exact accepted, genuinely unjoined NativeCall must return
            # the actual EventValue packet through shared completion, not a
            # separately ready read or an unrelated scalar call.
            assert witness.returned and witness.joined_after_return
            assert any(value is witness.value for value in delivered_values)
            assert witness.call.is_joined()
            assert isinstance(witness.value, tuple) and len(witness.value) == 3
            encoded, error, barrier = witness.value
            assert isinstance(encoded, str) and isinstance(barrier, bool)
            assert error is None
            packet = json.loads(encoded)
            assert isinstance(packet, list) and packet
            assert any(packet in probe.batches for probe in probes)
            assert all(item.call.is_joined() for item in calls if item.accepted)
        finally:
            release_peer.set()
            if running is not None:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

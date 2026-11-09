"""Drain real canonical Realtime history before releasing the public voice owner."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import pytest

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer

TRANSCRIPT = "The native canonical transcript tail must survive voice close."


class DelayCanonicalTail:
    """Retain native event bytes in order; delay delivery without replacing dispatch."""

    def __init__(self, native: Any) -> None:
        self.native = native
        self.pending: deque[str] = deque()
        self.generation: str | None = None
        self.history_held = asyncio.Event()
        self.release_history = asyncio.Event()
        self.close_acknowledged = asyncio.Event()
        self.closed_held = asyncio.Event()
        self.release_closed = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    async def call(self, operation: str, params_json: str = "{}") -> str:
        result: str = await self.native.call(operation, params_json)
        if (
            operation == "realtime_conversation_close_sdk"
            and json.loads(params_json)["generation"] == self.generation
        ):
            self.close_acknowledged.set()
        return result

    async def _next_session_event(self) -> str:
        if self.pending:
            return self.pending.popleft()
        raw: str = await self.native._next_session_event()
        event = json.loads(raw)
        message = event.get("msg", {}) if event is not None else {}
        payload = message.get("payload", {})
        started = payload.get("HistoryItemStarted", {}) if isinstance(payload, dict) else {}
        if self.generation is None and started.get("type") == "transcript_segment":
            self.generation = event["id"]
            item_id = started["id"]
            self.pending.append(raw)
            # Preserve the real Started/delta/Completed order while retaining
            # the completed canonical item before the Session reader sees it.
            while True:
                raw = await self.native._next_session_event()
                self.pending.append(raw)
                next_event = json.loads(raw)
                assert next_event is not None
                next_payload = next_event.get("msg", {}).get("payload", {})
                completed = (
                    next_payload.get("HistoryItemCompleted", {})
                    if isinstance(next_payload, dict)
                    else {}
                )
                if completed.get("id") == item_id:
                    assert completed["text"] == TRANSCRIPT
                    self.history_held.set()
                    await self.release_history.wait()
                    return self.pending.popleft()
        if message.get("type") == "realtime_conversation_closed" and event["id"] == self.generation:
            self.closed_held.set()
            await self.release_closed.wait()
        return raw


async def test_native_voice_close_drains_canonical_tail_and_exact_closed_before_replacement(
    realtime_peer: RealtimePeer, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = realtime_peer
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-history-drain-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Keep canonical voice history.", provider=provider).session() as session,
    ):
        delayed = DelayCanonicalTail(session._native)
        monkeypatch.setattr(session, "_native", delayed)
        voice = session.realtime(config=RealtimeConfig(model="fixture-realtime", voice="marin"))
        closing: asyncio.Task[None] | None = None
        try:
            await voice.__aenter__()
            assert peer.socket is not None
            await peer.socket.send_json(
                {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": "provider-transcript-tail",
                    "delta": TRANSCRIPT,
                }
            )
            await peer.socket.send_json(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "provider-transcript-tail",
                    "transcript": TRANSCRIPT,
                }
            )
            await delayed.history_held.wait()
            assert all(item.text != TRANSCRIPT for item in voice.history)
            closing = asyncio.create_task(voice.aclose())
            await delayed.close_acknowledged.wait()
            assert not closing.done()
            with pytest.raises(RuntimeError, match="already has a Realtime owner"):
                async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                    pytest.fail("Replacement must wait for the original canonical tail")

            delayed.release_history.set()
            await delayed.closed_held.wait()
            transcript = [item for item in voice.history if item.kind == "transcript_segment"]
            assert len(transcript) == 1
            assert transcript[0].text == TRANSCRIPT
            assert transcript[0].role == "user"
            assert not closing.done()
            with pytest.raises(RuntimeError, match="already has a Realtime owner"):
                async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                    pytest.fail("The exact Closed receipt still belongs to the original owner")

            delayed.release_closed.set()
            await closing
            assert any(item.kind == "realtime_session_closed" for item in voice.history)
            original_history = voice.history
            async with session.realtime(
                config=RealtimeConfig(model="fixture-realtime", voice="marin")
            ) as replacement:
                assert peer.connections == 2
                await replacement.send_text("replacement-after-canonical-drain")
                await peer.frame(
                    lambda frame: (
                        frame["type"] == "conversation.item.create"
                        and any(
                            part.get("text") == "replacement-after-canonical-drain"
                            for part in frame.get("item", {}).get("content", [])
                        )
                    )
                )
            assert voice.history == original_history
        finally:
            delayed.release_history.set()
            delayed.release_closed.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await voice.aclose()

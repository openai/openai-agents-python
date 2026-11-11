"""Canonical adapter frames obey structural admission before generic conversion."""

import json

import pytest

import _agents_codex.realtime as realtime
from agents import Agent
from agents.realtime import (
    RealtimeConfig,
    RealtimeConversationItemObserved,
    RealtimeProviderRequestError,
)
from tests._asyncio import timeout
from tests.test_native_realtime_adapter import SocketAdapter, adapter_peer as adapter_peer, provider


def frame_for(kind, count):
    ignored = [{} for _ in range(count)]
    if kind == "error":
        # Omit both message fields to exercise the generic JSON-text fallback.
        return {"type": "error", "error": {"code": "fixture_failure", "ignored": ignored}}
    return {
        "type": f"conversation.item.{kind}",
        "item": {
            "id": "generic-item",
            "type": "message",
            "role": "user",
            "status": "completed",
            "content": [],
            "ignored": ignored,
        },
    }


@pytest.mark.parametrize("kind", ["added", "created", "error"])
async def test_native_adapter_rejects_compact_generic_frame_before_conversion(
    adapter_peer, monkeypatch, kind
):
    original = realtime.RealtimeSession._payload
    delivered = []

    def capture(self, payload, native_input):
        if isinstance(payload, dict):
            observed = payload.get("ConversationItemObserved")
            if isinstance(observed, dict) and observed.get("item_id") == "generic-item":
                delivered.append("generic-item")
                self._finish(RuntimeError("Dense generic item escaped native guard"))
                return
            error = payload.get("Error")
            if isinstance(error, str) and "fixture_failure" in error:
                delivered.append("generic-error")
                self._finish(RuntimeError("Generic error fallback escaped native guard"))
                return
        original(self, payload, native_input)

    monkeypatch.setattr(realtime.RealtimeSession, "_payload", capture)
    adapter = SocketAdapter(adapter_peer.custom_url)
    frame = frame_for(kind, 33_000)
    assert len(json.dumps(frame, separators=(",", ":"))) < 150_000
    async with (
        timeout(10),
        provider(adapter_peer) as backend,
        Agent(instructions="Speak.", provider=backend).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as voice,
    ):
        assert adapter_peer.socket is not None
        await adapter_peer.socket.send_json(frame)
        with pytest.raises(RuntimeError, match="capacity"):
            async for _ in voice.events():
                pass
        assert delivered == []


@pytest.mark.parametrize("kind", ["added", "created", "error"])
async def test_native_adapter_preserves_bounded_generic_frames(adapter_peer, kind):
    adapter = SocketAdapter(adapter_peer.custom_url)
    async with (
        timeout(10),
        provider(adapter_peer) as backend,
        Agent(instructions="Speak.", provider=backend).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as voice,
    ):
        assert adapter_peer.socket is not None
        await adapter_peer.socket.send_json(frame_for(kind, 2))
        if kind == "error":
            async for event in voice.events():
                if isinstance(event, RealtimeProviderRequestError):
                    assert event == RealtimeProviderRequestError("fixture_failure", None)
                    break
            else:
                pytest.fail("Bounded generic error was not observed")
        else:
            async for event in voice.events():
                if isinstance(event, RealtimeConversationItemObserved):
                    assert event.item_id == "generic-item"
                    assert event.kind == "message"
                    assert event.role == "user"
                    assert event.status == "added"
                    break
            else:
                pytest.fail("Bounded generic item was not observed")

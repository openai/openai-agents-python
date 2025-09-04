from __future__ import annotations

import json
from typing import Any, cast

import pytest
from websockets.asyncio.client import ClientConnection

from agents.realtime.config import RealtimeSessionModelSettings
from agents.realtime.model_inputs import RealtimeModelSendSessionUpdate
from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel


class _DummyWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_session_update_flattens_audio_and_modalities() -> None:
    model = OpenAIRealtimeWebSocketModel()
    # Inject a dummy websocket so send() works without a network
    dummy = _DummyWS()
    model._websocket = cast(ClientConnection, dummy)

    settings: dict[str, object] = {
        "model_name": "gpt-realtime",
        "modalities": ["text", "audio"],
        "input_audio_format": "pcm16",
        "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
        "output_audio_format": "pcm16",
        "turn_detection": {"type": "semantic_vad", "threshold": 0.5},
        "voice": "ash",
        "speed": 1.0,
        "max_output_tokens": 2048,
    }

    await model.send_event(
        RealtimeModelSendSessionUpdate(
            session_settings=cast(RealtimeSessionModelSettings, settings)
        )
    )

    # One session.update should have been sent
    assert dummy.sent, "no websocket messages were sent"
    payload = json.loads(dummy.sent[-1])
    assert payload["type"] == "session.update"
    session = payload["session"]

    # GA expects flattened fields, not session.audio or session.type
    assert "audio" not in session
    assert "type" not in session
    # Modalities field is named 'modalities' in GA
    assert session.get("modalities") == ["text", "audio"]
    # Audio fields flattened
    assert session.get("input_audio_format") == "pcm16"
    assert session.get("output_audio_format") == "pcm16"
    assert isinstance(session.get("input_audio_transcription"), dict)
    assert isinstance(session.get("turn_detection"), dict)
    # Token field name normalized
    assert session.get("max_response_output_tokens") == 2048


@pytest.mark.asyncio
async def test_no_auto_interrupt_on_vad_speech_started(monkeypatch: Any) -> None:
    model = OpenAIRealtimeWebSocketModel()

    called = {"interrupt": False}

    async def _fake_interrupt(event: Any) -> None:
        called["interrupt"] = True

    # Prevent network use; _websocket only needed for other paths
    model._websocket = cast(ClientConnection, _DummyWS())
    monkeypatch.setattr(model, "_send_interrupt", _fake_interrupt)

    # This event previously triggered an interrupt; now it should be ignored
    await model._handle_ws_event({"type": "input_audio_buffer.speech_started"})

    assert called["interrupt"] is False

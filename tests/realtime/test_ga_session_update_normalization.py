from __future__ import annotations

from typing import Any, cast

import pytest
from websockets.asyncio.client import ClientConnection

from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel


class _DummyWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


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

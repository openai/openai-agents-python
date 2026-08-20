from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI

from agents.voice import STTModelSettings, StreamedAudioInput
from agents.voice.models import openai_stt
from agents.voice.models.openai_stt import OpenAISTTTranscriptionSession


class _RotatingClient:
    def __init__(self) -> None:
        self.api_key = ""
        self.refresh_calls = 0

    async def _refresh_api_key(self) -> None:
        self.refresh_calls += 1
        self.api_key = "sk-refreshed"


class _WebSocketContext:
    async def __aenter__(self) -> Any:
        return object()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_streamed_stt_refreshes_callable_api_key_before_handshake(monkeypatch) -> None:
    client = _RotatingClient()
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=cast(AsyncOpenAI, client),
        model="gpt-4o-mini-transcribe",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )

    captured_headers: dict[str, str] = {}

    def connect(_url: str, *, additional_headers: dict[str, str]) -> _WebSocketContext:
        captured_headers.update(additional_headers)
        return _WebSocketContext()

    monkeypatch.setattr(openai_stt.websockets, "connect", connect)
    monkeypatch.setattr(
        session,
        "_setup_connection",
        AsyncMock(side_effect=RuntimeError("stop after handshake")),
    )

    with pytest.raises(RuntimeError, match="stop after handshake"):
        await session._process_websocket_connection()

    assert client.refresh_calls == 1
    assert captured_headers["Authorization"] == "Bearer sk-refreshed"

import asyncio
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from openai import AsyncOpenAI

from agents.voice import OpenAISTTTranscriptionSession, StreamedAudioInput, STTModelSettings
from agents.voice.exceptions import STTWebsocketConnectionError


def create_mock_openai_client(api_key: str = "FAKE_KEY") -> AsyncOpenAI:
    client = AsyncMock(api_key=api_key)
    client.websocket_base_url = None
    client.base_url = httpx2.URL("https://api.openai.com/v1/")
    client.default_query = {}
    client.auth_headers = {"Authorization": f"Bearer {api_key}"}
    client.default_headers = {}
    client._refresh_api_key = AsyncMock()
    return cast(AsyncOpenAI, client)


@pytest.mark.asyncio
async def test_clean_websocket_close_during_setup_fails_without_waiting_for_timeout() -> None:
    mock_ws = AsyncMock()
    mock_ws.__aenter__.return_value = mock_ws
    mock_ws.__aiter__.return_value = iter([])

    with patch("websockets.connect", return_value=mock_ws):
        session = OpenAISTTTranscriptionSession(
            input=StreamedAudioInput(),
            client=create_mock_openai_client(),
            model="whisper-1",
            settings=STTModelSettings(),
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

        async def consume_turns() -> None:
            async for _ in session.transcribe_turns():
                pass

        with pytest.raises(STTWebsocketConnectionError):
            await asyncio.wait_for(consume_turns(), timeout=1)

        await session.close()

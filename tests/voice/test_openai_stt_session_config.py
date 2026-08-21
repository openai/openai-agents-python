import json
from unittest.mock import AsyncMock

import pytest

from agents.voice import StreamedAudioInput, STTModelSettings
from agents.voice.models.openai_stt import OpenAISTTTranscriptionSession


@pytest.mark.asyncio
async def test_streaming_stt_sends_language_and_prompt() -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-4o-transcribe",
        settings=STTModelSettings(language="fr", prompt="domain vocabulary"),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-transcribe",
        "language": "fr",
        "prompt": "domain vocabulary",
    }


@pytest.mark.asyncio
async def test_streaming_stt_sends_singular_language_for_gpt_transcribe() -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-transcribe",
        settings=STTModelSettings(language="fr", prompt="domain vocabulary"),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-transcribe",
        "language": "fr",
        "prompt": "domain vocabulary",
    }


@pytest.mark.asyncio
async def test_streaming_stt_sends_plural_languages_for_gpt_live_transcribe() -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-live-transcribe",
        settings=STTModelSettings(language="fr", prompt="domain vocabulary"),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-live-transcribe",
        "languages": ["fr"],
        "prompt": "domain vocabulary",
    }


@pytest.mark.asyncio
async def test_streaming_stt_omits_unset_language_and_prompt() -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-4o-transcribe",
        settings=STTModelSettings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-transcribe"
    }

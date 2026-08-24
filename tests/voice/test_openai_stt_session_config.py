import json
from unittest.mock import AsyncMock

import pytest

from agents.voice import StreamedAudioInput, STTModelSettings
from agents.voice.models.openai_stt import OpenAISTTTranscriptionSession


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "language_field", "language_value"),
    [
        ("gpt-4o-transcribe", "language", "fr"),
        ("gpt-transcribe", "languages", ["fr"]),
        ("gpt-live-transcribe", "languages", ["fr"]),
    ],
)
async def test_streaming_stt_sends_language_and_prompt(
    model: str,
    language_field: str,
    language_value: str | list[str],
) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model=model,
        settings=STTModelSettings(language="fr", prompt="domain vocabulary"),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": model,
        language_field: language_value,
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
    assert payload["session"]["audio"]["input"]["transcription"] == {"model": "gpt-4o-transcribe"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-4o-transcribe", "gpt-transcribe", "gpt-live-transcribe"])
async def test_streaming_stt_sends_languages_over_language(model: str) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model=model,
        settings=STTModelSettings(language="fr", languages=["fr", "en"]),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": model,
        "languages": ["fr", "en"],
    }


@pytest.mark.asyncio
async def test_streaming_stt_sends_keywords_and_delay() -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-live-transcribe",
        settings=STTModelSettings(keywords=["agents", "sdk"], delay="low"),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-live-transcribe",
        "keywords": ["agents", "sdk"],
        "delay": "low",
    }


@pytest.mark.asyncio
async def test_streaming_stt_delay_with_disabled_turn_detection() -> None:
    """gpt-realtime-whisper requires turn_detection: null alongside the delay option."""
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-realtime-whisper",
        settings=STTModelSettings(delay="high", turn_detection={"type": "none"}),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    audio_input = payload["session"]["audio"]["input"]
    assert audio_input["transcription"] == {"model": "gpt-realtime-whisper", "delay": "high"}
    assert audio_input["turn_detection"] is None


@pytest.mark.asyncio
async def test_streaming_stt_default_turn_detection_unchanged() -> None:
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
    assert payload["session"]["audio"]["input"]["turn_detection"] == {"type": "semantic_vad"}

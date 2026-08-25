import json
from dataclasses import dataclass, fields
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.voice import StreamedAudioInput, STTModelSettings
from agents.voice.models.openai_stt import OpenAISTTTranscriptionSession


def test_stt_model_settings_appends_streaming_options() -> None:
    assert [field.name for field in fields(STTModelSettings)] == [
        "prompt",
        "language",
        "temperature",
        "turn_detection",
        "languages",
        "keywords",
    ]


def test_stt_model_settings_preserves_provider_subclass_positional_fields() -> None:
    @dataclass
    class ProviderSTTModelSettings(STTModelSettings):
        provider_language: str | None = None

    settings = ProviderSTTModelSettings(
        None,
        None,
        None,
        None,
        "provider-ja",
        languages=["ja"],
        keywords=["Agents SDK"],
    )

    assert settings.provider_language == "provider-ja"
    assert settings.languages == ["ja"]
    assert settings.keywords == ["Agents SDK"]


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
@pytest.mark.parametrize("model", ["gpt-transcribe", "gpt-live-transcribe"])
async def test_streaming_stt_sends_languages_over_language(model: str) -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model=model,
        settings=STTModelSettings(language="fr", languages=["fr", "eng", "zh-tw"]),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket

    await session._configure_session()

    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["session"]["audio"]["input"]["transcription"] == {
        "model": model,
        "languages": ["fr", "eng", "zh-tw"],
    }


@pytest.mark.asyncio
async def test_streaming_stt_sends_keywords() -> None:
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-live-transcribe",
        settings=STTModelSettings(keywords=["agents", "sdk"]),
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
    }


@pytest.mark.asyncio
async def test_streaming_stt_trace_records_transcription_options_with_sensitive_data() -> None:
    languages = ["en", "fr"]
    keywords = ["Agents SDK"]
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-live-transcribe",
        settings=STTModelSettings(
            prompt="customer support",
            language="en",
            temperature=0.2,
            languages=languages,
            keywords=keywords,
        ),
        trace_include_sensitive_data=True,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket
    await session._configure_session()

    languages[:] = ["de"]
    keywords[:] = ["Changed after configuration"]
    span = MagicMock()

    with patch(
        "agents.voice.models.openai_stt.transcription_span",
        return_value=span,
    ) as create_span:
        session._start_turn()
        languages[:] = ["it"]
        keywords[:] = ["Changed during the turn"]
        session._end_turn("")

    create_span.assert_called_once_with(
        model="gpt-live-transcribe",
        model_config={
            "temperature": 0.2,
            "language": None,
            "languages": ["en", "fr"],
            "keywords": ["Agents SDK"],
            "prompt": "customer support",
            "turn_detection": {"type": "semantic_vad"},
        },
    )
    span.start.assert_called_once_with()
    span.finish.assert_called_once_with()


@pytest.mark.asyncio
async def test_streaming_stt_trace_redacts_keywords_without_sensitive_data() -> None:
    sensitive_keywords = ["CUSTOMER_SECRET_NAME"]
    session = OpenAISTTTranscriptionSession(
        input=StreamedAudioInput(),
        client=AsyncMock(api_key="FAKE_KEY"),
        model="gpt-live-transcribe",
        settings=STTModelSettings(keywords=sensitive_keywords),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    websocket = AsyncMock()
    session._websocket = websocket
    await session._configure_session()
    span = MagicMock()

    with patch(
        "agents.voice.models.openai_stt.transcription_span",
        return_value=span,
    ) as create_span:
        session._start_turn()
        session._end_turn("")

    model_config = create_span.call_args.kwargs["model_config"]
    assert model_config["keywords"] is None
    assert all(value is not sensitive_keywords for value in model_config.values())
    span.start.assert_called_once_with()
    span.finish.assert_called_once_with()

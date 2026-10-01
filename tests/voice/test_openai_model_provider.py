# Tests for the OpenAI voice model provider (OpenAIVoiceModelProvider).

import json
from email.parser import BytesParser
from email.policy import default
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx2
import numpy as np
import openai
import pytest

from agents.exceptions import UserError
from agents.models import _openai_shared
from agents.voice import AudioInput, StreamedAudioInput, STTModelSettings
from agents.voice.models import openai_model_provider
from agents.voice.models.openai_model_provider import OpenAIVoiceModelProvider, shared_http_client
from agents.voice.models.openai_stt import OpenAISTTTranscriptionSession


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", [None, "gpt-4o-transcribe"])
@pytest.mark.parametrize("language", [None, "fr"])
async def test_voice_provider_transcription_model_and_language_on_wire(
    model_name: str | None, language: str | None
) -> None:
    captured: dict[str, bytes] = {}

    async def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
            + await request.aread()
        )
        for part in message.iter_parts():
            captured[part.get_param("name", header="content-disposition")] = part.get_payload(
                decode=True
            )
        return httpx2.Response(200, json={"text": "Bonjour"})

    async with openai.AsyncOpenAI(
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    ) as client:
        model = OpenAIVoiceModelProvider(openai_client=client).get_stt_model(model_name)
        transcript = await model.transcribe(
            AudioInput(buffer=np.zeros(240, dtype=np.int16)),
            STTModelSettings(language=language, prompt="A greeting", temperature=0.2),
            False,
            False,
        )

    assert transcript == "Bonjour"
    assert captured["model"] == (b"gpt-transcribe" if model_name is None else b"gpt-4o-transcribe")
    assert captured["prompt"] == b"A greeting"
    assert captured["temperature"] == b"0.2"
    if language is None:
        assert "language" not in captured
        assert "languages[]" not in captured
    elif model_name is None:
        assert captured["languages[]"] == b"fr"
        assert "language" not in captured
    else:
        assert captured["language"] == b"fr"
        assert "languages[]" not in captured


@pytest.mark.asyncio
async def test_voice_provider_streamed_default_transcription_config() -> None:
    async with openai.AsyncOpenAI(api_key="test-key") as client:
        model = OpenAIVoiceModelProvider(openai_client=client).get_stt_model(None)
        session = await model.create_session(
            StreamedAudioInput(), STTModelSettings(language="fr"), False, False
        )
        assert isinstance(session, OpenAISTTTranscriptionSession)
        websocket = AsyncMock()
        session._websocket = websocket
        await session._configure_session()
        payload = json.loads(websocket.send.await_args.args[0])
        assert payload["session"]["audio"]["input"]["transcription"] == {
            "model": "gpt-transcribe",
            "languages": ["fr"],
        }
        await session.close()


@pytest.mark.parametrize(
    "conflicting_kwargs",
    [
        {"api_key": "other_key"},
        {"base_url": "https://example.com"},
        {"organization": "org_test"},
        {"project": "proj_test"},
        {"api_key": "other_key", "base_url": "https://example.com"},
    ],
)
def test_voice_provider_rejects_client_with_conflicting_args(conflicting_kwargs):
    # Regression test for #3808: this validation used a bare `assert`, which is
    # stripped under `python -O`, silently ignoring the conflicting arguments.
    client = openai.AsyncOpenAI(api_key="test_key")
    with pytest.raises(UserError, match="Don't provide"):
        OpenAIVoiceModelProvider(openai_client=client, **conflicting_kwargs)


def test_voice_provider_accepts_client_without_conflicting_args():
    client = openai.AsyncOpenAI(api_key="test_key")
    provider = OpenAIVoiceModelProvider(openai_client=client)
    assert provider._get_client() is client


def test_voice_provider_shared_http_client_uses_httpx2() -> None:
    assert isinstance(shared_http_client(), httpx2.AsyncClient)


def test_voice_provider_preserves_falsy_default_client(monkeypatch):
    class FalsyClient:
        def __bool__(self) -> bool:
            return False

    client = cast(Any, FalsyClient())
    monkeypatch.setattr(_openai_shared, "get_default_openai_client", lambda: client)

    assert OpenAIVoiceModelProvider()._get_client() is client


@pytest.mark.parametrize(
    ("option_name", "option_value"),
    [
        ("api_key", "sk-voice"),
        ("base_url", "https://voice.example.test/v1"),
        ("organization", "org-voice"),
        ("project", "proj-voice"),
        ("api_key", ""),
        ("base_url", ""),
        ("organization", ""),
        ("project", ""),
    ],
)
def test_voice_provider_explicit_options_override_default_client(
    monkeypatch: pytest.MonkeyPatch,
    option_name: str,
    option_value: str,
) -> None:
    default_client = cast(openai.AsyncOpenAI, object())
    created_client = cast(openai.AsyncOpenAI, object())
    captured_kwargs: dict[str, Any] = {}

    def create_client(**kwargs: Any) -> openai.AsyncOpenAI:
        captured_kwargs.update(kwargs)
        return created_client

    monkeypatch.setattr(_openai_shared, "get_default_openai_client", lambda: default_client)
    monkeypatch.setattr(_openai_shared, "get_default_openai_key", lambda: "sk-global")
    monkeypatch.setattr(openai_model_provider, "AsyncOpenAI", create_client)
    monkeypatch.setattr(openai_model_provider, "shared_http_client", object)

    provider = OpenAIVoiceModelProvider(**cast(dict[str, Any], {option_name: option_value}))

    assert provider._get_client() is created_client
    assert captured_kwargs[option_name] == option_value

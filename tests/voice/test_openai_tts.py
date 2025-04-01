# Tests for the OpenAI text-to-speech model (OpenAITTSModel).

from types import SimpleNamespace
from typing import Any

import pytest

try:
    from agents.voice import OpenAITTSModel, TTSModelSettings
except ImportError:
    pass


class _FakeStreamResponse:
    """A minimal async context manager to simulate streaming audio bytes."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    async def iter_bytes(self, chunk_size: int = 1024):
        for chunk in self._chunks:
            yield chunk


def _make_fake_openai_client(fake_create) -> SimpleNamespace:
    """Construct an object with nested audio.speech.with_streaming_response.create."""
    return SimpleNamespace(
        audio=SimpleNamespace(
            speech=SimpleNamespace(with_streaming_response=SimpleNamespace(create=fake_create))
        )
    )


@pytest.mark.asyncio
async def test_openai_tts_default_voice_and_instructions() -> None:
    """If no voice is specified, OpenAITTSModel uses its default voice and passes instructions."""
    chunks = [b"abc", b"def"]
    captured: dict[str, object] = {}

    def fake_create(
        *, model: str, voice: str, input: str, response_format: str, extra_body: dict[str, Any]
    ) -> _FakeStreamResponse:
        captured["model"] = model
        captured["voice"] = voice
        captured["input"] = input
        captured["response_format"] = response_format
        captured["extra_body"] = extra_body
        return _FakeStreamResponse(chunks)

    client = _make_fake_openai_client(fake_create)
    tts_model = OpenAITTSModel(model="test-model", openai_client=client)  # type: ignore[arg-type]
    settings = TTSModelSettings()
    out: list[bytes] = []
    async for b in tts_model.run("hello world", settings):
        out.append(b)
    assert out == chunks
    assert captured["model"] == "test-model"
    assert captured["voice"] == "ash"
    assert captured["input"] == "hello world"
    assert captured["response_format"] == "pcm"
    assert captured["extra_body"] == {"instructions": settings.instructions}


@pytest.mark.asyncio
async def test_openai_tts_custom_voice_and_instructions() -> None:
    """Specifying voice and instructions are forwarded to the API."""
    chunks = [b"x"]
    captured: dict[str, object] = {}

    def fake_create(
        *, model: str, voice: str, input: str, response_format: str, extra_body: dict[str, Any]
    ) -> _FakeStreamResponse:
        captured["model"] = model
        captured["voice"] = voice
        captured["input"] = input
        captured["response_format"] = response_format
        captured["extra_body"] = extra_body
        return _FakeStreamResponse(chunks)

    client = _make_fake_openai_client(fake_create)
    tts_model = OpenAITTSModel(model="my-model", openai_client=client)  # type: ignore[arg-type]
    settings = TTSModelSettings(voice="fable", instructions="Custom instructions")
    out: list[bytes] = []
    async for b in tts_model.run("hi", settings):
        out.append(b)
    assert out == chunks
    assert captured["voice"] == "fable"
    assert captured["extra_body"] == {"instructions": "Custom instructions"}

# Tests for the OpenAI voice model provider (OpenAIVoiceModelProvider).

from typing import Any, cast

import openai
import pytest

from agents.exceptions import UserError
from agents.models import _openai_shared
from agents.voice.models.openai_model_provider import OpenAIVoiceModelProvider


@pytest.mark.parametrize(
    "conflicting_kwargs",
    [
        {"api_key": "other_key"},
        {"base_url": "https://example.com"},
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


def test_voice_provider_preserves_falsy_default_client(monkeypatch):
    class FalsyClient:
        def __bool__(self) -> bool:
            return False

    client = cast(Any, FalsyClient())
    monkeypatch.setattr(_openai_shared, "get_default_openai_client", lambda: client)

    assert OpenAIVoiceModelProvider()._get_client() is client

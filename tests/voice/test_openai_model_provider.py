# Tests for the OpenAI voice model provider (OpenAIVoiceModelProvider).

from typing import Any, cast

import httpx2
import openai
import pytest

from agents.exceptions import UserError
from agents.models import _openai_shared
from agents.voice.models import openai_model_provider
from agents.voice.models.openai_model_provider import OpenAIVoiceModelProvider, shared_http_client


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

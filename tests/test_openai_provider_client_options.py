from __future__ import annotations

from typing import Any, cast

import pytest
from openai import AsyncOpenAI

from agents.exceptions import UserError
from agents.models import _openai_shared, openai_provider
from agents.models.openai_provider import OpenAIProvider


@pytest.mark.parametrize(
    "client_option",
    [
        {"organization": "org-test"},
        {"project": "proj-test"},
    ],
)
def test_openai_provider_rejects_ignored_options_with_explicit_client(
    client_option: dict[str, str],
) -> None:
    client = cast(AsyncOpenAI, object())

    with pytest.raises(UserError, match="organization, or project"):
        OpenAIProvider(
            openai_client=client,
            **cast(dict[str, Any], client_option),
        )


@pytest.mark.parametrize(
    ("option_name", "option_value"),
    [
        ("api_key", "sk-provider"),
        ("base_url", "https://provider.example.test/v1"),
        ("websocket_base_url", "wss://provider.example.test/v1"),
        ("organization", "org-provider"),
        ("project", "proj-provider"),
    ],
)
def test_openai_provider_explicit_options_override_default_client(
    monkeypatch: pytest.MonkeyPatch,
    option_name: str,
    option_value: str,
) -> None:
    default_client = cast(AsyncOpenAI, object())
    created_client = cast(AsyncOpenAI, object())
    captured_kwargs: dict[str, Any] = {}

    def create_client(**kwargs: Any) -> AsyncOpenAI:
        captured_kwargs.update(kwargs)
        return created_client

    monkeypatch.setattr(_openai_shared, "get_default_openai_client", lambda: default_client)
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", create_client)
    monkeypatch.setattr(openai_provider, "shared_http_client", object)

    provider = OpenAIProvider(**cast(dict[str, Any], {option_name: option_value}))

    assert provider._get_client() is created_client
    assert captured_kwargs[option_name] == option_value


@pytest.mark.parametrize(
    ("option_name", "environment_name"),
    [
        ("api_key", None),
        ("base_url", "OPENAI_BASE_URL"),
        ("websocket_base_url", "OPENAI_WEBSOCKET_BASE_URL"),
    ],
)
def test_openai_provider_preserves_explicit_empty_options(
    monkeypatch: pytest.MonkeyPatch,
    option_name: str,
    environment_name: str | None,
) -> None:
    default_client = cast(AsyncOpenAI, object())
    created_client = cast(AsyncOpenAI, object())
    captured_kwargs: dict[str, Any] = {}

    def create_client(**kwargs: Any) -> AsyncOpenAI:
        captured_kwargs.update(kwargs)
        return created_client

    monkeypatch.setattr(_openai_shared, "get_default_openai_client", lambda: default_client)
    monkeypatch.setattr(_openai_shared, "get_default_openai_key", lambda: "sk-global")
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", create_client)
    monkeypatch.setattr(openai_provider, "shared_http_client", object)
    if environment_name is not None:
        monkeypatch.setenv(environment_name, "https://global.example.test/v1")

    provider = OpenAIProvider(**cast(dict[str, Any], {option_name: ""}))

    assert provider._get_client() is created_client
    assert captured_kwargs[option_name] == ""

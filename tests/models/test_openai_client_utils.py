from __future__ import annotations

import pytest

from agents.models.openai_client_utils import (
    is_official_openai_base_url,
    is_official_openai_client,
)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com",
        "https://api.openai.com/v1/",
    ],
)
def test_official_openai_base_url_matches_exact_host(base_url: str) -> None:
    assert is_official_openai_base_url(base_url) is True


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com.evil/v1/",
        "https://api.openai.com.proxy.local/v1/",
        "http://api.openai.com/v1/",
        "https://custom.example.test/v1/",
    ],
)
def test_official_openai_base_url_rejects_non_openai_hosts(base_url: str) -> None:
    assert is_official_openai_base_url(base_url) is False


def test_official_openai_websocket_base_url_matches_exact_host() -> None:
    assert is_official_openai_base_url("wss://api.openai.com/v1/", websocket=True) is True
    assert (
        is_official_openai_base_url("wss://api.openai.com.proxy.local/v1/", websocket=True) is False
    )


def test_official_openai_client_rejects_client_without_base_url() -> None:
    assert is_official_openai_client(object()) is False  # type: ignore[arg-type]

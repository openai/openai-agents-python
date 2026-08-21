from typing import cast
from unittest.mock import MagicMock

import httpx2
from openai import NOT_GIVEN, AsyncOpenAI, omit

from agents.voice.models.openai_stt import (
    _prepare_websocket_headers,
    _prepare_websocket_url,
)


def _mock_client(**attributes: object) -> AsyncOpenAI:
    attributes.setdefault("default_query", {})
    return cast(AsyncOpenAI, MagicMock(**attributes))


def test_streaming_stt_websocket_url_uses_client_base_url() -> None:
    client = _mock_client(
        websocket_base_url=None,
        base_url=httpx2.URL("https://voice-proxy.example.test/v1/"),
    )

    url = httpx2.URL(_prepare_websocket_url(client))

    assert url.scheme == "wss"
    assert url.host == "voice-proxy.example.test"
    assert url.path == "/v1/realtime"
    assert url.params["intent"] == "transcription"


def test_streaming_stt_websocket_url_prefers_websocket_base_url() -> None:
    client = _mock_client(
        websocket_base_url="https://voice-ws.example.test/custom/?tenant=one",
        base_url=httpx2.URL("https://ignored.example.test/v1/"),
    )

    url = httpx2.URL(_prepare_websocket_url(client))

    assert url.scheme == "wss"
    assert url.host == "voice-ws.example.test"
    assert url.path == "/custom/realtime"
    assert url.params["tenant"] == "one"
    assert url.params["intent"] == "transcription"


def test_streaming_stt_websocket_url_merges_client_default_query() -> None:
    client = _mock_client(
        websocket_base_url="wss://voice-ws.example.test/custom/?tenant=one&remove=base",
        base_url=httpx2.URL("https://ignored.example.test/v1/"),
        default_query={
            "api-version": "2026-08-01-preview",
            "remove": omit,
            "skip": NOT_GIVEN,
        },
    )

    url = httpx2.URL(_prepare_websocket_url(client))

    assert url.params["tenant"] == "one"
    assert url.params["api-version"] == "2026-08-01-preview"
    assert url.params["intent"] == "transcription"
    assert "remove" not in url.params
    assert "skip" not in url.params


def test_streaming_stt_websocket_headers_use_client_configuration() -> None:
    client = _mock_client(
        auth_headers={"Authorization": "Bearer sk-client"},
        default_headers={
            "OpenAI-Organization": "org-client",
            "OpenAI-Project": "proj-client",
            "X-Proxy-Token": "proxy-token",
        },
    )

    headers = _prepare_websocket_headers(client)

    assert headers["Authorization"] == "Bearer sk-client"
    assert headers["OpenAI-Organization"] == "org-client"
    assert headers["OpenAI-Project"] == "proj-client"
    assert headers["X-Proxy-Token"] == "proxy-token"
    assert headers["OpenAI-Log-Session"] == "1"


def test_streaming_stt_websocket_headers_skip_openai_omission_sentinels() -> None:
    client = _mock_client(
        auth_headers={"Authorization": "Bearer sk-client"},
        default_headers={
            "OpenAI-Organization": omit,
            "OpenAI-Project": NOT_GIVEN,
            "X-Proxy-Token": "proxy-token",
        },
    )

    headers = _prepare_websocket_headers(client)

    assert headers["Authorization"] == "Bearer sk-client"
    assert headers["X-Proxy-Token"] == "proxy-token"
    assert "OpenAI-Organization" not in headers
    assert "OpenAI-Project" not in headers
    assert headers["OpenAI-Log-Session"] == "1"


def test_streaming_stt_websocket_headers_omit_removes_inherited_header() -> None:
    client = _mock_client(
        auth_headers={"Authorization": "Bearer sk-client"},
        default_headers={"authorization": omit},
    )

    headers = _prepare_websocket_headers(client)

    assert all(key.lower() != "authorization" for key in headers)
    assert headers["OpenAI-Log-Session"] == "1"


def test_streaming_stt_websocket_fixed_session_header_replaces_client_casing() -> None:
    client = _mock_client(
        auth_headers={},
        default_headers={"openai-log-session": "0"},
    )

    headers = _prepare_websocket_headers(client)

    session_headers = {
        key: value for key, value in headers.items() if key.lower() == "openai-log-session"
    }
    assert session_headers == {"OpenAI-Log-Session": "1"}

from typing import cast
from unittest.mock import MagicMock

import httpx2
from openai import AsyncOpenAI

from agents.voice.models.openai_stt import (
    _prepare_websocket_headers,
    _prepare_websocket_url,
)


def _mock_client(**attributes: object) -> AsyncOpenAI:
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

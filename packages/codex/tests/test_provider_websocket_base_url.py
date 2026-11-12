"""Separate endpoint declarations preserve existing transport authority."""

from copy import copy, deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from _agents_codex.session import _opening_provider_config
from agents import CodexLogin, NativeProvider, SignInWithChatGPT


async def config(provider):
    result = await _opening_provider_config(
        provider,
        SimpleNamespace(),
        captured_api_key="synthetic",
        captured_siwc_provider=None,
        saved_plan_provider=None,
        standalone_web_search=False,
    )
    return result["model_providers.native_agent"]


@pytest.mark.parametrize("scheme", ["ws", "wss", "http", "https"])
async def test_separate_endpoint_captures_and_forwards_supported_aliases(scheme):
    endpoint = f"{scheme}://socket.example.invalid:8443/custom%23path?route=synthetic%23query"
    provider = NativeProvider(websocket_base_url=endpoint, api_key="synthetic")
    for captured in (provider, copy(provider), deepcopy(provider), replace(provider)):
        assert captured.websocket_base_url == endpoint
        assert asdict(captured)["websocket_base_url"] == endpoint
        assert "socket.example.invalid" not in repr(captured)
        assert (await config(captured))["websocket_base_url"] == endpoint
        assert (await config(captured))["base_url"] == "https://api.openai.com/v1"


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        " ",
        "/relative",
        "wss:relative",
        "ftp://socket.example.invalid",
        "wss:///missing-host",
        "wss://user@socket.example.invalid",
        "wss://user:secret@socket.example.invalid",
        "wss://socket.example.invalid/path#fragment",
        "wss://socket.example.invalid/path#",
        "ws://127.0.0.1/#",
        "wss://socket.example.invalid:invalid",
        "wss://socket.example.invalid:65536",
        "wss://[invalid",
        "wss://socket.example.invalid/\npath",
        "wss://socket .example.invalid",
    ],
)
def test_separate_endpoint_rejects_invalid_urls(endpoint):
    with pytest.raises(ValueError, match="websocket_base_url"):
        NativeProvider(websocket_base_url=endpoint)


@pytest.mark.parametrize("permission", [None, False, True])
async def test_both_endpoints_preserve_explicit_http_base_transport_selection(permission):
    provider = NativeProvider(
        base_url="https://http.example.invalid/v1",
        websocket_base_url="wss://socket.example.invalid/v1",
        supports_websockets=permission,
    )
    actual = await config(provider)
    assert actual["base_url"] == "https://http.example.invalid/v1"
    assert actual["websocket_base_url"] == "wss://socket.example.invalid/v1"
    assert actual["supports_websockets"] is (permission is True)


async def test_separate_endpoint_does_not_enable_websockets_for_buffered_http():
    provider = NativeProvider(
        base_url="https://http.example.invalid/v1",
        websocket_base_url="wss://socket.example.invalid/v1",
        response_mode="buffered",
    )
    assert (await config(provider))["supports_websockets"] is False


async def test_omission_preserves_existing_config_and_default_transport():
    provider = NativeProvider()
    actual = await config(provider)
    assert "websocket_base_url" not in actual
    assert actual["supports_websockets"] is True


@pytest.mark.parametrize("plan", ["codex", "sign-in"])
def test_plan_authentication_refuses_custom_websocket_endpoint_before_credentials(plan):
    calls = []

    async def credentials():
        calls.append("credentials")
        return None

    auth = CodexLogin() if plan == "codex" else SignInWithChatGPT(get_credentials=credentials)
    with pytest.raises(ValueError, match="requires HTTP and its own endpoint"):
        NativeProvider(auth=auth, websocket_base_url="wss://socket.example.invalid")
    assert calls == []

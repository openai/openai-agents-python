"""Captured HTTP fields; real peer delivery belongs to the native MCP tests."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from agents import ChatGPTAppsMCP, HostedVerificationMCP, HttpMCP


def test_http_headers_capture_and_preserve_current_selection() -> None:
    supplied = {"X-Tenant": "header-secret", "X-Empty": "", "X-Label": "café\tvalue"}
    server = HttpMCP(
        name="records", tools=["read"], url="https://example.invalid/mcp", headers=supplied
    )
    supplied["X-Tenant"] = "later"
    assert server.headers is not None
    assert server.headers["X-Tenant"] == "header-secret"
    with pytest.raises(TypeError):
        server.headers["X-Tenant"] = "changed"  # type: ignore[index]
    first: Any = server._config()
    first["http_headers"]["X-Tenant"] = "changed"
    narrowed = replace(server, tools=[])
    assert narrowed._config()["http_headers"] == {
        "X-Tenant": "header-secret",
        "X-Empty": "",
        "X-Label": "café\tvalue",
    }
    assert "header-secret" not in repr(server)
    assert inspect.get_annotations(HttpMCP, eval_str=True)["headers"] == Mapping[str, str] | None


@pytest.mark.parametrize(
    "headers",
    [
        [],
        1,
        {"": "header-secret"},
        {"bad name": "header-secret"},
        {"é": "header-secret"},
        {1: "header-secret"},
        {"X-Value": 1},
        {"X-Value": "header-secret\r\ninjected"},
        {"X-Value": "header-secret\x7f"},
        {"X-Value": "header-secret\x00"},
        {"X-Value": "header-secret\ud800"},
        {"X-Tenant": "header-secret", "x-tenant": "another"},
    ],
)
def test_http_headers_reject_invalid_maps_without_echoing_values(headers: Any) -> None:
    with pytest.raises((TypeError, ValueError)) as error:
        HttpMCP(name="records", tools=[], url="https://example.invalid/mcp", headers=headers)
    assert "header-secret" not in str(error.value)


def test_http_headers_authentication_is_unambiguous() -> None:
    options: Any = {"name": "records", "tools": [], "url": "https://example.invalid/mcp"}
    server = HttpMCP(**options, bearer_token="token", headers={"X-Tenant": "tenant"})
    assert server._config()["http_headers"] == {
        "X-Tenant": "tenant",
        "Authorization": "Bearer token",
    }
    assert HttpMCP(**options, headers={"authorization": "Basic value"})._config()[
        "http_headers"
    ] == {"authorization": "Basic value"}
    for name in ["Authorization", "authorization", "AUTHORIZATION"]:
        with pytest.raises(ValueError, match="either bearer_token"):
            HttpMCP(**options, bearer_token="token", headers={name: "header-secret"})
    assert "http_headers" not in HttpMCP(**options, headers={})._config()


def test_chatgpt_apps_keeps_fixed_authentication() -> None:
    assert "headers" not in inspect.signature(ChatGPTAppsMCP).parameters
    with pytest.raises(TypeError):
        ChatGPTAppsMCP(
            tools=[], access_token="token", account_id="account", headers={"X-Tenant": "secret"}
        )  # type: ignore[call-arg]
    hosted = HostedVerificationMCP(
        tools=[], url="https://example.invalid/mcp", headers={"X-Tenant": "tenant"}
    )
    assert hosted._config()["http_headers"] == {"X-Tenant": "tenant"}

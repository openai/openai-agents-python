from __future__ import annotations

import json
from collections import deque
from typing import Any, Callable

import pytest

from agents.sandbox.app_server.client import (
    AppServerClient,
    AppServerConfig,
    AppServerTransportOps,
)
from agents.sandbox.app_server.models import InitializeResponse, UnknownNotification


class _FakeConnection:
    def __init__(self, responses: list[str | Callable[[list[dict[str, object]]], str]]) -> None:
        self._responses = deque(responses)
        self.sent_payloads: list[dict[str, object]] = []
        self.closed = False

    def send(self, message: str, text: bool | None = None) -> None:
        assert text is True
        self.sent_payloads.append(json.loads(message))

    def recv(self, timeout: float | None = None) -> str:
        assert timeout is None or timeout >= 0
        response = self._responses.popleft()
        if callable(response):
            return response(self.sent_payloads)
        return response

    def close(self) -> None:
        self.closed = True


def _initialize_result(sent_payloads: list[dict[str, object]]) -> str:
    request = next(payload for payload in sent_payloads if payload.get("method") == "initialize")
    return json.dumps(
        {
            "id": request["id"],
            "result": {
                "serverInfo": {"name": "codex-app-server", "version": "2"},
                "platformOs": "linux",
            },
        }
    )


def test_app_server_client_initializes_over_explicit_websocket_url() -> None:
    fake_connection = _FakeConnection([_initialize_result])
    captured_connect: dict[str, object] = {}

    def _connect(url: str, **kwargs: object) -> Any:
        captured_connect["url"] = url
        captured_connect["kwargs"] = kwargs
        return fake_connection

    client = AppServerClient(
        AppServerConfig(
            websocket_url="ws://sandbox.example.test:4500/",
            websocket_headers={"Authorization": "Bearer test"},
        ),
        transport_ops=AppServerTransportOps(ws_connect=_connect),
    )

    try:
        client.start()
        response = client.initialize()
    finally:
        client.close()

    assert captured_connect["url"] == "ws://sandbox.example.test:4500/"
    assert captured_connect["kwargs"] == {
        "additional_headers": {"Authorization": "Bearer test"},
        "open_timeout": 10.0,
        "max_size": None,
    }
    assert isinstance(response, InitializeResponse)
    assert response.serverInfo is not None
    assert response.serverInfo.name == "codex-app-server"
    assert response.platformOs == "linux"
    assert fake_connection.sent_payloads[0]["method"] == "initialize"
    assert fake_connection.sent_payloads[1] == {"method": "initialized", "params": {}}
    assert fake_connection.closed is True


def test_app_server_client_handles_server_requests_and_queues_notifications() -> None:
    def _initialize_with_notification(sent_payloads: list[dict[str, object]]) -> str:
        request = next(
            payload for payload in sent_payloads if payload.get("method") == "initialize"
        )
        return json.dumps({"id": request["id"], "result": {"platformOs": "linux"}})

    fake_connection = _FakeConnection(
        [
            json.dumps(
                {
                    "id": "server-approval-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "ls"},
                }
            ),
            json.dumps({"method": "custom/notice", "params": {"seen": True}}),
            _initialize_with_notification,
        ]
    )

    def _connect(url: str, **kwargs: object) -> Any:
        return fake_connection

    client = AppServerClient(
        AppServerConfig(websocket_url="ws://sandbox.example.test:4500/"),
        transport_ops=AppServerTransportOps(ws_connect=_connect),
    )

    try:
        client.start()
        response = client.initialize()
        notification = client.next_notification()
    finally:
        client.close()

    assert response.platformOs == "linux"
    assert fake_connection.sent_payloads[1] == {
        "id": "server-approval-1",
        "result": {"decision": "accept"},
    }
    assert notification.method == "custom/notice"
    assert isinstance(notification.payload, UnknownNotification)
    assert notification.payload.params == {"seen": True}


def test_app_server_client_requires_websocket_url() -> None:
    def _unused_connect(url: str, **kwargs: object) -> Any:
        raise AssertionError("ws_connect should not be called without a websocket_url")

    client = AppServerClient(
        AppServerConfig(),
        transport_ops=AppServerTransportOps(ws_connect=_unused_connect),
    )

    with pytest.raises(ValueError, match="websocket_url is required"):
        client.start()

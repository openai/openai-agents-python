"""Bound unauthenticated SIP webhooks before signature verification and call effects."""

import base64
import hashlib
import hmac
import importlib
import json
import sys
import time
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest

pytest.importorskip("fastapi", reason="optional: Classic Twilio SIP example requirements")

SECRET = "synthetic-sip-webhook-secret"
BUDGET = 64 * 1024
MODULE = "examples.classic.realtime.twilio_sip.server"


@pytest.fixture
async def server(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-sip-key")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "accept_call", AsyncMock())
    monkeypatch.setattr(module, "_track_call_task", Mock())
    unwrap = Mock(wraps=module.client.webhooks.unwrap)
    monkeypatch.setattr(module.client.webhooks, "unwrap", unwrap)
    try:
        yield module
    finally:
        await module.client.close()
        sys.modules.pop(MODULE, None)


def signed_headers(payload):
    timestamp = str(int(time.time()))
    event_id = "evt_synthetic_sip"
    signature = base64.b64encode(
        hmac.new(
            SECRET.encode(),
            f"{event_id}.{timestamp}.".encode() + payload,
            hashlib.sha256,
        ).digest()
    ).decode()
    return {
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
        "content-type": "application/json",
    }


def incoming_event():
    return json.dumps(
        {
            "id": "evt_synthetic_sip",
            "type": "realtime.call.incoming",
            "created_at": int(time.time()),
            "data": {"call_id": "call_synthetic", "sip_headers": []},
        }
    ).encode()


@pytest.mark.parametrize("at_limit", [False, True])
async def test_signed_streamed_webhook_preserves_call_handling(server, at_limit):
    payload = incoming_event()
    if at_limit:
        payload += b" " * (BUDGET - len(payload))

    async def chunks():
        yield payload[:13]
        yield payload[13:]

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=server.app), base_url="http://sip.test"
    ) as client:
        response = await client.post(
            "/openai/webhook", content=chunks(), headers=signed_headers(payload)
        )
    assert response.status_code == 200
    assert "content-length" not in response.request.headers
    assert server.client.webhooks.unwrap.call_args.args[0] == payload
    server.accept_call.assert_awaited_once_with("call_synthetic")
    server._track_call_task.assert_called_once_with("call_synthetic")


@pytest.mark.parametrize("parts", [(BUDGET, 1, BUDGET), (BUDGET + 1, BUDGET)])
async def test_oversized_stream_stops_before_verification_and_call_effects(server, parts):
    received = 0

    async def chunks():
        nonlocal received
        for size in parts:
            received += 1
            yield b"x" * size

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=server.app), base_url="http://sip.test"
    ) as client:
        response = await client.post("/openai/webhook", content=chunks())
    assert response.status_code == 413
    assert "content-length" not in response.request.headers
    assert received < len(parts)
    server.client.webhooks.unwrap.assert_not_called()
    server.accept_call.assert_not_awaited()
    server._track_call_task.assert_not_called()


async def test_small_invalid_signature_still_rejects_before_call_effects(server):
    payload = incoming_event()
    headers = signed_headers(payload)
    headers["webhook-signature"] = "v1,invalid"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=server.app), base_url="http://sip.test"
    ) as client:
        response = await client.post("/openai/webhook", content=payload, headers=headers)
    assert response.status_code == 400
    server.client.webhooks.unwrap.assert_called_once()
    server.accept_call.assert_not_awaited()
    server._track_call_task.assert_not_called()

"""Public connection observations retain no URL credentials or header values."""

import json
from dataclasses import asdict

import pytest

from agents.realtime import RealtimeAdapterContext
from agents.testing import RealtimeConnectCall, ScriptedRealtimeAdapter


@pytest.mark.parametrize(
    "headers,expected",
    [({}, False), ({"X-Test": "header-sentinel"}, False), ({"aUtHoRiZaTiOn": ""}, True)],
)
async def test_connection_snapshot_records_endpoint_and_auth_presence(headers, expected):
    adapter = ScriptedRealtimeAdapter()
    context = RealtimeAdapterContext(
        url="wss://user-sentinel:password-sentinel@fixture.invalid:8443/custom?token=query-sentinel&model=ignored#fragment-sentinel",
        headers=headers,
        model="selected",
    )
    try:
        assert await adapter.connect(context) is adapter
        snapshot = adapter.connect_calls[0]
        assert snapshot.url == "wss://fixture.invalid:8443/custom"
        assert snapshot.api_key_provided is expected
        assert snapshot.headers_provided is bool(headers)
        assert snapshot.model == "selected"
        headers.clear()
        assert adapter.connect_calls[0] == snapshot
        encoded = json.dumps(asdict(snapshot))
        assert "sentinel" not in encoded
        assert context.url.endswith("#fragment-sentinel")
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://fixture.invalid:443", "https://fixture.invalid/"),
        ("http://fixture.invalid/path?secret=value", "http://fixture.invalid/path"),
        ("ws://[::1]:8080/custom", "ws://[::1]:8080/custom"),
        ("relative/endpoint?secret=value", None),
        ("wss://fixture.invalid:invalid/path", None),
        ("wss://fixture.invalid:65536/path", None),
        ("custom:opaque?secret=value", None),
    ],
)
async def test_endpoint_redaction_is_observation_only(url, expected):
    adapter = ScriptedRealtimeAdapter()
    try:
        await adapter.connect(RealtimeAdapterContext(url=url, headers={}))
        assert adapter.connect_calls[0].url == expected
    finally:
        await adapter.aclose()


def test_existing_connection_call_constructor_remains_supported():
    call = RealtimeConnectCall("selected", False)
    assert call.api_key_provided is False
    assert call.url is None

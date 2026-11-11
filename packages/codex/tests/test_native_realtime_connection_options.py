"""Per-connection WebSocket routing and replacement headers on the native wire."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig, RealtimeSIP, RealtimeWebRTC
from tests._asyncio import timeout
from tests.native_fixtures import text_response
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer
from tests.test_native_realtime_transports import (
    AUTHORIZATION,
    OFFER_SDP,
    CallPeer,
    call_peer as call_peer,
)


@pytest_asyncio.fixture(loop_scope="function")
async def connection_endpoint(
    realtime_peer: RealtimePeer,
) -> AsyncIterator[tuple[str, list[str]]]:
    paths: list[str] = []

    async def connect(request: web.Request) -> web.WebSocketResponse:
        paths.append(str(request.rel_url))
        return await realtime_peer.websocket(request)

    app = web.Application()
    app.router.add_get("/trusted/voice", connect)
    async with TestServer(app, shutdown_timeout=1) as server:
        url = str(server.make_url("/trusted/voice?model=fixture-realtime&token=synthetic-query"))
        yield url.replace("http://", "ws://", 1), paths


@pytest.mark.parametrize(
    ("headers", "api_key", "expected_authorization"),
    [
        (None, None, None),
        (None, "explicit-voice-key", "Bearer explicit-voice-key"),
        ({}, "discarded-voice-key", None),
        ({"X-Voice": "valid\tvalue"}, None, None),
        (
            {"Authorization": "Bearer custom-header-key", "X-Voice": "custom"},
            None,
            "Bearer custom-header-key",
        ),
    ],
)
async def test_connection_url_is_exact_and_never_inherits_session_credentials(
    realtime_peer: RealtimePeer,
    connection_endpoint: tuple[str, list[str]],
    headers: dict[str, str] | None,
    api_key: str | None,
    expected_authorization: str | None,
) -> None:
    peer = realtime_peer
    url, paths = connection_endpoint
    peer.responses.respond(*text_response("ordinary-auth", "Session auth survived."))
    config = RealtimeConfig(model="fixture-realtime", url=url, headers=headers, api_key=api_key)
    assert "synthetic-query" not in repr(config)
    assert "custom-header-key" not in repr(config)
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="private-session-key",
            headers={"X-Provider-Secret": "private-provider-header"},
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(config=config) as voice:
            await voice.send_audio(b"\0\0" * 240)
            await peer.frame(lambda frame: frame["type"] == "input_audio_buffer.append")
            await voice.send_text("Use the overridden transport.")
        assert paths == ["/trusted/voice?model=fixture-realtime&token=synthetic-query"]
        wire = peer.upgrades[0]
        assert wire.get("authorization") == expected_authorization
        assert "x-provider-secret" not in wire
        if headers is None:
            assert wire.get("x-session-id")
            assert wire.get("session-id")
            assert wire.get("thread-id")
            assert wire.get("originator")
            assert wire.get("user-agent")
        else:
            for name in ("x-session-id", "session-id", "thread-id", "originator", "user-agent"):
                assert name not in wire
        assert "x-codex-turn-metadata" not in wire
        assert wire.get("x-voice") == (headers or {}).get("X-Voice")
        turn = await session.submit("Use the ordinary Session endpoint.")
        assert (await turn.get_result()).final_output == "Session auth survived."
        assert peer.responses.request_headers[0]["authorization"] == "Bearer private-session-key"
        assert peer.responses.request_headers[0]["x-provider-secret"] == "private-provider-header"


@pytest.mark.parametrize("headers", [{}, {"X-Voice": "replacement"}])
async def test_replacement_headers_apply_without_url_override(
    realtime_peer: RealtimePeer, headers: dict[str, str]
) -> None:
    peer = realtime_peer
    async with (
        timeout(30),
        NativeProvider(base_url=peer.responses.url, api_key="private-session-key") as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", headers=headers)
        ):
            assert "authorization" not in peer.upgrades[0]
            assert peer.upgrades[0].get("x-voice") == headers.get("X-Voice")


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer replacement-header-key"}])
async def test_replacement_headers_skip_unused_credential_callback(
    realtime_peer: RealtimePeer, headers: dict[str, str]
) -> None:
    peer = realtime_peer
    calls = 0

    def credential() -> str:
        nonlocal calls
        calls += 1
        raise LookupError("Unused credential source must not run")

    async with (
        timeout(30),
        NativeProvider(base_url=peer.responses.url, api_key="session-key") as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", headers=headers, api_key=credential)
        ) as voice:
            await voice.send_text("Use only the replacement headers.")
        assert calls == 0
        assert peer.upgrades[0].get("authorization") == headers.get("Authorization")


async def test_url_without_replacement_headers_resolves_credential_callback(
    realtime_peer: RealtimePeer, connection_endpoint: tuple[str, list[str]]
) -> None:
    peer = realtime_peer
    calls = 0

    def credential() -> str:
        nonlocal calls
        calls += 1
        return "resolved-voice-key"

    async with (
        timeout(30),
        NativeProvider(base_url=peer.responses.url, api_key="session-key") as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime", url=connection_endpoint[0], api_key=credential
            )
        ):
            assert calls == 1
            assert peer.upgrades[0]["authorization"] == "Bearer resolved-voice-key"


async def test_existing_call_accepts_exact_url_and_replacement_headers(call_peer: CallPeer) -> None:
    peer = call_peer
    calls = 0

    def credential() -> str:
        nonlocal calls
        calls += 1
        raise LookupError("Unused attachment credential source must not run")

    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP))):
            pass
        peer.sessions["rtc_external_sip"] = peer.sessions["rtc_native_1"]
        url = (
            peer.responses.url.replace("http://", "ws://", 1) + "/realtime?call_id=rtc_external_sip"
        )
        async with session.realtime(
            config=RealtimeConfig(
                transport=RealtimeSIP("rtc_external_sip"),
                url=url,
                headers={"Authorization": AUTHORIZATION, "X-Voice": "attachment"},
                api_key=credential,
            )
        ) as voice:
            assert voice.call_id == "rtc_external_sip"
        assert calls == 0
        assert peer.sidebands[-1][1]["x-voice"] == "attachment"
        assert [call_id for call_id, _ in peer.hangups] == ["rtc_native_1"]


@pytest.mark.parametrize("api_key", [None, "explicit-sideband-key"])
async def test_existing_call_url_only_retains_handshake_without_provider_credentials(
    call_peer: CallPeer, api_key: str | None
) -> None:
    peer = call_peer
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            headers={"X-Provider-Secret": "private-provider-header"},
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP))):
            pass
        normal_headers = peer.sidebands[0][1]
        peer.sessions["rtc_external_sip"] = peer.sessions["rtc_native_1"]
        peer.expected_sideband_authorization = f"Bearer {api_key}" if api_key else None
        url = (
            peer.responses.url.replace("http://", "ws://", 1) + "/realtime?call_id=rtc_external_sip"
        )
        async with session.realtime(
            config=RealtimeConfig(
                transport=RealtimeSIP("rtc_external_sip"), url=url, api_key=api_key
            )
        ):
            headers = peer.sidebands[-1][1]
            assert headers.get("authorization") == peer.expected_sideband_authorization
            for name in ("session-id", "thread-id", "originator", "user-agent"):
                assert headers.get(name) == normal_headers[name]
            assert "x-provider-secret" not in headers
            assert "x-codex-turn-metadata" not in headers
        assert [call_id for call_id, _ in peer.hangups] == ["rtc_native_1"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/voice",
        "ws://user:password@example.test/voice",
        "ws://example.test/#secret",
        "ws://example.test/voice#",
        "ws://example.test:invalid/voice",
        "ws://example.test:65536/voice",
        "ws://bad host/voice",
        "ws://bad%20host/voice",
        "ws://256.256.0.1/voice",
    ],
)
async def test_invalid_url_rejects_before_resolving_credentials(
    realtime_peer: RealtimePeer, url: str
) -> None:
    def credential() -> str:
        pytest.fail("Invalid URL must reject before credential resolution")

    async with (
        NativeProvider(base_url=realtime_peer.responses.url, api_key="session-key") as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        with pytest.raises(ValueError, match="ws or wss") as error:
            async with session.realtime(config=RealtimeConfig(url=url, api_key=credential)):
                pytest.fail("Invalid URL must reject before transport I/O")
        assert url not in str(error.value)
        assert error.value.__context__ is None
        assert not realtime_peer.upgrades


@pytest.mark.parametrize(
    "headers",
    [
        {"invalid name": "value"},
        {"X-Voice": "private\nvalue"},
        {"X-Voice": "private\x7fvalue"},
        {"X-Voice": "private-café-value"},
    ],
)
async def test_invalid_headers_reject_without_sending_or_exposing_values(
    realtime_peer: RealtimePeer, headers: dict[str, str]
) -> None:
    calls = 0

    def credential() -> str:
        nonlocal calls
        calls += 1
        return "private-callback-key"

    async with (
        timeout(30),
        NativeProvider(base_url=realtime_peer.responses.url, api_key="session-key") as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        with pytest.raises(ValueError, match="Invalid Realtime header") as error:
            async with session.realtime(config=RealtimeConfig(headers=headers, api_key=credential)):
                pytest.fail("Invalid headers must reject before transport I/O")
        assert "private" not in str(error.value)
        assert calls == 0
        assert not realtime_peer.upgrades


async def test_redirect_never_forwards_explicit_or_session_credentials(
    realtime_peer: RealtimePeer, connection_endpoint: tuple[str, list[str]]
) -> None:
    destination, paths = connection_endpoint
    received: list[dict[str, str]] = []

    async def redirect(request: web.Request) -> web.Response:
        received.append(dict(request.headers))
        return web.Response(status=302, headers={"Location": destination})

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    async with (
        TestServer(app) as server,
        NativeProvider(base_url=realtime_peer.responses.url, api_key="session-key") as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        with pytest.raises(RuntimeError):
            async with session.realtime(
                config=RealtimeConfig(
                    url=str(server.make_url("/redirect")).replace("http://", "ws://", 1),
                    api_key="explicit-voice-key",
                )
            ):
                pytest.fail("Redirect must not publish a usable connection")
        assert received[0]["Authorization"] == "Bearer explicit-voice-key"
        assert paths == []
        assert not realtime_peer.upgrades


async def test_webrtc_rejects_websocket_connection_options_before_transport_io(
    realtime_peer: RealtimePeer,
) -> None:
    async with (
        NativeProvider(base_url=realtime_peer.responses.url, api_key="session-key") as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        with pytest.raises(ValueError, match="built-in WebSocket"):
            session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP), headers={}))
        assert not realtime_peer.upgrades

"""Public native WebRTC and existing-call lifetime against local provider protocols."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider, PromptedOutput
from agents.realtime import RealtimeConfig, RealtimeSIP, RealtimeWebRTC
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, text_response

OFFER_SDP = "v=0\r\ns=synthetic-browser-offer\r\n"
ANSWER_SDP = "v=0\r\ns=synthetic-provider-answer\r\n"
AUTHORIZATION = "Bearer synthetic-native-call-key"


@dataclass
class CallPeer:
    """Script the provider wire while public SDK owners run the actual native binding."""

    responses: ResponsesFixture = field(default_factory=ResponsesFixture)
    offers: list[str] = field(default_factory=list)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    create_headers: list[dict[str, str]] = field(default_factory=list)
    sidebands: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    hangups: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)
    sequence: list[str] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    receipt_release: asyncio.Event = field(default_factory=asyncio.Event)
    sideband_connected: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    sideband_status: int = 101
    hangup_allowed: bool = True
    socket: web.WebSocketResponse | None = None

    async def create(self, request: web.Request) -> web.Response:
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert headers["authorization"] == AUTHORIZATION
        assert "x-sdk-sentinel-grant" not in headers
        assert not request.query
        parts = await request.multipart()
        payload: dict[str, str] = {}
        async for part in parts:
            assert part.name is not None
            assert part.name not in payload
            payload[part.name] = await part.text()
        assert set(payload) == {"sdp", "session"}
        call_id = f"rtc_native_{len(self.offers) + 1}"
        self.offers.append(payload["sdp"])
        self.sessions[call_id] = json.loads(payload["session"])
        self.create_headers.append(headers)
        self.sequence.append("create")
        return web.Response(
            text=ANSWER_SDP,
            content_type="application/sdp",
            headers={"Location": f"/v1/realtime/calls/{call_id}"},
        )

    async def sideband(self, request: web.Request) -> web.StreamResponse:
        call_id = request.query["call_id"]
        assert call_id in self.sessions
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert headers["authorization"] == AUTHORIZATION
        assert "x-sdk-sentinel-grant" not in headers
        self.sidebands.append((call_id, headers))
        self.sequence.append("sideband")
        if self.sideband_status != 101:
            return web.json_response(
                {"error": {"message": "synthetic sideband refused"}},
                status=self.sideband_status,
            )
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.socket = socket
        self.disconnected.clear()
        self.sideband_connected.set()
        try:
            await self.receipt_release.wait()
            await socket.send_json(
                {
                    "type": "session.created",
                    "session": {**self.sessions[call_id], "id": f"session-{call_id}"},
                }
            )
            self.sequence.append("ready")
            async for message in socket:
                assert message.type == WSMsgType.TEXT
                async with self.changed:
                    self.frames.append(json.loads(message.data))
                    self.changed.notify_all()
        finally:
            self.socket = None
            self.disconnected.set()
        return socket

    async def frame(
        self, predicate: Callable[[dict[str, Any]], bool], *, after: int = 0
    ) -> dict[str, Any]:
        async with self.changed:
            await self.changed.wait_for(
                lambda: any(predicate(frame) for frame in self.frames[after:])
            )
            return next(frame for frame in self.frames[after:] if predicate(frame))

    async def hangup(self, request: web.Request) -> web.Response:
        headers = {key.lower(): value for key, value in request.headers.items()}
        assert headers["authorization"] == AUTHORIZATION
        assert "x-sdk-sentinel-grant" not in headers
        self.hangups.append((request.match_info["call_id"], headers))
        self.sequence.append("hangup")
        if not self.hangup_allowed:
            return web.json_response(
                {"error": {"message": "synthetic cleanup temporarily unavailable"}}, status=409
            )
        return web.Response(status=200)


@pytest_asyncio.fixture(loop_scope="function")
async def call_peer() -> AsyncIterator[CallPeer]:
    peer = CallPeer()
    peer.receipt_release.set()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.responses.handle)
    app.router.add_post("/v1/realtime/calls", peer.create)
    app.router.add_get("/v1/realtime", peer.sideband)
    app.router.add_post("/v1/realtime/calls/{call_id}/hangup", peer.hangup)

    async def close_socket(app: web.Application) -> None:
        peer.receipt_release.set()
        if peer.socket is not None:
            await peer.socket.close()

    app.on_shutdown.append(close_socket)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.responses.url = str(server.make_url("/v1"))
        yield peer


async def test_native_webrtc_sends_initial_config_and_waits_for_sideband_before_answer(
    call_peer: CallPeer,
) -> None:
    peer = call_peer
    peer.receipt_release.clear()
    peer.responses.respond(*text_response("after-webrtc", json.dumps("Caller Session survives.")))
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Use the native caller's profile.",
            output_type=PromptedOutput(str),
            provider=provider,
        ).session() as session,
    ):
        voice = session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                voice="marin",
                instructions="WEBRTC_INITIAL_INSTRUCTIONS",
                settings={
                    "turn_detection": None,
                    "transcription": {"model": "gpt-4o-transcribe", "language": "en"},
                    "noise_reduction": {"type": "near_field"},
                    "output_speed": 1.25,
                    "max_output_tokens": 256,
                    "parallel_tool_calls": False,
                    "prompt": {"id": "pmpt_native_call", "variables": {"topic": "synthetic"}},
                },
                transport=RealtimeWebRTC(offer_sdp=OFFER_SDP),
            )
        )
        entering = asyncio.create_task(voice.__aenter__())
        answer: asyncio.Task[str] | None = None
        try:
            await peer.sideband_connected.wait()
            answer = asyncio.create_task(voice.answer_sdp())
            assert not entering.done()
            assert not answer.done()
            assert peer.sequence == ["create", "sideband"]
            initial = peer.sessions["rtc_native_1"]
            assert peer.offers == [OFFER_SDP]
            assert initial["type"] == "realtime"
            assert initial["model"] == "fixture-realtime"
            assert "WEBRTC_INITIAL_INSTRUCTIONS" in initial["instructions"]
            assert initial["output_modalities"] == ["audio"]
            assert initial["audio"]["input"]["turn_detection"] is None
            assert initial["audio"]["input"]["transcription"] == {
                "model": "gpt-4o-transcribe",
                "language": "en",
            }
            assert initial["audio"]["input"]["noise_reduction"] == {"type": "near_field"}
            assert initial["audio"]["output"]["voice"] == "marin"
            assert initial["audio"]["output"]["speed"] == 1.25
            assert initial["max_output_tokens"] == 256
            assert initial["parallel_tool_calls"] is False
            assert initial["prompt"] == {
                "id": "pmpt_native_call",
                "variables": {"topic": "synthetic"},
            }
            assert any(tool["name"] == "background_agent" for tool in initial["tools"])
            peer.receipt_release.set()
            assert await entering is voice
            assert await answer == ANSWER_SDP
            assert voice.call_id == "rtc_native_1"
            assert await voice.answer_sdp() == ANSWER_SDP
        finally:
            peer.receipt_release.set()
            await asyncio.gather(entering, return_exceptions=True)
            if answer is not None:
                await asyncio.gather(answer, return_exceptions=True)
            await voice.aclose()
        await voice.aclose()
        await peer.disconnected.wait()
        assert [call_id for call_id, _ in peer.hangups] == ["rtc_native_1"]
        assert all(frame["type"] != "session.update" for frame in peer.frames)
        result = await (await session.submit("Continue the caller Session.")).get_result()
        assert result.final_output == "Caller Session survives."


async def test_native_webrtc_sideband_failure_cleans_up_the_created_call(
    call_peer: CallPeer,
) -> None:
    peer = call_peer
    peer.sideband_status = 403
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Native transport fixture.", provider=provider).session() as session,
    ):
        voice = session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP)))
        with pytest.raises(RuntimeError):
            async with voice:
                pytest.fail("A failed sideband must not publish a usable voice owner")
        assert peer.offers == [OFFER_SDP]
        assert [call_id for call_id, _ in peer.hangups] == ["rtc_native_1"]
        assert "ready" not in peer.sequence
        await voice.aclose()
        assert len(peer.hangups) == 1


async def test_native_webrtc_hangup_failure_is_retryable_after_session_graph_close(
    call_peer: CallPeer,
) -> None:
    peer = call_peer
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
    ):
        scope = Agent(
            instructions="Retain call cleanup after graph shutdown.", provider=provider
        ).session()
        session = await scope.__aenter__()
        voice = session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP)))
        try:
            await voice.__aenter__()
            peer.hangup_allowed = False
            with pytest.raises(RuntimeError):
                await session.aclose()
            assert peer.hangups
            assert {call_id for call_id, _ in peer.hangups} == {"rtc_native_1"}
            failed_attempts = len(peer.hangups)
            await peer.disconnected.wait()
            peer.hangup_allowed = True
            await voice.aclose()
            assert len(peer.hangups) == failed_attempts + 1
            await session.aclose()
            await voice.aclose()
            assert len(peer.hangups) == failed_attempts + 1
        finally:
            peer.hangup_allowed = True
            await voice.aclose()
            await session.aclose()
            await scope.__aexit__(None, None, None)


async def test_native_sip_rejects_client_audio_and_detaches_without_hangup(
    call_peer: CallPeer,
) -> None:
    peer = call_peer
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Use one captured native profile.", provider=provider
        ).session() as session,
    ):
        # Capture that profile's default creation config. The application, rather
        # than this SDK, owns accepting the separate incoming SIP call with it.
        async with session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP))):
            pass
        peer.sessions["rtc_external_sip"] = peer.sessions["rtc_native_1"]
        peer.sideband_connected.clear()
        async with session.realtime(
            config=RealtimeConfig(transport=RealtimeSIP(call_id="rtc_external_sip"))
        ) as voice:
            assert voice.call_id == "rtc_external_sip"
            with pytest.raises(RuntimeError, match="SIP calls receive audio from the provider"):
                await voice.send_audio(b"\x00\x00" * 240)
            with pytest.raises(RuntimeError, match="WebRTC"):
                await voice.answer_sdp()
        await peer.disconnected.wait()
        assert len(peer.offers) == 1
        assert [call_id for call_id, _ in peer.sidebands] == ["rtc_native_1", "rtc_external_sip"]
        assert [call_id for call_id, _ in peer.hangups] == ["rtc_native_1"]
        assert all(frame["type"] != "input_audio_buffer.append" for frame in peer.frames)
        assert all(frame["type"] != "session.update" for frame in peer.frames)


@pytest.mark.parametrize("kind", ["ascii", "utf8", "astral", "whitespace"])
async def test_native_webrtc_rejects_oversized_offer_before_credentials(
    call_peer: CallPeer, kind: str
) -> None:
    budget = 1024 * 1024
    unit = {"ascii": "a", "utf8": "é", "astral": "😀", "whitespace": " "}[kind]
    offer = unit * (budget // len(unit.encode("utf-8")) + 1)
    credential_calls = 0

    def credentials() -> str:
        nonlocal credential_calls
        credential_calls += 1
        return "synthetic-native-call-key"

    async with (
        timeout(30),
        NativeProvider(
            base_url=call_peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Answer briefly.", provider=provider).session() as session,
    ):
        with pytest.raises(ValueError, match="offer_sdp.*1048576.*UTF-8"):
            session.realtime(
                config=RealtimeConfig(transport=RealtimeWebRTC(offer), api_key=credentials)
            )
        assert credential_calls == 0
        assert not call_peer.offers
        assert not call_peer.sidebands
        async with session.realtime(
            config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP), api_key=credentials)
        ) as voice:
            assert await voice.answer_sdp() == ANSWER_SDP
        assert credential_calls == 1
        assert call_peer.offers == [OFFER_SDP]


@pytest.mark.parametrize("unit", ["a", "é"], ids=["ascii", "utf8"])
async def test_native_webrtc_accepts_offer_at_utf8_byte_limit(
    call_peer: CallPeer, unit: str
) -> None:
    offer = unit * (1024 * 1024 // len(unit.encode("utf-8")))
    async with (
        timeout(30),
        NativeProvider(
            base_url=call_peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Answer briefly.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(offer))) as voice,
    ):
        assert await voice.answer_sdp() == ANSWER_SDP
        assert call_peer.offers == [offer]


async def test_native_sip_repeats_call_accept_options_without_initial_update(
    call_peer: CallPeer,
) -> None:
    peer = call_peer
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep the accepted call configuration.", provider=provider
        ).session() as session,
    ):
        config = RealtimeConfig(
            model="fixture-realtime",
            voice="marin",
            instructions="Repeat the accepted SIP instructions.",
            tools=(),
        )
        accepted = await session.build_realtime_sip_config(config=config)
        assert accepted["model"] == "fixture-realtime"
        assert accepted["audio"]["output"]["voice"] == "marin"
        assert "Repeat the accepted SIP instructions." in accepted["instructions"]
        peer.sessions["rtc_repeated_sip_options"] = accepted
        async with session.realtime(
            config=replace(config, transport=RealtimeSIP("rtc_repeated_sip_options"))
        ) as voice:
            assert voice.call_id == "rtc_repeated_sip_options"
        await peer.disconnected.wait()
        assert [call_id for call_id, _ in peer.sidebands] == ["rtc_repeated_sip_options"]
        assert peer.offers == []
        assert peer.hangups == []
        assert not any(frame["type"] == "session.update" for frame in peer.frames)


@pytest.mark.parametrize("mismatch", ["model", "voice", "instructions"])
async def test_native_sip_rejects_readiness_configuration_mismatch(
    call_peer: CallPeer, mismatch: str
) -> None:
    peer = call_peer
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Verify accepted SIP configuration.", provider=provider
        ).session() as session,
    ):
        config = RealtimeConfig(
            model="fixture-realtime",
            voice="marin",
            instructions="Expected accepted instructions.",
            tools=(),
        )
        accepted = await session.build_realtime_sip_config(config=config)
        if mismatch == "model":
            accepted["model"] = "different-fixture-realtime"
        elif mismatch == "voice":
            accepted["audio"]["output"]["voice"] = "alloy"
        else:
            accepted["instructions"] = "Different provider-accepted instructions."
        peer.sessions["rtc_mismatched_sip_options"] = accepted
        with pytest.raises(RuntimeError):
            async with session.realtime(
                config=replace(config, transport=RealtimeSIP("rtc_mismatched_sip_options"))
            ):
                pytest.fail("Mismatched provider readiness must not admit the SIP owner")
        await peer.disconnected.wait()
        assert [call_id for call_id, _ in peer.sidebands] == ["rtc_mismatched_sip_options"]
        assert peer.offers == []
        assert peer.hangups == []
        assert not any(frame["type"] == "session.update" for frame in peer.frames)

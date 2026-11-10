"""Actual fake-microphone capture and browser playback with native call ownership."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider

CLIENT = Path(__file__).resolve().parents[3] / "examples" / "codex" / "web_starter"


@pytest.mark.skipif(
    os.environ.get("OPENAI_AGENTS_BROWSER_TESTS") != "1",
    reason="Requires explicit native realtime browser qualification on a sandbox-capable host",
)
async def test_native_webrtc_browser_microphone_and_playback() -> None:
    # This public API belongs to the separately coordinated native voice change.
    from agents.realtime import RealtimeConfig, RealtimeWebRTC

    node = shutil.which("node")
    assert node is not None and (CLIENT / "node_modules/playwright").exists(), (
        "Prepare the web_starter browser dependencies and Node 24"
    )
    voice: Any = None
    session: Any = None
    answer = ""
    accepted: dict[str, Any] = {}
    offers: list[str] = []
    sidebands: list[str] = []
    hangups: list[str] = []
    failures: list[str] = []

    async def index(request: web.Request) -> web.Response:
        return web.Response(
            text="<button>Connect</button><audio autoplay></audio>"
            '<script type="module" src="/client.mjs"></script>',
            content_type="text/html",
        )

    async def client(request: web.Request) -> web.Response:
        return web.FileResponse(CLIENT / "realtime-client.mjs")

    async def exchange(request: web.Request) -> web.Response:
        nonlocal voice, answer
        payload = await request.json()
        answer = payload["answerSdp"]
        voice = session.realtime(
            config=RealtimeConfig(
                model="synthetic-browser-realtime",
                transport=RealtimeWebRTC(payload["offerSdp"]),
            )
        )
        await voice.__aenter__()
        return web.json_response({"answerSdp": await voice.answer_sdp()})

    async def close(request: web.Request) -> web.Response:
        await voice.aclose()
        return web.Response()

    async def create(request: web.Request) -> web.Response:
        nonlocal accepted
        assert request.headers["Authorization"] == "Bearer synthetic-browser-key"
        payload = {part.name: await part.text() async for part in await request.multipart()}
        offers.append(payload["sdp"])
        accepted = json.loads(payload["session"])
        return web.Response(
            text=answer,
            content_type="application/sdp",
            headers={"Location": "/v1/realtime/calls/rtc_browser"},
        )

    async def sideband(request: web.Request) -> web.WebSocketResponse:
        sidebands.append(request.query["call_id"])
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json(
            {"type": "session.created", "session": {**accepted, "id": "session-browser"}}
        )
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                failures.append("Unexpected sideband frame")
                continue
            frame = json.loads(message.data)
            if frame["type"] == "session.update":
                accepted.update(frame["session"])
                await socket.send_json(
                    {"type": "session.updated", "session": {**accepted, "id": "session-browser"}}
                )
        return socket

    async def hangup(request: web.Request) -> web.Response:
        hangups.append("rtc_browser")
        return web.Response()

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/client.mjs", client)
    app.router.add_post("/exchange", exchange)
    app.router.add_post("/close", close)
    app.router.add_post("/v1/realtime/calls", create)
    app.router.add_get("/v1/realtime", sideband)
    app.router.add_post("/v1/realtime/calls/rtc_browser/hangup", hangup)
    async with TestServer(app) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")), api_key="synthetic-browser-key"
        ) as provider:
            agent = Agent(instructions="Answer the microphone.", model="gpt-5.5", provider=provider)
            async with agent.session() as session:
                process = await asyncio.create_subprocess_exec(
                    node,
                    str(CLIENT / "realtime-browser-test.mjs"),
                    str(server.make_url("/")),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
                    assert process.returncode == 0, stderr.decode()
                    observed = json.loads(stdout)
                    assert observed["microphoneBytes"] > 0
                    assert observed["playbackTime"] > 0
                    assert observed["closed"] is True
                    assert len(offers) == 1 and "m=audio" in offers[0]
                    assert sidebands == ["rtc_browser"]
                    assert hangups == ["rtc_browser"]
                    assert failures == []
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    if voice is not None:
                        await voice.aclose()

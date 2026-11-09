"""Application transports still execute native configuration and event handling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType, web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider
from agents.realtime import RealtimeAdapterContext, RealtimeConfig, RealtimeText
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer


def translated_reply(text: str) -> list[dict]:
    response_id, item_id = "translated-response", "translated-item"
    attribution = {
        "response_id": response_id,
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
    }
    item = {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "in_progress",
        "content": [],
    }
    completed = {**item, "status": "completed", "content": [{"type": "output_text", "text": text}]}
    return [
        {"type": "response.created", "response": {"id": response_id}},
        {
            "type": "response.output_item.added",
            "response_id": response_id,
            "output_index": 0,
            "item": item,
        },
        {
            "type": "response.content_part.added",
            **attribution,
            "part": {"type": "output_text", "text": ""},
        },
        {"type": "response.output_text.delta", **attribution, "delta": text},
        {"type": "response.output_text.done", **attribution, "text": text},
        {
            "type": "response.output_item.done",
            "response_id": response_id,
            "output_index": 0,
            "item": completed,
        },
        {"type": "response.done", "response": {"id": response_id, "output": [completed]}},
    ]


@dataclass
class AdapterPeer(RealtimePeer):
    paths: list[str] = field(default_factory=list)
    vendor_frames: list[dict] = field(default_factory=list)
    custom_url: str = ""
    vendor_url: str = ""

    async def custom(self, request: web.Request) -> web.WebSocketResponse:
        self.paths.append(request.raw_path)
        assert request.headers["x-route-key"] == "custom-connection-key"
        assert "authorization" not in request.headers
        return await self.websocket(request)

    async def vendor(self, request: web.Request) -> web.WebSocketResponse:
        self.paths.append(request.raw_path)
        assert request.headers["x-route-key"] == "custom-connection-key"
        assert "authorization" not in request.headers
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.socket = socket
        try:
            async for message in socket:
                assert message.type == WSMsgType.TEXT
                frame = json.loads(message.data)
                self.vendor_frames.append(frame)
                if frame["command"] == "configure":
                    await socket.send_json(
                        {
                            "notice": "configured",
                            "settings": {**frame["settings"], "id": "custom-model-session"},
                        }
                    )
                elif frame["command"] == "text":
                    await socket.send_json({"notice": "reply", "text": "Translated model answer"})
        finally:
            self.socket = None
            self.disconnected.set()
        return socket


@pytest_asyncio.fixture(loop_scope="function")
async def adapter_peer() -> AsyncIterator[AdapterPeer]:
    peer = AdapterPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.responses.handle)
    app.router.add_get("/socket/custom", peer.custom)
    app.router.add_get("/custom/model", peer.vendor)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.responses.url = str(server.make_url("/v1"))
        peer.custom_url = str(
            server.make_url("/socket/custom?tenant=adapter&model=fixture-realtime")
        )
        peer.vendor_url = str(server.make_url("/custom/model?tenant=adapter"))
        yield peer


@dataclass
class SocketConnection:
    client: ClientSession
    socket: ClientWebSocketResponse
    translated: bool = False
    fail_close: bool = False
    close_calls: int = 0
    close_error: LookupError = field(default_factory=lambda: LookupError("Synthetic close failure"))

    @property
    def incoming(self) -> AsyncIterator[str]:
        return self.read()

    async def read(self) -> AsyncIterator[str]:
        async for message in self.socket:
            if message.type == WSMsgType.ERROR:
                raise self.socket.exception() or RuntimeError("Custom socket failed")
            assert message.type == WSMsgType.TEXT
            if not self.translated:
                yield message.data
                continue
            frame = json.loads(message.data)
            if frame["notice"] == "configured":
                yield json.dumps({"type": "session.updated", "session": frame["settings"]})
            elif frame["notice"] == "reply":
                for canonical in translated_reply(frame["text"]):
                    yield json.dumps(canonical)
            else:
                pytest.fail("Unexpected alternate model packet")

    async def send(self, frame: str) -> None:
        if not self.translated:
            await self.socket.send_str(frame)
            return
        canonical = json.loads(frame)
        if canonical["type"] == "session.update":
            await self.socket.send_json({"command": "configure", "settings": canonical["session"]})
        elif canonical["type"] == "conversation.item.create":
            item = canonical["item"]
            assert item["role"] == "user"
            text = "".join(part["text"] for part in item["content"])
            await self.socket.send_json({"command": "text", "text": text})
        elif canonical["type"] == "response.create":
            await self.socket.send_json({"command": "continue"})
        else:
            pytest.fail("Unexpected canonical outgoing packet")

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            self.fail_close = False
            raise self.close_error
        await self.socket.close()
        await self.client.close()


@dataclass
class SocketAdapter:
    url: str
    translated: bool = False
    fail_close: bool = False
    contexts: list[RealtimeAdapterContext] = field(default_factory=list)
    connection: SocketConnection | None = None
    hold_factory: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def connect(self, context: RealtimeAdapterContext) -> SocketConnection:
        self.contexts.append(context)
        client = ClientSession()
        try:
            socket = await client.ws_connect(
                self.url, headers={"x-route-key": "custom-connection-key"}
            )
        except BaseException:
            await client.close()
            raise
        self.connection = SocketConnection(client, socket, self.translated, self.fail_close)
        self.entered.set()
        if self.hold_factory:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # An uncooperative factory can return a connection after cancel.
                # Its SDK owner must retain, close and join that late resource.
                self.cancelled.set()
                await self.release.wait()
        return self.connection


def provider(peer: AdapterPeer) -> NativeProvider:
    return NativeProvider(
        base_url=peer.responses.url,
        api_key="synthetic-native-adapter-key",
        supports_websockets=False,
    )


async def test_native_adapter_uses_exact_custom_path_query_and_non_bearer_headers(adapter_peer):
    peer = adapter_peer
    adapter = SocketAdapter(peer.custom_url)
    async with (
        timeout(20),
        provider(peer) as backend,
        Agent(instructions="Speak.", provider=backend).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", adapter=adapter)
        ) as voice:
            await voice.send_text("Native route")
            sent = await peer.frame(lambda frame: frame["type"] == "conversation.item.create")
            assert "Native route" in json.dumps(sent)
            assert peer.paths == ["/socket/custom?tenant=adapter&model=fixture-realtime"]
            assert len(adapter.contexts) == 1
            context = adapter.contexts[0]
            assert context.model == "fixture-realtime"
            assert "/v1/realtime" in context.url
            assert any(key.lower() == "authorization" for key in context.headers)
            assert "synthetic-native-adapter-key" not in repr(context)
            assert peer.responses.requests == []
        assert adapter.connection is not None
        assert adapter.connection.socket.closed and adapter.connection.client.closed


async def test_native_adapter_translates_an_alternate_model_protocol(adapter_peer):
    peer = adapter_peer
    adapter = SocketAdapter(peer.vendor_url, translated=True)
    async with (
        timeout(20),
        provider(peer) as backend,
        Agent(instructions="Speak.", provider=backend).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", adapter=adapter)
        ) as voice:
            await voice.send_text("Explain this")
            async for event in voice.events():
                if isinstance(event, RealtimeText) and event.final and event.role == "assistant":
                    assert event.text == "Translated model answer"
                    assert event.response_id == "translated-response"
                    break
            else:
                pytest.fail("The native runtime did not parse the translated reply")
            assert peer.paths == ["/custom/model?tenant=adapter"]
            assert any(
                frame == {"command": "text", "text": "Explain this"} for frame in peer.vendor_frames
            )
            assert "Translated model answer" in repr(voice.history)
            assert peer.responses.requests == []


async def test_native_adapter_cancellation_joins_factory_and_closes_late_connection(adapter_peer):
    peer = adapter_peer
    adapter = SocketAdapter(peer.custom_url, hold_factory=True)
    async with (
        timeout(20),
        provider(peer) as backend,
        Agent(instructions="Speak.", provider=backend).session() as session,
    ):
        voice = session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter))
        opening = asyncio.create_task(voice.__aenter__())
        cancellation_requested = False
        try:
            await adapter.entered.wait()
            opening.cancel()
            cancellation_requested = True
            await adapter.cancelled.wait()
            assert not opening.done()
        finally:
            adapter.release.set()
            if not opening.done() and not cancellation_requested:
                opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert adapter.connection is not None
        assert adapter.connection.socket.closed and adapter.connection.client.closed
        replacement = SocketAdapter(peer.custom_url)
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", adapter=replacement)
        ):
            pass
        assert replacement.connection is not None and replacement.connection.client.closed


async def test_native_adapter_failed_physical_close_retains_owner_and_retries(adapter_peer):
    peer = adapter_peer
    adapter = SocketAdapter(peer.custom_url, fail_close=True)
    async with (
        timeout(20),
        provider(peer) as backend,
        Agent(instructions="Speak.", provider=backend).session() as session,
    ):
        voice = await session.realtime(
            config=RealtimeConfig(model="fixture-realtime", adapter=adapter)
        ).__aenter__()
        try:
            assert adapter.connection is not None
            with pytest.raises(LookupError) as failure:
                await voice.aclose()
            assert failure.value is adapter.connection.close_error
            assert not adapter.connection.client.closed
            with pytest.raises(RuntimeError, match="already has"):
                async with session.realtime(
                    config=RealtimeConfig(
                        model="fixture-realtime", adapter=SocketAdapter(peer.custom_url)
                    )
                ):
                    pytest.fail("A failed close released its Realtime owner")
        finally:
            await voice.aclose()
        assert adapter.connection.socket.closed and adapter.connection.client.closed
        assert adapter.connection.close_calls >= 2

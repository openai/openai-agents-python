from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Iterator, Sequence
from typing import cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope
from starlette.websockets import WebSocketDisconnect, WebSocketState

from agents.realtime import RealtimeSession
from examples.realtime.twilio import server, twilio_handler

AUTH_TOKEN = "synthetic-twilio-auth-token"
PUBLIC_URL = "https://voice.example.test"


def sign(url: str, params: Sequence[tuple[str, str]] = ()) -> str:
    # Independent protocol oracle, including repeated form values.
    payload = url + "".join(key + value for key, value in sorted(set(params)))
    return base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


class FakeSession:
    def __init__(self) -> None:
        self.enter = AsyncMock()
        self.close = AsyncMock()
        self.audio_received = asyncio.Event()
        self.send_audio = AsyncMock(side_effect=lambda _audio: self.audio_received.set())
        self.events_done = asyncio.Event()

    async def __aiter__(self):
        await self.events_done.wait()
        if False:
            yield None


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    result = FakeSession()
    runner = Mock()
    runner.run = AsyncMock(return_value=result)
    monkeypatch.setattr(twilio_handler, "RealtimeRunner", Mock(return_value=runner))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    return result


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("TWILIO_PUBLIC_BASE_URL", PUBLIC_URL)


@pytest.fixture
def client(configured: None) -> Iterator[TestClient]:
    with TestClient(server.app) as result:
        yield result


@pytest.mark.parametrize(
    "name,value",
    [
        ("TWILIO_AUTH_TOKEN", ""),
        ("TWILIO_PUBLIC_BASE_URL", "http://voice.example.test"),
        ("TWILIO_PUBLIC_BASE_URL", "https://voice.example.test/path"),
        ("TWILIO_PUBLIC_BASE_URL", "https://voice.example.test:bad"),
        ("TWILIO_PUBLIC_BASE_URL", "https://voice.example.test:65536"),
    ],
)
def test_invalid_configuration_fails_at_startup(
    configured: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        with TestClient(server.app):
            pass


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unsigned_webhook_is_rejected(client: TestClient, method: str) -> None:
    assert client.request(method, "/incoming-call").status_code == 403


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_signed_webhook_uses_configured_origin(client: TestClient, method: str) -> None:
    params = [("CallSid", "CA-synthetic"), ("Tag", "two"), ("Tag", "one")]
    path = "/incoming-call?source=phone"
    body = b""
    headers = {
        "Host": "attacker.example",
        "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-Proto": "http",
    }
    if method == "GET":
        path += "&" + urlencode(params)
        signed_params = []
    else:
        body = urlencode(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        signed_params = params
    headers["X-Twilio-Signature"] = sign(PUBLIC_URL + path, signed_params)
    response = client.request(method, path, content=body, headers=headers)
    assert response.status_code == 200
    stream = ElementTree.fromstring(response.text).find("Connect/Stream")
    assert stream is not None
    assert stream.attrib["url"] == "wss://voice.example.test/media-stream"
    response = client.request(method, path + "&tampered=1", content=body, headers=headers)
    assert response.status_code == 403


def test_changed_post_body_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/incoming-call",
        data={"CallSid": "changed"},
        headers={
            "X-Twilio-Signature": sign(PUBLIC_URL + "/incoming-call", [("CallSid", "original")])
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", [None, b"1"])
async def test_webhook_body_limit_stops_reading_before_signature_validation(
    configured: None, content_length: bytes | None
) -> None:
    # Each field is small enough to parse, but the stream exceeds the total budget.
    chunk = b"Tag=" + b"a" * 4096 + b"&"
    receive = AsyncMock(return_value={"type": "http.request", "body": chunk, "more_body": True})
    send = AsyncMock()
    headers = [
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"x-twilio-signature", b"invalid"),
    ]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/incoming-call",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("localhost", 8000),
    }
    async with server.lifespan(server.app):
        await asyncio.wait_for(server.app(scope, receive, send), timeout=2)
    assert send.call_args_list[0].args[0]["status"] == 413
    assert receive.await_count == 16


@pytest.mark.parametrize("body", ["Tag=" + "x" * 8192, "Tag=x&" * 101])
def test_webhook_form_limits(client: TestClient, body: str) -> None:
    response = client.post(
        "/incoming-call",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": "invalid",
        },
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "configured_origin,public_origin,stream_url",
    [
        ("HTTPS://voice.example.test/", PUBLIC_URL, "wss://voice.example.test/media-stream"),
        (
            "https://voice.example.test:8443",
            "https://voice.example.test:8443",
            "wss://voice.example.test:8443/media-stream",
        ),
    ],
)
def test_configured_origin_supports_signed_call_and_stream(
    configured: None,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
    configured_origin: str,
    public_origin: str,
    stream_url: str,
) -> None:
    monkeypatch.setenv("TWILIO_PUBLIC_BASE_URL", configured_origin)
    with TestClient(server.app) as client:
        response = client.get(
            "/incoming-call", headers={"X-Twilio-Signature": sign(public_origin + "/incoming-call")}
        )
        assert response.status_code == 200
        stream = ElementTree.fromstring(response.text).find("Connect/Stream")
        assert stream is not None
        assert stream.attrib["url"] == stream_url
        with client.websocket_connect(
            "/media-stream", headers={"X-Twilio-Signature": sign(stream.attrib["url"])}
        ) as ws:
            ws.send_json({"event": "stop"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    session.close.assert_awaited_once()


@pytest.mark.parametrize("signature", ["", "invalid", sign("wss://attacker.example/media-stream")])
def test_websocket_rejected_before_handler_creation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, signature: str
) -> None:
    factory = Mock()
    monkeypatch.setattr(server, "TwilioHandler", factory)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/media-stream", headers={"X-Twilio-Signature": signature}):
            pass
    assert exc.value.code == 1008
    factory.assert_not_called()


@pytest.mark.parametrize("suffix", ["", "/"])
def test_signed_websocket_stop_closes_session(
    client: TestClient, session: FakeSession, suffix: str
) -> None:
    with client.websocket_connect(
        "/media-stream",
        headers={
            "X-Twilio-Signature": sign("wss://voice.example.test/media-stream" + suffix),
            "Host": "attacker.example",
        },
    ) as ws:
        ws.send_json({"event": "stop"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    session.enter.assert_awaited_once()
    session.close.assert_awaited_once()


def test_startup_failure_closes_partial_session(client: TestClient, session: FakeSession) -> None:
    session.enter.side_effect = RuntimeError("synthetic connection failure")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/media-stream",
            headers={"X-Twilio-Signature": sign("wss://voice.example.test/media-stream")},
        ):
            pass
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["disconnect", "model_end", "cancel", "oversized"])
async def test_call_termination_releases_all_tasks(session: FakeSession, termination: str) -> None:
    socket = Mock()
    socket.application_state = WebSocketState.CONNECTED
    socket.accept = AsyncMock()
    socket.close = AsyncMock()
    incoming: asyncio.Queue[str] = asyncio.Queue()
    socket.receive_text = AsyncMock(side_effect=incoming.get)
    handler = twilio_handler.TwilioHandler(socket)
    await handler.start()
    tasks = handler._background_tasks()

    async def run_call() -> None:
        try:
            await handler.wait_until_done()
        finally:
            await handler.close()

    owner = asyncio.create_task(run_call())
    await asyncio.sleep(0)
    if termination == "disconnect":
        socket.receive_text.side_effect = WebSocketDisconnect()
        incoming.put_nowait('{"event":"connected"}')
    elif termination == "model_end":
        session.events_done.set()
    elif termination == "cancel":
        owner.cancel()
    else:
        incoming.put_nowait("x" * (handler.MAX_MESSAGE_BYTES + 1))
    if termination == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2)
    else:
        await asyncio.wait_for(owner, timeout=2)
    assert all(task.done() for task in tasks)
    session.close.assert_awaited_once()
    assert socket.close.await_count >= 1
    session.send_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_cleanup_failure_still_closes_websocket(session: FakeSession) -> None:
    socket = Mock()
    socket.application_state = WebSocketState.CONNECTED
    socket.close = AsyncMock()
    handler = twilio_handler.TwilioHandler(socket)
    handler.session = cast(RealtimeSession, session)
    session.close.side_effect = RuntimeError("synthetic close failure")
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await handler.close()
    socket.close.assert_awaited_once()


def test_signed_audio_and_disconnect(
    client: TestClient, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWILIO_STARTUP_BUFFER_CHUNKS", "0")
    audio = b"\x01" * 400
    with client.websocket_connect(
        "/media-stream",
        headers={"X-Twilio-Signature": sign("wss://voice.example.test/media-stream")},
    ) as ws:
        ws.send_json({"event": "media", "media": {"payload": base64.b64encode(audio).decode()}})
        ws.portal.call(session.audio_received.wait)
        # TestClient cancels its ASGI scope during disconnect; cleanup must finish.
    session.send_audio.assert_awaited_once_with(audio)
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_failure", [False, True])
async def test_peer_disconnect_does_not_send_another_close(
    configured: None, session: FakeSession, close_failure: bool
) -> None:
    # Model the Uvicorn ASGI transport boundary, which rejects sends after disconnect.
    disconnected = False
    receives: asyncio.Queue[Message] = asyncio.Queue()
    receives.put_nowait({"type": "websocket.connect"})
    receives.put_nowait({"type": "websocket.disconnect", "code": 1006})
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal disconnected
        message = await receives.get()
        if message["type"] == "websocket.disconnect":
            disconnected = True
        return message

    async def send(message: Message) -> None:
        if disconnected:
            raise RuntimeError("Unexpected ASGI message after peer disconnect")
        sent.append(message)

    scope: Scope = {
        "type": "websocket",
        "path": "/media-stream",
        "query_string": b"",
        "headers": [
            (b"x-twilio-signature", sign("wss://voice.example.test/media-stream").encode())
        ],
        "scheme": "ws",
        "server": ("localhost", 8000),
    }
    if close_failure:
        session.close.side_effect = RuntimeError("synthetic session close failure")
    async with server.lifespan(server.app):
        if close_failure:
            with pytest.raises(RuntimeError, match="synthetic session close failure"):
                await asyncio.wait_for(server.app(scope, receive, send), timeout=2)
        else:
            await asyncio.wait_for(server.app(scope, receive, send), timeout=2)
    assert [message["type"] for message in sent] == ["websocket.accept"]
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_failure", [False, True])
async def test_transport_closes_before_disconnect_is_received(
    configured: None, session: FakeSession, monkeypatch: pytest.MonkeyPatch, close_failure: bool
) -> None:
    from uvicorn import Config
    from uvicorn._types import ASGISendEvent, WebSocketScope
    from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol
    from uvicorn.server import ServerState

    handlers: list[twilio_handler.TwilioHandler] = []

    def make_handler(websocket):
        handler = twilio_handler.TwilioHandler(websocket)
        handlers.append(handler)
        return handler

    monkeypatch.setattr(server, "TwilioHandler", make_handler)
    receiving = asyncio.Event()
    receive_cancelled = asyncio.Event()
    messages: asyncio.Queue[Message] = asyncio.Queue()
    messages.put_nowait({"type": "websocket.connect"})

    async def receive() -> Message:
        if messages.empty():
            receiving.set()
        try:
            return await messages.get()
        except asyncio.CancelledError:
            receive_cancelled.set()
            raise

    scope: Scope = {
        "type": "websocket",
        "path": "/media-stream",
        "query_string": b"",
        "headers": [
            (b"x-twilio-signature", sign("wss://voice.example.test/media-stream").encode())
        ],
        "scheme": "ws",
        "server": ("localhost", 8000),
        "client": ("localhost", 12345),
        "root_path": "",
    }
    # Exercise the included backend's real closed-send behavior without opening a socket.
    protocol = WebSocketProtocol(Config(server.app, log_config=None), ServerState(), {})
    protocol.scope = cast(WebSocketScope, scope)
    sent: list[str] = []

    async def send(message: Message) -> None:
        sent.append(message["type"])
        await protocol.asgi_send(cast(ASGISendEvent, message))

    session_error = RuntimeError("synthetic session close failure")
    if close_failure:
        session.close.side_effect = session_error
    async with server.lifespan(server.app):
        owner = asyncio.create_task(server.app(scope, receive, send))
        await asyncio.wait_for(receiving.wait(), timeout=2)
        socket = handlers[0].twilio_websocket
        assert socket.client_state == socket.application_state == WebSocketState.CONNECTED
        protocol.closed_event.set()
        session.events_done.set()  # Model completion wins before disconnect can be delivered.
        if close_failure:
            with pytest.raises(RuntimeError) as exc:
                await asyncio.wait_for(owner, timeout=2)
            assert exc.value is session_error
        else:
            await asyncio.wait_for(owner, timeout=2)
    assert receive_cancelled.is_set()
    assert socket.client_state == WebSocketState.CONNECTED
    assert all(task.done() for task in handlers[0]._background_tasks())
    assert sent == ["websocket.accept", "websocket.close"]
    session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnected", [False, True])
async def test_close_only_tolerates_transport_disconnect(
    session: FakeSession, disconnected: bool
) -> None:
    socket = Mock()
    socket.application_state = socket.client_state = WebSocketState.CONNECTED
    failure = WebSocketDisconnect(code=1006) if disconnected else RuntimeError("unrelated failure")
    socket.close = AsyncMock(side_effect=failure)
    handler = twilio_handler.TwilioHandler(socket)
    handler.session = cast(RealtimeSession, session)
    if disconnected:
        await handler.close()
    else:
        with pytest.raises(RuntimeError) as exc:
            await handler.close()
        assert exc.value is failure
    session.close.assert_awaited_once()
    socket.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,close_failure", [("tasks", False), ("session", False), ("session", True)]
)
async def test_server_cancellation_during_cleanup_waits_for_owned_resources(
    configured: None,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    close_failure: bool,
) -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    session_closed = asyncio.Event()
    handlers: list[twilio_handler.TwilioHandler] = []

    class Handler(twilio_handler.TwilioHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            handlers.append(self)

        async def _buffer_flush_loop(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                if phase == "tasks":
                    cleanup_started.set()
                    await release_cleanup.wait()

    async def close_session() -> None:
        if phase == "session":
            cleanup_started.set()
            await release_cleanup.wait()
        session_closed.set()
        if close_failure:
            raise RuntimeError("synthetic session close failure")

    session.close.side_effect = close_session
    monkeypatch.setattr(server, "TwilioHandler", Handler)
    receive: asyncio.Queue[Message] = asyncio.Queue()
    receive.put_nowait({"type": "websocket.connect"})
    receive.put_nowait({"type": "websocket.receive", "text": '{"event":"stop"}'})
    send = AsyncMock()
    scope: Scope = {
        "type": "websocket",
        "path": "/media-stream",
        "query_string": b"",
        "headers": [
            (b"x-twilio-signature", sign("wss://voice.example.test/media-stream").encode())
        ],
        "scheme": "ws",
        "server": ("localhost", 8000),
    }
    async with server.lifespan(server.app):
        owner = asyncio.create_task(server.app(scope, receive.get, send))
        try:
            await asyncio.wait_for(cleanup_started.wait(), timeout=2)
            owner.cancel()  # Uvicorn's graceful-shutdown timeout uses Task.cancel().
            await asyncio.sleep(0)
            assert not owner.done()
        finally:
            release_cleanup.set()
        if close_failure:
            with pytest.raises(RuntimeError, match="synthetic session close failure"):
                await asyncio.wait_for(owner, timeout=2)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, timeout=2)
    assert session_closed.is_set()
    session.close.assert_awaited_once()
    assert all(task.done() for task in handlers[0]._background_tasks())
    assert [call.args[0]["type"] for call in send.call_args_list] == [
        "websocket.accept",
        "websocket.close",
    ]

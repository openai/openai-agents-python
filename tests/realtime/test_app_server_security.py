from __future__ import annotations

import asyncio
import importlib
import json
import runpy
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


class DemoSession:
    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.messages: list[Any] = []
        self.approvals: list[tuple[str, bool]] = []
        self.closed = False
        self.events_stopped = False

    async def __aenter__(self) -> DemoSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True

    async def __aiter__(self):
        try:
            await asyncio.Future()
            yield  # pragma: no cover
        finally:
            self.events_stopped = True

    async def send_audio(self, data: bytes) -> None:
        self.audio.append(data)

    async def send_message(self, message: Any) -> None:
        self.messages.append(message)

    async def approve_tool_call(self, call_id: str, *, always: bool) -> None:
        self.approvals.append((call_id, always))


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, list[DemoSession]]:
    server = importlib.import_module("examples.realtime.app.server")
    monkeypatch.setattr(server, "manager", server.RealtimeWebSocketManager())
    sessions: list[DemoSession] = []

    class Runner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self, **kwargs: Any) -> DemoSession:
            session = DemoSession()
            sessions.append(session)
            return session

    monkeypatch.setattr(server, "RealtimeRunner", Runner)
    return server, sessions


def assert_released(server: ModuleType, sessions: list[DemoSession]) -> None:
    assert not server.manager.websockets
    assert not server.manager.active_sessions
    assert not server.manager.session_contexts
    assert not server.manager.event_tasks
    assert all(session.closed and session.events_stopped for session in sessions)


@pytest.mark.parametrize(
    ("host", "origins"),
    [
        ("localhost:8000", []),
        ("localhost:8000", [("origin", "null")]),
        ("localhost:8000", [("origin", "https://untrusted.example")]),
        ("localhost:8000", [("origin", "http://localhost:9000")]),
        ("untrusted.example:8000", [("origin", "http://untrusted.example:8000")]),
        ("localhost:8000", [("origin", "http://localhost:8000")] * 2),
    ],
)
def test_reject_before_opening_upstream(demo, host: str, origins: list[tuple[str, str]]) -> None:
    server, sessions = demo
    with TestClient(server.app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/ws/demo", headers=httpx.Headers([("host", host), *origins])
            ):
                pytest.fail("Rejected client was accepted")
    assert rejected.value.code == 1008
    assert sessions == []
    assert_released(server, sessions)


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000"])
def test_local_audio_image_and_approval_round_trip(demo, host: str) -> None:
    server, sessions = demo
    with TestClient(server.app, base_url=f"http://{host}") as client:
        with client.websocket_connect(
            "/ws/demo", headers={"host": host, "origin": f"http://{host}"}
        ) as ws:
            ws.send_json({"type": "audio", "data": [-32768, 0, 32767]})
            ws.send_json({"type": "tool_approval_decision", "call_id": "call-1", "approve": True})
            ws.send_json({"type": "image_start", "id": "image-1", "text": "Describe it"})
            assert ws.receive_json()["info"] == "image_start_ack"
            ws.send_json(
                {"type": "image_chunk", "id": "image-1", "chunk": "data:image/png;base64,"}
            )
            ws.send_json({"type": "image_chunk", "id": "image-1", "chunk": "eA=="})
            ws.send_json({"type": "image_end", "id": "image-1"})
            assert ws.receive_json()["info"] == "image_enqueued"
            assert sessions[0].audio == [b"\x00\x80\x00\x00\xff\x7f"]
            assert sessions[0].approvals == [("call-1", False)]
            assert sessions[0].messages[0]["content"] == [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,eA==",
                    "detail": "high",
                },
                {"type": "input_text", "text": "Describe it"},
            ]
    assert_released(server, sessions)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("not json", 1008),
        ("[]", 1008),
        (json.dumps({"type": "audio", "data": [32768]}), 1008),
        (json.dumps({"type": "audio", "data": [True]}), 1008),
        (json.dumps({"type": "audio", "data": [0] * 24_001}), 1008),
        (json.dumps({"type": "audio", "data": "not samples"}), 1008),
        (json.dumps({"type": "tool_approval_decision", "call_id": "c", "approve": "false"}), 1008),
        (json.dumps({"type": "image", "data_url": ["invalid"]}), 1008),
        (json.dumps({"type": "image_start", "id": "i", "text": []}), 1008),
        (" " * (1024 * 1024 + 1), 1009),
        (b"binary", 1003),
    ],
    ids=[
        "invalid-json",
        "non-object",
        "audio-range",
        "bool-audio",
        "audio-count",
        "audio-shape",
        "approval-type",
        "image-shape",
        "prompt-shape",
        "message-size",
        "binary",
    ],
)
def test_invalid_messages_close_and_release_the_session(
    demo, payload: str | bytes, code: int
) -> None:
    server, sessions = demo
    with TestClient(server.app, base_url="http://localhost:8000") as client:
        with client.websocket_connect(
            "/ws/demo", headers={"host": "localhost:8000", "origin": "http://localhost:8000"}
        ) as ws:
            if isinstance(payload, bytes):
                ws.send_bytes(payload)
            else:
                ws.send_text(payload)
            with pytest.raises(WebSocketDisconnect) as rejected:
                ws.receive_json()
            assert rejected.value.code == code
    assert len(sessions) == 1
    assert not sessions[0].audio and not sessions[0].messages and not sessions[0].approvals
    assert_released(server, sessions)


@pytest.mark.parametrize("overflow", ["second-image", "size", "chunks", "empty-chunk"])
def test_incomplete_image_uploads_are_bounded(demo, monkeypatch, overflow: str) -> None:
    server, sessions = demo
    monkeypatch.setattr(server, "MAX_IMAGE_CHARS", 8)
    monkeypatch.setattr(server, "MAX_IMAGE_CHUNKS", 2)
    with TestClient(server.app, base_url="http://localhost:8000") as client:
        with client.websocket_connect(
            "/ws/demo", headers={"host": "localhost:8000", "origin": "http://localhost:8000"}
        ) as ws:
            ws.send_json({"type": "image_start", "id": "i"})
            assert ws.receive_json()["info"] == "image_start_ack"
            if overflow == "second-image":
                ws.send_json({"type": "image_start", "id": "j"})
            elif overflow == "empty-chunk":
                ws.send_json({"type": "image_chunk", "id": "i", "chunk": ""})
            else:
                chunk = "12345" if overflow == "size" else "x"
                for _ in range(2 if overflow == "size" else 3):
                    ws.send_json({"type": "image_chunk", "id": "i", "chunk": chunk})
            with pytest.raises(WebSocketDisconnect) as rejected:
                ws.receive_json()
            assert rejected.value.code == (
                1008 if overflow in {"second-image", "empty-chunk"} else 1009
            )
    assert not sessions[0].messages
    assert_released(server, sessions)


def test_colliding_client_labels_keep_independent_resources(demo) -> None:
    server, sessions = demo
    with TestClient(server.app, base_url="http://localhost:8000") as client:
        headers = {"host": "localhost:8000", "origin": "http://localhost:8000"}
        with client.websocket_connect("/ws/same-label", headers=headers) as first:
            with client.websocket_connect("/ws/same-label", headers=headers) as second:
                first.send_json(
                    {"type": "image", "data_url": "data:image/png;base64,eA==", "text": "first"}
                )
                assert first.receive_json()["info"] == "image_enqueued"
                second.send_json(
                    {"type": "image", "data_url": "data:image/png;base64,eA==", "text": "second"}
                )
                assert second.receive_json()["info"] == "image_enqueued"
                assert len(server.manager.active_sessions) == 2
            assert sessions[1].closed
            assert not sessions[0].closed
            first.send_json(
                {"type": "image", "data_url": "data:image/png;base64,eA==", "text": "survivor"}
            )
            assert first.receive_json()["info"] == "image_enqueued"
            assert [m["content"][1]["text"] for m in sessions[0].messages] == ["first", "survivor"]
            assert [m["content"][1]["text"] for m in sessions[1].messages] == ["second"]
    assert_released(server, sessions)


def test_session_limit_rejects_without_disrupting_existing_connections(demo) -> None:
    server, sessions = demo
    with TestClient(server.app, base_url="http://localhost:8000") as client:
        with ExitStack() as connections:
            headers = {"host": "localhost:8000", "origin": "http://localhost:8000"}
            sockets = [
                connections.enter_context(client.websocket_connect("/ws/demo", headers=headers))
                for _ in range(4)
            ]
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect("/ws/demo", headers=headers):
                    pytest.fail("Fifth session was accepted")
            assert rejected.value.code == 1008
            assert len(sessions) == 4
            for ws in sockets:
                ws.send_json({"type": "image_start", "id": "i"})
                assert ws.receive_json()["info"] == "image_start_ack"
            assert len(server.manager.websockets) == 4
    assert_released(server, sessions)


def test_connecting_sessions_reserve_capacity_and_release_on_cancellation(
    demo, monkeypatch
) -> None:
    server, sessions = demo
    started: list[bool] = []

    async def pending_run(self, **kwargs):
        started.append(True)
        await asyncio.Future()

    monkeypatch.setattr(server.RealtimeRunner, "run", pending_run)
    with TestClient(server.app) as client:
        with ExitStack() as connections:
            headers = {"host": "localhost:8000", "origin": "http://localhost:8000"}
            for _ in range(4):
                connections.enter_context(client.websocket_connect("/ws/demo", headers=headers))
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect("/ws/demo", headers=headers):
                    pytest.fail("Connecting sessions did not reserve capacity")
            assert rejected.value.code == 1008
            assert len(started) == 4
            assert len(server.manager.websockets) == 4
    assert_released(server, sessions)


def test_setup_failure_releases_reserved_capacity(demo, monkeypatch) -> None:
    server, sessions = demo

    async def failed_run(self, **kwargs):
        raise RuntimeError("synthetic setup failure")

    monkeypatch.setattr(server.RealtimeRunner, "run", failed_run)
    with TestClient(server.app) as client:
        with pytest.raises(RuntimeError, match="synthetic setup failure"):
            with client.websocket_connect(
                "/ws/demo", headers={"host": "localhost:8000", "origin": "http://localhost:8000"}
            ) as ws:
                ws.receive_json()
    assert_released(server, sessions)


def test_completed_image_releases_upload_budget(demo, monkeypatch) -> None:
    server, sessions = demo
    monkeypatch.setattr(server, "MAX_IMAGE_CHARS", 8)
    monkeypatch.setattr(server, "MAX_IMAGE_CHUNKS", 2)
    with TestClient(server.app) as client:
        with client.websocket_connect(
            "/ws/demo", headers={"host": "localhost:8000", "origin": "http://localhost:8000"}
        ) as ws:
            for _ in range(2):
                ws.send_json({"type": "image_start", "id": "i"})
                assert ws.receive_json()["info"] == "image_start_ack"
                for chunk in ("1234", "5678"):
                    ws.send_json({"type": "image_chunk", "id": "i", "chunk": chunk})
                ws.send_json({"type": "image_end", "id": "i"})
                assert ws.receive_json()["size"] == 8
    assert len(sessions[0].messages) == 2
    assert_released(server, sessions)


def test_launch_uses_bounded_loopback_transport(monkeypatch) -> None:
    import uvicorn

    options: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: options.update(kwargs))
    app_dir = Path(__file__).parents[2] / "examples" / "realtime" / "app"
    monkeypatch.syspath_prepend(str(app_dir))
    runpy.run_path(str(app_dir / "server.py"), run_name="__main__")
    assert options["host"] == "127.0.0.1"
    assert options["ws"] == "websockets"
    assert options["ws_max_size"] == 1024 * 1024
    assert options["ws_max_queue"] == 4

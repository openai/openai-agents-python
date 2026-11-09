"""Hosted errors are checked before serialization and before native export."""

import json
import weakref

import pytest

import _agents_codex.realtime as realtime
from tests._asyncio import timeout
from tests.test_native_realtime import realtime_peer as realtime_peer
from tests.test_native_realtime_hosted import opened
from tests.test_realtime_terminal_event import owner_for


class ErrorObject(dict):
    pass


def ingest_error(owner, error, arguments="{}"):
    owner._ingest(
        {
            "id": owner._generation,
            "msg": {
                "type": "realtime_conversation_realtime",
                "payload": {
                    "HostedMcpCallCompleted": {
                        "item_id": "call",
                        "response_id": "response",
                        "server_label": "docs",
                        "tool_name": "search",
                        "arguments": arguments,
                        "error": error,
                    }
                },
            },
        }
    )


@pytest.mark.parametrize("kind", ["nodes", "emoji", "control", "queued", "events", "metadata"])
async def test_hosted_error_rejects_before_dumps_without_retaining_input(monkeypatch, kind):
    owner = owner_for()
    if kind == "queued":
        owner._emit(realtime.RealtimeText("assistant", "x" * 1_500_000, False))
    error = ErrorObject(
        value=(
            [{} for _ in range(33_000)]
            if kind == "nodes"
            else "\U0001f680" * 180_000
            if kind == "emoji"
            else "\x00" * 400_000
            if kind == "control"
            else "x" * 600_000
        )
    )
    if kind == "events":
        for _ in range(owner._EVENT_LIMIT):
            owner._emit(realtime.RealtimeText("assistant", "", False))
        error = ErrorObject(value="small")
    if kind == "metadata":
        error = ErrorObject(value="x" * 1_700_000)
    retained = weakref.ref(error)

    def forbidden_dump(*args, **kwargs):
        raise AssertionError("Reject structured error before json.dumps")

    monkeypatch.setattr(realtime.json, "dumps", forbidden_dump)
    ingest_error(owner, error, "x" * 500_000 if kind == "metadata" else "{}")
    assert owner._terminal.done()
    failure = owner._terminal.result()
    assert isinstance(failure, RuntimeError)
    assert "capacity" in str(failure)
    assert failure.__traceback__ is None
    assert failure.__context__ is None
    del error
    assert retained() is None
    assert owner._queue_bytes == 0
    await owner.aclose()


@pytest.mark.parametrize(
    "error", [None, "provider failed", {"detail": ["\U0001f680", "\n", False, 1.5]}]
)
async def test_hosted_error_preserves_public_string_api(error):
    owner = owner_for()
    ingest_error(owner, error)
    assert not owner._terminal.done()
    event = owner._dequeue()
    assert isinstance(event, realtime.RealtimeHostedMcpCallCompleted)
    assert event.error == (json.dumps(error) if isinstance(error, dict) else error)
    assert owner._queue_bytes == 0
    await owner.aclose()


@pytest.mark.parametrize("sparse", [False, True], ids=["completed-item", "sparse-failure"])
async def test_native_wire_rejects_structured_hosted_error_before_host_conversion(
    realtime_peer, monkeypatch, sparse
):
    original = realtime.RealtimeSession._payload
    delivered = []

    def capture(self, payload, native_input):
        if isinstance(payload, dict) and "HostedMcpCallCompleted" in payload:
            delivered.append(payload)
            self._finish(RuntimeError("Structured error escaped native guard"))
            return
        original(self, payload, native_input)

    monkeypatch.setattr(realtime.RealtimeSession, "_payload", capture)
    async with timeout(10), opened(realtime_peer, automatic=False) as voice:
        socket = realtime_peer.socket
        assert socket is not None
        await socket.send_json({"type": "response.created", "response": {"id": "response"}})
        item = {
            "type": "mcp_call",
            "id": "call",
            "server_label": "docs",
            "name": "search",
            "arguments": "{}",
        }
        await socket.send_json(
            {
                "type": "response.output_item.added",
                "response_id": "response",
                "output_index": 0,
                "item": item,
            }
        )
        error = {"parts": [{} for _ in range(33_000)]}
        await socket.send_json(
            {
                "type": "response.mcp_call.failed",
                "response_id": "response",
                "item_id": "call",
                "error": error,
            }
            if sparse
            else {
                "type": "conversation.item.done",
                "item": {**item, "error": error, "status": "completed"},
            }
        )
        with pytest.raises(RuntimeError, match="capacity|configuration|identity"):
            async for _ in voice.events():
                pass
        assert delivered == []

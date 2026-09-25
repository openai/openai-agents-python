import asyncio
import json

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError
from websockets.frames import CloseCode

from agents.realtime.model import RealtimeModelConfig, RealtimeModelListener
from agents.realtime.model_events import (
    RealtimeModelEvent,
    RealtimeModelExceptionEvent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelRawServerEvent,
)
from agents.realtime.openai_realtime import (
    OpenAIRealtimeSIPModel,
    OpenAIRealtimeWebSocketModel,
    TransportConfig,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_type", [OpenAIRealtimeWebSocketModel, OpenAIRealtimeSIPModel])
@pytest.mark.parametrize(
    ("transport_config", "message_size", "accepted"),
    [
        pytest.param(None, 8 * 1024 * 1024, True, id="default-at-limit"),
        pytest.param(None, 8 * 1024 * 1024 + 1, False, id="default-over-limit"),
        pytest.param(
            {"ping_interval": None}, 8 * 1024 * 1024 + 1, False, id="other-config-over-limit"
        ),
        pytest.param({"max_size": 1024}, 1025, False, id="explicit-smaller-limit"),
        pytest.param(
            {"max_size": 9 * 1024 * 1024}, 8 * 1024 * 1024 + 1, True, id="explicit-larger-limit"
        ),
        pytest.param({"max_size": None}, 8 * 1024 * 1024 + 1, True, id="explicit-unlimited"),
    ],
)
async def test_incoming_message_size_limit(
    model_type: type[OpenAIRealtimeWebSocketModel],
    transport_config: TransportConfig | None,
    message_size: int,
    accepted: bool,
) -> None:
    events: list[RealtimeModelEvent] = []
    result_received = asyncio.Event()
    peer_closed = asyncio.Event()
    peer_close_code: int | None = None

    class Listener(RealtimeModelListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            events.append(event)
            if isinstance(event, RealtimeModelOutputTextDeltaEvent | RealtimeModelExceptionEvent):
                result_received.set()

    payload = {
        "type": "response.output_text.delta",
        "event_id": "event_test",
        "response_id": "resp_test",
        "item_id": "item_test",
        "output_index": 0,
        "content_index": 0,
        "delta": "",
    }
    payload["delta"] = "x" * (message_size - len(json.dumps(payload).encode("utf-8")))
    message = json.dumps(payload)
    assert len(message.encode("utf-8")) == message_size

    async def handler(websocket: ServerConnection) -> None:
        nonlocal peer_close_code
        # Wait for session.update before sending the response.
        await websocket.recv()
        # Keep the library's default compression enabled on both ends. For rejections,
        # each frame fits the limit; bound the assembled, decompressed message.
        if accepted:
            await websocket.send(message)
        else:
            midpoint = len(message) // 2
            await websocket.send([message[:midpoint], message[midpoint:]])
        await websocket.wait_closed()
        peer_close_code = websocket.close_code
        peer_closed.set()

    model = model_type(transport_config=transport_config)
    model.add_listener(Listener())
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        options: RealtimeModelConfig = {
            "api_key": "test-key",
            "url": f"ws://127.0.0.1:{port}/v1/realtime",
        }
        if model_type is OpenAIRealtimeSIPModel:
            options["call_id"] = "call_test"
        try:
            await model.connect(options)
            await asyncio.wait_for(result_received.wait(), timeout=5)
            failures = [event for event in events if isinstance(event, RealtimeModelExceptionEvent)]
            text_events = [
                event for event in events if isinstance(event, RealtimeModelOutputTextDeltaEvent)
            ]
            if accepted:
                assert not failures
                assert len(text_events) == 1
                assert text_events[0].delta == payload["delta"]
            else:
                assert not text_events
                assert not any(isinstance(event, RealtimeModelRawServerEvent) for event in events)
                assert len(failures) == 1
                exception = failures[0].exception
                assert isinstance(exception, ConnectionClosedError)
                assert exception.sent is not None
                assert exception.sent.code == CloseCode.MESSAGE_TOO_BIG
                await asyncio.wait_for(peer_closed.wait(), timeout=5)
                assert peer_close_code == CloseCode.MESSAGE_TOO_BIG
        finally:
            await model.close()
        await asyncio.wait_for(peer_closed.wait(), timeout=5)

from __future__ import annotations

import json
import logging
import traceback
from typing import Any

import pytest
from openai import AsyncOpenAI

from _agents_codex import _responses_websocket as transport
from agents import Agent, ModelRequest, NativeProvider, ResponsesWebSocketModel
from tests._asyncio import timeout
from tests.test_native_responses_websocket_model import Socket as CompleteSocket
from tests.test_native_responses_websocket_review import _diagnostics

_PRIVATE = "synthetic-private-structural-frame-4566096155"


@pytest.mark.parametrize("kind", ["array", "keys", "depth"])
def test_structural_limit_precedes_json_materialization(kind, monkeypatch):
    # Modest inputs exercise the real default limits without exhausting memory.
    if kind == "array":
        frame = '{"padding":[' + "0," * 65_536 + "0]}"
    elif kind == "keys":
        frame = "{" + ",".join(f'"k{i}":0' for i in range(32_769)) + "}"
    else:
        frame = '{"padding":' + "[" * 128 + "0" + "]" * 128 + "}"
    entered_decoder = False
    original = json.loads

    def decode(*args, **kwargs):
        nonlocal entered_decoder
        entered_decoder = True
        return original(*args, **kwargs)

    monkeypatch.setattr(transport.json, "loads", decode)
    with pytest.raises(ValueError, match="invalid JSON frame"):
        transport._decode_socket_frame(frame, None)
    assert not entered_decoder


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("kind", ["array", "keys", "depth"])
async def test_structural_limit_rejects_without_retaining_frame_in_native_errors(
    streaming, kind, monkeypatch, caplog
):
    # A small private budget proves the public path for both native callbacks.
    monkeypatch.setattr(transport, "_MAX_SOCKET_JSON_NODES", 128, raising=False)

    class Socket(CompleteSocket):
        async def recv(self) -> str:
            event = json.loads(await super().recv())
            event["private"] = _PRIVATE
            if kind == "array":
                event["padding"] = [0] * 256
            elif kind == "keys":
                event["padding"] = {f"k{i}": 0 for i in range(128)}
            else:
                return json.dumps(event)[:-1] + ',"padding":' + "[" * 128 + "0" + "]" * 128 + "}"
            return json.dumps(event)

    socket = Socket()

    async def connect(request: ModelRequest, options: Any) -> Socket:
        return socket

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        async with NativeProvider(
            model_backend=ResponsesWebSocketModel(
                client, socket_factory=connect, websocket_options={"max_size": None}
            )
        ) as provider:
            async with Agent(
                model="gpt-4.1", instructions="Complete once.", provider=provider
            ).session() as session:
                turn = await session.submit("Synthetic request.")
                with pytest.raises(Exception) as caught:
                    if streaming:
                        async for _ in turn.events():
                            pass
                    await turn.get_result()
                reporter = _diagnostics(caught.value)
                logging.getLogger(__name__).error("Native structural failure: %s", reporter)
                assert _PRIVATE not in reporter and _PRIVATE not in caplog.text
                assert "invalid JSON frame" in "".join(traceback.format_exception(caught.value))
                assert len(socket.sent) == 1 and socket.closed == 1
        assert not client.is_closed()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("encoding", ["text", "utf-8", "utf-16", "utf-32"])
async def test_structural_limit_preserves_quoted_syntax_and_supported_encodings(
    streaming, encoding, monkeypatch
):
    monkeypatch.setattr(transport, "_MAX_SOCKET_JSON_NODES", 128, raising=False)
    quoted = '[{\\"quoted\\":null},0]' * 1024

    class Socket(CompleteSocket):
        async def recv(self) -> Any:
            event = json.loads(await super().recv())
            event["quoted"] = quoted
            value = json.dumps(event, ensure_ascii=False)
            return value if encoding == "text" else value.encode(encoding)

    socket = Socket()

    async def connect(request: ModelRequest, options: Any) -> Socket:
        return socket

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        async with NativeProvider(
            model_backend=ResponsesWebSocketModel(client, socket_factory=connect)
        ) as provider:
            async with Agent(
                model="gpt-4.1", instructions="Complete once.", provider=provider
            ).session() as session:
                turn = await session.submit("Synthetic request.")
                if streaming:
                    async for _ in turn.events():
                        pass
                assert (await turn.get_result()).final_output == "accepted"
        assert socket.closed == 1 and not client.is_closed()

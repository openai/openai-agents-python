from __future__ import annotations

import json
import logging
import traceback
from typing import Any

import pytest
from openai import AsyncOpenAI

from agents import Agent, HostedResponsesWebSocketModel, ModelRequest, NativeProvider
from tests._asyncio import timeout
from tests.test_native_responses_websocket_review import _diagnostics

_PRIVATE = "synthetic-private-hosted-size-4567095441"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("ceiling", ["cumulative", "configured"])
@pytest.mark.parametrize("encoding", ["characters", "multibyte", "surrogate"])
async def test_hosted_receive_checks_bounded_bytes_before_encoding_or_json_materialization(
    streaming, ceiling, encoding, monkeypatch, caplog
):
    from _agents_codex import (
        _hosted_responses_websocket as hosted,
        _responses_websocket as ordinary,
    )

    budget = 512
    encoded_whole_frames = []
    decoded_private_frames = []
    original_loads = json.loads

    class Frame(str):
        def encode(self, *args, **kwargs):
            encoded_whole_frames.append(len(self))
            return super().encode(*args, **kwargs)

    def decode(payload, *args, **kwargs):
        if isinstance(payload, str) and _PRIVATE in payload:
            decoded_private_frames.append(True)
        return original_loads(payload, *args, **kwargs)

    monkeypatch.setattr(ordinary.json, "loads", decode)

    class Socket:
        closed = 0
        sends = 0

        async def send(self, value: str) -> None:
            self.sends += 1
            if ceiling == "cumulative":
                # Leave a small receive quota after the real create was admitted.
                monkeypatch.setattr(
                    hosted, "_MAX_RESPONSE_BYTES", len(value.encode("utf-8")) + budget
                )

        async def recv(self) -> str:
            if encoding == "characters":
                padding = "x" * (budget + 1)
            elif encoding == "multibyte":
                padding = "😀" * (budget // 2)
            else:
                padding = "\ud800"
            return Frame('{"private":"' + _PRIVATE + '","padding":"' + padding + '"}')

        async def close(self) -> None:
            self.closed += 1

    socket = Socket()

    async def connect(request: ModelRequest, options: Any) -> Socket:
        return socket

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        async with NativeProvider(
            model_backend=HostedResponsesWebSocketModel(
                client,
                socket_factory=connect,
                websocket_options={"max_size": budget if ceiling == "configured" else None},
            )
        ) as provider:
            async with Agent(
                model="gpt-4.1", instructions="Answer.", provider=provider
            ).session() as session:
                turn = await session.submit("Public synthetic input.")
                with pytest.raises(Exception) as caught:
                    if streaming:
                        async for _ in turn.events():
                            pass
                    await turn.get_result()
                assert encoded_whole_frames == []
                assert decoded_private_frames == []
                report = _diagnostics(caught.value)
                logging.getLogger(__name__).error("Native hosted size refusal: %s", report)
                assert _PRIVATE not in report and _PRIVATE not in caplog.text
                assert "invalid frame size" in "".join(traceback.format_exception(caught.value))
                assert socket.sends == socket.closed == 1
        assert not client.is_closed()


def test_socket_frame_size_accepts_exact_multibyte_boundary_and_binary():
    from _agents_codex._responses_websocket import _socket_frame_size

    value = '{"text":"😀é"}'
    actual = len(value.encode("utf-8"))
    assert _socket_frame_size(value, actual) == actual
    assert _socket_frame_size(value.encode("utf-8"), actual) == actual
    with pytest.raises(ValueError, match="invalid frame size"):
        _socket_frame_size(value, actual - 1)

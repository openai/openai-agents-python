from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from openai import AsyncOpenAI

from agents import Agent, ModelRequest, NativeProvider, ResponsesWebSocketModel
from tests._asyncio import timeout

_PRIVATE = "synthetic-private-websocket-frame-4564742910"


def _diagnostics(error: BaseException) -> str:
    pending = [error]
    visited = set()
    output = []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        output.append(repr(vars(current)))
        trace = current.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            if (
                "/json/" in frame.f_code.co_filename
                or "_agents_codex" in Path(frame.f_code.co_filename).parts
            ):
                output.extend(
                    repr(frame.f_locals[key])
                    for key in (
                        "doc",
                        "s",
                        "payload",
                        "body",
                        "event",
                        "request",
                        "snapshot",
                        "selected",
                    )
                    if key in frame.f_locals
                )
            trace = trace.tb_next
        pending.extend(
            item
            for item in (
                current.__cause__,
                current.__context__,
                getattr(current, "model_error", None),
            )
            if isinstance(item, BaseException)
        )
        pending.extend(
            item for item in getattr(current, "exceptions", ()) if isinstance(item, BaseException)
        )
    return "\n".join(output)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "encoding", ["text", "binary", "invalid-utf8", "invalid-type", "depth", "integer"]
)
async def test_malformed_socket_frame_releases_decoder_data_from_public_native_errors_and_logs(
    streaming, encoding, caplog
) -> None:
    class Socket:
        closed = 0

        async def send(self, value: str) -> None:
            pass

        async def recv(self) -> Any:
            frame = '{"private":"' + _PRIVATE + '","malformed":'
            if encoding == "binary":
                return frame.encode()
            if encoding == "invalid-utf8":
                return frame.encode() + b"\xff"
            if encoding == "invalid-type":
                return {"private": _PRIVATE}
            if encoding == "depth":
                return '["' + _PRIVATE + '",' + "[" * 2000 + "0" + "]" * 2000 + "]"
            if encoding == "integer":
                return '{"private":"' + _PRIVATE + '","integer":' + "1" * 5000 + "}"
            return frame

        async def close(self) -> None:
            self.closed += 1

    socket = Socket()

    async def connect(request: ModelRequest, options: Any) -> Socket:
        return socket

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        model = ResponsesWebSocketModel(client, socket_factory=connect)
        async with NativeProvider(model_backend=model) as provider:
            async with Agent(
                model="gpt-4.1", instructions="Complete once.", provider=provider
            ).session() as session:
                turn = await session.submit("Synthetic request.")
                with pytest.raises(Exception) as caught:
                    if streaming:
                        async for _ in turn.events():
                            pass
                    await turn.get_result()
                error = caught.value
                logging.getLogger(__name__).error(
                    "Native transport failure: %s",
                    _diagnostics(error),
                    exc_info=(type(error), error, error.__traceback__),
                )
                assert _PRIVATE not in _diagnostics(error)
                assert _PRIVATE not in caplog.text
                assert socket.closed == 1 and not client.is_closed()


async def test_peer_closed_socket_reopens_before_native_tool_continuation() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import function_tool
    from tests.test_native_responses_websocket_model import events

    frames = []
    connections = []
    effects = []
    peer_closed = asyncio.Event()

    async def socket_route(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        connections.append(socket)
        async for message in socket:
            frame = json.loads(message.data)
            frames.append(frame)
            first = len(frames) == 1
            for event in events(len(frames), call=first):
                await socket.send_json(event)
            if first:
                await socket.close()
                peer_closed.set()
                break
        return socket

    @function_tool
    async def lookup() -> str:
        effects.append("lookup")
        await peer_closed.wait()
        return "found"

    app = web.Application()
    app.router.add_get("/v1/responses", socket_route)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            async with NativeProvider(model_backend=ResponsesWebSocketModel(client)) as provider:
                result = await Agent(
                    model="gpt-4.1", instructions="Use lookup.", tools=[lookup], provider=provider
                ).run("Run once.")
                assert result.final_output == "accepted"
            assert not client.is_closed()
    assert len(frames) == len(connections) == 2
    assert effects == ["lookup"]
    assert any(item["type"] == "function_call_output" for item in frames[1]["input"])


async def test_socket_send_and_cleanup_failures_retain_their_original_diagnostics() -> None:
    from agents import ModelRunOwner

    sending = OSError("synthetic send failure")
    closing = RuntimeError("synthetic close failure")

    class Socket:
        def send(self, value: str) -> None:
            raise sending

        async def recv(self) -> str:
            raise AssertionError("No response can precede a failed send")

        async def close(self) -> None:
            raise closing

    async def connect(request: ModelRequest, options: Any) -> Socket:
        return Socket()

    async with AsyncOpenAI(api_key="synthetic") as client:
        model = ResponsesWebSocketModel(client, socket_factory=connect)
        owner = ModelRunOwner("session", "turn")
        request = ModelRequest(
            {"model": "gpt-4.1", "input": []}, None, "session", "turn", run_owner=owner
        )
        with pytest.raises(RuntimeError, match="after send") as caught:
            await model.complete(request)
        assert caught.value.__cause__ is sending
        assert sending.__context__ is closing
        await model.cleanup_on_run_end(owner)

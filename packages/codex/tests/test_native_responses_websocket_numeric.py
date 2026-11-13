from __future__ import annotations

import json
import logging
import traceback
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import Agent, ModelRequest, ModelRunOwner, NativeProvider, ResponsesWebSocketModel
from tests._asyncio import timeout
from tests.test_native_responses_websocket_review import _diagnostics

_PRIVATE = "synthetic-private-numeric-frame-4565288164"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
async def test_nonfinite_frame_rejects_before_public_native_encoding_and_reporter_retention(
    number, streaming, caplog
):
    class Socket:
        closed = 0

        async def send(self, value: str) -> None:
            pass

        async def recv(self) -> str:
            return (
                '{"type":"response.created","response":{"id":"resp_numeric","private":"'
                + _PRIVATE
                + '","bad":'
                + number
                + "}}"
            )

        async def close(self) -> None:
            self.closed += 1

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
                with pytest.raises(Exception) as caught:
                    if streaming:
                        async for _ in turn.events():
                            pass
                    await turn.get_result()
                reporter = _diagnostics(caught.value)
                logging.getLogger(__name__).error("Native numeric failure: %s", reporter)
                assert _PRIVATE not in reporter and _PRIVATE not in caplog.text
                assert "invalid JSON frame" in "".join(traceback.format_exception(caught.value))
                assert socket.closed == 1 and not client.is_closed()


@pytest.mark.parametrize("streaming", [False, True])
async def test_default_socket_limits_real_oversized_frames_before_native_encoding(streaming):
    async def socket_route(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for _ in socket:
            await socket.send_json(
                {
                    "type": "response.created",
                    "response": {"id": "resp_oversized", "padding": "x" * (8 * 1024 * 1024)},
                }
            )
            break
        await socket.close()
        return socket

    app = web.Application()
    app.router.add_get("/v1/responses", socket_route)
    async with timeout(30), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            async with NativeProvider(model_backend=ResponsesWebSocketModel(client)) as provider:
                async with Agent(
                    model="gpt-4.1", instructions="Complete once.", provider=provider
                ).session() as session:
                    turn = await session.submit("Synthetic request.")
                    with pytest.raises(Exception) as caught:
                        if streaming:
                            async for _ in turn.events():
                                pass
                        await turn.get_result()
                    rendered = "".join(traceback.format_exception(caught.value))
                    assert "1009" in rendered and "8388608" in rendered
                    assert "encoder.py" not in rendered
                assert not client.is_closed()


@pytest.mark.parametrize("unlimited", [False, True])
async def test_custom_socket_receive_ceiling_preserves_explicit_unlimited_override(unlimited):
    from tests.test_native_responses_websocket_model import events

    captured = []

    class Socket:
        async def send(self, value: str) -> None:
            pass

        async def recv(self) -> str:
            event = events(1)[-1]
            event["padding"] = "x" * (8 * 1024 * 1024)
            return json.dumps(event)

        async def close(self) -> None:
            pass

    async def connect(request: ModelRequest, options: Any) -> Socket:
        captured.append(options["websocket_options"])
        return Socket()

    async with AsyncOpenAI(api_key="synthetic") as client:
        model = ResponsesWebSocketModel(
            client,
            socket_factory=connect,
            websocket_options={"max_size": None} if unlimited else None,
        )
        owner = ModelRunOwner("session", "turn")
        request = ModelRequest(
            {"model": "gpt-4.1", "input": []}, None, "session", "turn", run_owner=owner
        )
        if unlimited:
            assert (await model.complete(request)).output[0]["content"][0]["text"] == "accepted"
        else:
            with pytest.raises(RuntimeError, match="after send"):
                await model.complete(request)
        await model.cleanup_on_run_end(owner)
        assert captured == [{"max_size": None if unlimited else 8 * 1024 * 1024}]
        assert not client.is_closed()


@pytest.mark.parametrize("streaming", [False, True])
async def test_finite_fraction_and_large_numbers_survive_native_callback_views(streaming):
    from tests.test_native_responses_websocket_model import Socket as CompleteSocket

    class Socket(CompleteSocket):
        async def recv(self) -> str:
            event = json.loads(await super().recv())
            if "response" in event:
                event["response"]["finite_numbers"] = [0.125, 1e308, -1e308]
            return json.dumps(event)

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

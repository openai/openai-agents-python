from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider
from tests._asyncio import timeout


def events():
    item = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": "wire answer",
                "annotations": [],
                "logprobs": [],
            }
        ],
    }
    response = {
        "id": "resp_probe",
        "object": "response",
        "created_at": 1,
        "model": "gpt-5.5",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": True,
        "temperature": 1,
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1,
        "output": [item],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 1,
            "output_index": 0,
            "item": item,
        },
        {"type": "response.completed", "sequence_number": 2, "response": response},
    ]


@asynccontextmanager
async def peer(mode: str, *, advertised_frame: int | None = None):
    wire = {"upgrades": 0, "http": 0, "frames": 0, "pings": 0}
    tasks = []

    async def answer(socket):
        try:
            await socket.send_json(events()[0])
            if mode == "uncertain":
                await socket.close()
                return
            await asyncio.sleep(0.15)
            for event in events()[1:]:
                await socket.send_json(event)
        except (ConnectionError, RuntimeError):
            pass

    async def websocket(request):
        wire["upgrades"] += 1
        if mode == "upgrade426":
            return web.Response(status=426)
        if mode == "slow-connect":
            await asyncio.sleep(0.075)
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        async for message in socket:
            if message.type == WSMsgType.PING:
                wire["pings"] += 1
                if mode == "pong":
                    await socket.pong(message.data)
            elif message.type == WSMsgType.TEXT:
                frame = json.loads(message.data)
                if frame.get("generate") is False:
                    await socket.send_json(
                        {
                            "type": "response.completed",
                            "response": {"id": "resp_warmup", "output": []},
                        }
                    )
                else:
                    wire["frames"] += 1
                    if advertised_frame is not None:
                        size = advertised_frame
                        header = (
                            bytes((0x81, size))
                            if size < 126
                            else b"\x81\x7f" + size.to_bytes(8, "big")
                        )
                        request.transport.write(header)
                    else:
                        tasks.append(asyncio.create_task(answer(socket)))
        return socket

    async def http(request):
        wire["http"] += 1
        await request.json()
        return web.Response(
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events()),
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_get("/v1/responses", websocket)
    app.router.add_post("/v1/responses", http)
    async with TestServer(app, shutdown_timeout=0.1) as server:
        yield str(server.make_url("/v1")), wire
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_native_explicit_websocket_never_crosses_to_http(transport):
    async with peer("upgrade426") as (url, wire):
        provider = NativeProvider(
            base_url=url,
            api_key="synthetic-controls",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
        )
        agent = Agent(
            instructions="Answer.",
            model="gpt-5.5",
            provider=provider,
            model_settings={"request_transport": transport},
        )
        if transport == "http":
            assert (await agent.run("hello")).final_output == "wire answer"
            assert wire["upgrades"] == 0 and wire["http"] == 1
        else:
            with pytest.raises(NativeError):
                await agent.run("hello")
            assert wire["upgrades"] == 1 and wire["http"] == 0


@pytest.mark.parametrize(
    "mode,pong_timeout,succeeds",
    [("pong", 40, True), ("no-pong", 40, False), ("no-pong", None, True)],
)
async def test_native_websocket_ping_pong_options(mode, pong_timeout, succeeds):
    async with peer(mode) as (url, wire):
        provider = NativeProvider(
            base_url=url,
            api_key="synthetic-controls",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
            stream_idle_timeout_ms=1000,
            websocket_options={"ping_interval_ms": 20, "ping_timeout_ms": pong_timeout},
        )
        agent = Agent(
            instructions="Answer.",
            model="gpt-5.5",
            provider=provider,
            model_settings={"request_transport": "websocket"},
        )
        if succeeds:
            assert (await agent.run("hello")).final_output == "wire answer"
        else:
            with pytest.raises(NativeError):
                await agent.run("hello")
        assert wire["frames"] == 1 and wire["pings"] >= 1 and wire["http"] == 0


async def test_native_fixed_websocket_never_replays_an_uncertain_send():
    async with peer("uncertain") as (url, wire):
        provider = NativeProvider(
            base_url=url,
            api_key="synthetic-controls",
            supports_websockets=True,
            request_max_retries=3,
            stream_max_retries=3,
        )
        agent = Agent(
            instructions="Answer.",
            model="gpt-5.5",
            provider=provider,
            model_settings={"request_transport": "websocket"},
        )
        with pytest.raises(NativeError):
            await agent.run("hello")
        assert wire["frames"] == wire["upgrades"] == 1 and wire["http"] == 0


async def test_native_websocket_max_message_size_is_independent_from_idle_timeout():
    async with peer("pong") as (url, wire):
        provider = NativeProvider(
            base_url=url,
            api_key="synthetic-controls",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
            websocket_options={"max_message_bytes": 256},
        )
        with pytest.raises(NativeError):
            await Agent(
                instructions="Answer.",
                model="gpt-5.5",
                provider=provider,
                model_settings={"request_transport": "websocket"},
            ).run("hello")
        assert wire["frames"] == 1 and wire["http"] == 0


async def test_native_websocket_send_deadline_does_not_expire_a_longer_receive():
    async with peer("pong") as (url, wire):
        provider = NativeProvider(
            base_url=url,
            api_key="synthetic-controls",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
            stream_idle_timeout_ms=30,
            websocket_options={"send_timeout_ms": 30, "receive_timeout_ms": 500},
        )
        assert (
            await Agent(
                instructions="Answer.",
                model="gpt-5.5",
                provider=provider,
                model_settings={"request_transport": "websocket"},
            ).run("hello")
        ).final_output == "wire answer"
        assert wire["frames"] == 1 and wire["http"] == 0


async def test_native_websocket_connect_deadline_can_be_disabled_for_a_slow_handshake():
    async with peer("slow-connect") as (url, wire):
        provider = NativeProvider(
            base_url=url,
            api_key="synthetic-controls",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
            websocket_connect_timeout_ms=10,
            websocket_options={"connect_timeout_ms": None},
        )
        async with timeout(3):
            assert (
                await Agent(
                    instructions="Answer.",
                    model="gpt-5.5",
                    provider=provider,
                    model_settings={"request_transport": "websocket"},
                ).run("hello")
            ).final_output == "wire answer"
        assert wire["frames"] == 1 and wire["http"] == 0


async def test_native_websocket_receive_deadline_is_independent_from_send():
    async with peer("pong") as (url, wire):
        provider = NativeProvider(
            base_url=url,
            api_key="synthetic-controls",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
            stream_idle_timeout_ms=500,
            websocket_options={"send_timeout_ms": 500, "receive_timeout_ms": 20},
        )
        with pytest.raises(NativeError):
            await Agent(
                instructions="Answer.",
                model="gpt-5.5",
                provider=provider,
                model_settings={"request_transport": "websocket"},
            ).run("hello")
        assert wire["frames"] == 1 and wire["http"] == 0


@pytest.mark.parametrize("limit,bound", [(None, 16 << 20), (32 << 20, 16 << 20), (8, 8)])
async def test_native_frame_limit_rejects_a_header_before_its_missing_body(limit, bound):
    async with peer("frame-header", advertised_frame=bound + 1) as (url, wire):
        async with NativeProvider(
            base_url=url,
            api_key="synthetic-frame-bound",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
            stream_idle_timeout_ms=10000,
            websocket_options={"max_message_bytes": limit},
        ) as provider:
            agent = Agent(
                instructions="Answer.",
                model="gpt-5.5",
                provider=provider,
                model_settings={"request_transport": "websocket"},
            )
            task = asyncio.create_task(agent.run("hello"))
            try:
                done, _ = await asyncio.wait({task}, timeout=2)
                assert done, (
                    "Native socket must reject the oversized header before waiting for its body"
                )
                with pytest.raises(NativeError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert wire["frames"] == 1 and wire["http"] == 0

"""Hosted responses apply the active native handoff configuration."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    Handoff,
    HostedResponsesWebSocketModel,
    NativeError,
    NativeProvider,
    function_tool,
)
from tests._asyncio import timeout


@pytest.mark.parametrize("store", [False, True])
@pytest.mark.parametrize("old_response", ["paused", "closed-terminal", "closed-unsettled"])
async def test_hosted_handoff_replaces_source_configuration(store, old_response):
    frames = []
    sockets = []
    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []
    call = {
        "type": "function_call",
        "id": "fc_transfer",
        "call_id": "call_transfer",
        "name": "route_to_target",
        "arguments": "{}",
    }
    answer = {
        "type": "message",
        "id": "msg_destination",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "agent": {"agent_name": "/root"},
        "content": [{"type": "output_text", "text": "resolved"}],
    }

    async def route(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.append(socket)
        number = len(sockets)
        async for message in socket:
            frame = json.loads(message.data)
            frames.append((number, frame))
            if len(frames) == 1:
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_source"}}
                )
                await socket.send_json(
                    {"type": "response.output_item.done", "output_index": 0, "item": call}
                )
                continue
            broken = frame["type"] == "response.inject"
            if broken:
                await socket.send_json(
                    {"type": "response.inject.created", "response_id": "resp_source"}
                )
            else:
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_destination"}}
                )
            await socket.send_json(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_source" if broken else "resp_destination",
                        "status": "completed",
                        "store": store,
                        "output": [answer],
                    },
                }
            )
        return socket

    @function_tool
    async def route_to_target() -> str:
        effects.append("route")
        if old_response != "paused":
            entered.set()
            await release.wait()
        return "accepted transfer"

    @function_tool
    def destination_only() -> str:
        return "destination"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            backend = HostedResponsesWebSocketModel(client)
            async with NativeProvider(model_backend=backend) as provider:
                destination = Agent(instructions="DESTINATION_ONLY", tools=[destination_only])
                source = Agent(
                    instructions="SOURCE_ONLY",
                    model="gpt-5.5",
                    model_settings={"store": store},
                    provider=provider,
                    handoffs=[Handoff(destination, route_to_target)],
                )
                async with source.session() as session:
                    turn = await session.submit("Route this.")
                    if old_response != "paused":
                        await entered.wait()
                        if old_response == "closed-terminal":
                            await sockets[0].send_json(
                                {
                                    "type": "response.completed",
                                    "response": {
                                        "id": "resp_source",
                                        "status": "completed",
                                        "store": store,
                                        "output": [call],
                                        "usage": {
                                            "input_tokens": 2,
                                            "output_tokens": 3,
                                            "total_tokens": 5,
                                        },
                                    },
                                }
                            )
                        await sockets[0].close()
                        release.set()
                    if old_response == "closed-unsettled":
                        with pytest.raises((NativeError, ValueError), match="no genuine terminal"):
                            await turn.get_result()
                        assert len(sockets) == 1
                        assert len(frames) == 1
                        assert effects == ["route"]
                        return
                    result = await turn.get_result()
                    assert result.final_output == "resolved"
                    assert result.run_usage.requests == 2
                    if old_response == "closed-terminal":
                        assert result.run_usage.total_tokens == 5
    assert effects == ["route"]
    assert len(frames) == 2
    assert frames[1][0] == 2
    first = frames[0][1]
    second = frames[1][1]
    assert "SOURCE_ONLY" in json.dumps(first["input"])
    assert second["type"] == "response.create"
    assert "DESTINATION_ONLY" in json.dumps(second["input"])
    names = {tool.get("name") for tool in second["tools"]}
    assert "destination_only" in names
    assert "route_to_target" not in names
    assert second.get("previous_response_id") != "resp_source"
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call_transfer"
        for item in second["input"]
    )

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    HostedResponsesWebSocketModel,
    ModelRetrySettings,
    NativeError,
    NativeProvider,
    RetryDecision,
    function_tool,
)
from tests._asyncio import timeout


@pytest.mark.parametrize("dense_frames", [1, 3])
@pytest.mark.parametrize("observe", [False, True])
async def test_hosted_reconciliation_bounds_retained_raw_graph(dense_frames, observe):
    frames = []
    sockets = []
    effects = []
    entered = asyncio.Event()
    release = asyncio.Event()
    policies = []

    def policy(context):
        policies.append(context.request_attempt)
        return RetryDecision(retry=True, delay_ms=0, approve_unsafe_replay=True)

    call = {
        "type": "function_call",
        "id": "fc_scope",
        "call_id": "call_scope",
        "name": "lookup",
        "arguments": "{}",
    }

    async def route(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.append(socket)
        number = len(sockets)
        async for message in socket:
            frame = message.json()
            frames.append((number, frame))
            response_id = "resp_scope_source" if number == 1 else "resp_other"
            if frame["type"] == "response.inject":
                response_id = frame["response_id"]
                await socket.send_json(
                    {"type": "response.inject.created", "response_id": response_id}
                )
            else:
                await socket.send_json(
                    {"type": "response.created", "response": {"id": response_id}}
                )
            if number == 1:
                await socket.send_json(
                    {"type": "response.output_item.done", "output_index": 0, "item": call}
                )
            else:
                await socket.send_json(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": response_id,
                            "status": "completed",
                            "store": True,
                            "output": [
                                {
                                    "type": "message",
                                    "id": "msg_other",
                                    "role": "assistant",
                                    "status": "completed",
                                    "phase": "final_answer",
                                    "agent": {"agent_name": "/root"},
                                    "content": [{"type": "output_text", "text": "done"}],
                                }
                            ],
                        },
                    }
                )
        return socket

    @function_tool
    async def lookup() -> str:
        effects.append("lookup")
        entered.set()
        await release.wait()
        return "local result"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    error = None
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            backend = HostedResponsesWebSocketModel(client)
            async with NativeProvider(model_backend=backend) as provider:
                agent = Agent(
                    model="gpt-5.5",
                    instructions="Use lookup.",
                    tools=[lookup],
                    provider=provider,
                    model_settings={
                        "retry": ModelRetrySettings(
                            max_retries=1 if dense_frames > 1 and observe else 0, policy=policy
                        )
                    },
                )
                async with agent.session(include_raw_provider_events=observe) as session:
                    turn = await session.submit("Start.")
                    await entered.wait()
                    for _ in range(dense_frames):
                        await sockets[0].send_json(
                            {"type": "response.progress", "dense": [{}] * 24_000}
                        )
                    await sockets[0].send_json(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_scope_source",
                                "status": "completed",
                                "store": True,
                                "output": [call],
                            },
                        }
                    )
                    await sockets[0].close()
                    release.set()
                    try:
                        await turn.get_result()
                    except (NativeError, ValueError) as cause:
                        error = str(cause)
    if dense_frames == 1 or not observe:
        assert error is None
        assert len(sockets) == 2
        assert len(frames) == 2
    else:
        assert error is not None, "Excess decoded raw nodes were retained and accepted"
        assert "invalid JSON frame" in error or "retired" in error
        assert len(sockets) == 1
        assert len(frames) == 1
    assert effects == ["lookup"]
    assert len(policies) == (1 if dense_frames > 1 and observe else 0)

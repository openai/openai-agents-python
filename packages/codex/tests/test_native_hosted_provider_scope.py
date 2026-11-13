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


@pytest.mark.parametrize("changed", ["authorization", "query"])
@pytest.mark.parametrize("retry", [False, True])
async def test_hosted_continuation_rejects_changed_provider_scope(changed, retry):
    frames = []
    sockets = []
    scopes = []
    effects = []
    entered = asyncio.Event()
    release = asyncio.Event()
    option_calls = 0
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
        scopes.append(
            request.headers.get("Authorization")
            if changed == "authorization"
            else request.query.get("account")
        )
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

    def request_options(request):
        nonlocal option_calls
        option_calls += 1
        scope = "source" if option_calls == 1 else "destination"
        if changed == "authorization":
            return {"headers": {"Authorization": f"Bearer synthetic-{scope}-scope"}}
        return {"query": {"account": scope}}

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
            backend = HostedResponsesWebSocketModel(client, request_options=request_options)
            async with NativeProvider(model_backend=backend) as provider:
                agent = Agent(
                    model="gpt-5.5",
                    instructions="Use lookup.",
                    tools=[lookup],
                    provider=provider,
                    model_settings={
                        "retry": ModelRetrySettings(max_retries=1 if retry else 0, policy=policy)
                    },
                )
                async with agent.session() as session:
                    turn = await session.submit("Start.")
                    await entered.wait()
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
    if error is None:
        crossed = (
            len(frames) > 1
            and (frames[1][1].get("previous_response_id") or frames[1][1].get("response_id"))
            == "resp_scope_source"
        )
        crossed_scope = len(scopes) > 1 and scopes[0] != scopes[1]
        pytest.fail(
            f"Hosted response crossed provider scope: cursor={crossed}, scope={crossed_scope}"
        )
    assert "provider scope" in error
    assert "synthetic-source-scope" not in error
    assert "synthetic-destination-scope" not in error
    assert option_calls == 2 + len(policies)
    assert effects == ["lookup"]
    assert scopes == ["Bearer synthetic-source-scope" if changed == "authorization" else "source"]
    assert len(sockets) == 1
    assert len(frames) == 1

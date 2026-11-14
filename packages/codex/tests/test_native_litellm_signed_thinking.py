from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    LitellmModel,
    ModelRetrySettings,
    NativeProvider,
    SessionState,
    function_tool,
)
from tests._asyncio import timeout

BLOCKS = [
    {"type": "thinking", "thinking": "", "signature": "signed_empty"},
    {"type": "thinking", "thinking": "plan", "signature": "signed_plan"},
    {"type": "redacted_thinking", "data": "opaque_provider_block"},
]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cold_restore", [False, True])
@pytest.mark.parametrize("visible_text", [False, True])
async def test_native_litellm_signed_thinking_replays_before_the_tool_result(
    monkeypatch, streaming, cold_restore, visible_text
):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    litellm = pytest.importorskip("litellm")
    monkeypatch.setattr(litellm, "telemetry", False)
    requests = []
    effects = []
    tool = {
        "id": "call_lookup",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }

    async def route(request):
        body = await request.json()
        requests.append(body)
        first = len(requests) == 1
        message = {
            "role": "assistant",
            "content": ("before" if visible_text else None) if first else "complete",
        }
        if first:
            message.update(reasoning_content="plan", thinking_blocks=BLOCKS, tool_calls=[tool])
        if not body.get("stream"):
            return web.json_response(
                {
                    "id": f"chat_{len(requests)}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "claude-fixture",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if first else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        delta = {key: value for key, value in message.items() if key != "tool_calls"}
        if first:
            delta["tool_calls"] = [{"index": 0, **tool}]
        for part, finish in ((delta, None), ({}, "tool_calls" if first else "stop")):
            chunk = {
                "id": f"chat_{len(requests)}",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "claude-fixture",
                "choices": [{"index": 0, "delta": part, "finish_reason": finish}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    @function_tool
    def lookup() -> str:
        effects.append("once")
        return "tool_result"

    app = web.Application()
    app.router.add_post("/v1/chat/completions", route)
    async with timeout(20), TestServer(app) as server:
        base = str(server.make_url("/v1"))
        async with AsyncOpenAI(api_key="synthetic", base_url=base, max_retries=0) as client:

            async def create(**options):
                options.setdefault("num_retries", 0)
                return await litellm.acompletion(
                    **options,
                    client=client,
                    api_base=base,
                    api_key="synthetic",
                    allowed_openai_params=["reasoning_effort"],
                )

            backend = LitellmModel(
                SimpleNamespace(acompletion=create), "openai/claude-fixture", stream=streaming
            )
            async with NativeProvider(model_backend=backend) as provider:
                agent = Agent(
                    model="gpt-5.5",
                    instructions="Use lookup.",
                    tools=[lookup],
                    provider=provider,
                    model_settings={
                        "reasoning_effort": "high",
                        "retry": ModelRetrySettings(max_retries=0),
                    },
                )
                if cold_restore:
                    async with agent.session() as session:
                        turn = await session.submit("Look up the value.")
                        assert (await turn.get_result()).final_output == "complete"
                        saved = await session.save_state()
                    saved = SessionState.from_json(saved.to_json())
                    async with agent.session(state=saved) as session:
                        turn = await session.submit("Continue.")
                        assert (await turn.get_result()).final_output == "complete"
                else:
                    result = await agent.run("Look up the value.")
                    assert result.final_output == "complete"
    assert effects == ["once"]
    assert len(requests) == (3 if cold_restore else 2)
    assistant = next(message for message in requests[1]["messages"] if message.get("tool_calls"))
    assert assistant["thinking_blocks"] == BLOCKS
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["content"] == "tool_result"

    if cold_restore:
        replayed = [message for message in requests[2]["messages"] if "thinking_blocks" in message]
        assert len(replayed) == 1
        assert replayed[0]["thinking_blocks"] == BLOCKS

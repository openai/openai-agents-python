from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, AnyLLMModel, NativeProvider, function_tool
from tests._asyncio import timeout


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("disable_parallel", [False, True])
async def test_gemini_native_chat_tool_continuation(stream, disable_parallel):
    any_llm = pytest.importorskip("any_llm")
    pytest.importorskip("google.genai")
    requests = []
    effects = []

    @function_tool
    def lookup(query: str) -> str:
        effects.append(query)
        return "available"

    async def generate(request):
        body = await request.json()
        requests.append(body)
        part = (
            {"functionCall": {"id": "gemini-call", "name": "lookup", "args": {"query": "test"}}}
            if len(requests) == 1
            else {"text": "continued"}
        )
        result = {
            "candidates": [
                {"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}
            ],
            "modelVersion": "gemini-fixture",
            "responseId": f"gemini-response-{len(requests)}",
            "usageMetadata": {
                "promptTokenCount": 2,
                "candidatesTokenCount": 1,
                "totalTokenCount": 3,
            },
        }
        if stream:
            return web.Response(
                text=f"data: {json.dumps(result)}\n\n", content_type="text/event-stream"
            )
        return web.json_response(result)

    app = web.Application()
    app.router.add_post("/{prefix:.*}", generate)
    async with timeout(20), TestServer(app) as server:
        base = str(server.make_url("/")).rstrip("/")
        borrowed = any_llm.AnyLLM.create("gemini", api_key="synthetic", api_base=base)
        try:
            backend = AnyLLMModel(borrowed, model="gemini-fixture", api="auto", stream=stream)
            async with NativeProvider(base_url=base + "/unused", model_backend=backend) as provider:
                agent = Agent(
                    model="gemini-fixture",
                    provider=provider,
                    instructions="Use lookup.",
                    tools=[lookup],
                    **(
                        {"model_settings": {"parallel_tool_calls": False}}
                        if disable_parallel
                        else {}
                    ),
                )
                if disable_parallel:
                    with pytest.raises(
                        ValueError, match="Gemini Chat cannot disable parallel tool calls"
                    ):
                        await agent.run("check")
                    assert not requests and not effects
                    return
                result = await agent.run("check")
        finally:
            await borrowed.client.aio.aclose()
            borrowed.client.close()
    assert result.final_output == "continued"
    assert effects == ["test"] and len(requests) == 2
    replies = [
        msg for msg in requests[1]["contents"] if any("functionResponse" in p for p in msg["parts"])
    ]
    assert len(replies) == 1 and replies[0]["role"] == "user"
    assert replies[0]["parts"][0]["functionResponse"]["name"] == "lookup"

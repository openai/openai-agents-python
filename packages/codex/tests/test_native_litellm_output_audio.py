from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import Agent, LitellmModel, NativeProvider, function_tool
from tests._asyncio import timeout


async def test_litellm_output_audio_refuses_before_native_tool_dispatch(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    litellm = pytest.importorskip("litellm")
    monkeypatch.setattr(litellm, "telemetry", False)
    seen = []
    effects = []

    @function_tool
    def double(value: int) -> str:
        effects.append(value)
        return str(value * 2)

    async def completion(request):
        assert request.headers["Authorization"] == "Bearer synthetic"
        seen.append(await request.json())
        message: dict[str, Any] = {"role": "assistant", "content": "accepted"}
        if len(seen) == 1:
            message.update(
                audio={
                    "id": "audio_synthetic",
                    "data": "UklGRg==",
                    "transcript": "synthetic",
                    "expires_at": 1,
                },
                tool_calls=[
                    {
                        "id": "call_audio",
                        "type": "function",
                        "function": {"name": "double", "arguments": json.dumps({"value": 21})},
                    }
                ],
            )
        return web.json_response(
            {
                "id": f"chatcmpl_audio_{len(seen)}",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4.1",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(seen) == 1 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completion)
    async with timeout(30), TestServer(app) as server:
        base = str(server.make_url("/v1"))
        async with AsyncOpenAI(api_key="synthetic", base_url=base, max_retries=0) as client:

            async def create(**options):
                return await litellm.acompletion(
                    **options, client=client, api_base=base, api_key="synthetic", num_retries=0
                )

            backend = LitellmModel(SimpleNamespace(acompletion=create), "openai/gpt-4.1")
            async with NativeProvider(model_backend=backend) as provider:
                failure = None
                try:
                    await Agent(
                        model="gpt-4.1",
                        instructions="Use double.",
                        tools=[double],
                        provider=provider,
                    ).run("Double21.")
                except Exception as error:
                    failure = error
                assert effects == [], "Unsupported output audio must not dispatch a native tool"
                assert len(seen) == 1
                assert failure is not None and "output audio" in str(failure)
                assert not client.is_closed()

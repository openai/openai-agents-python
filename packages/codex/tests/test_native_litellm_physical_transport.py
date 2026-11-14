"""Explicit native LiteLLM headers reach its owned loopback transport."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import Agent, NativeProvider
from agents.models import LitellmModel


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_current_litellm_scope_reaches_physical_transport(stream, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    litellm = pytest.importorskip("litellm")
    monkeypatch.setattr(litellm, "telemetry", False)
    seen = []

    async def respond(request):
        seen.append(dict(request.headers))
        body = await request.json()
        assert body.get("stream", False) is stream
        assert "extra_headers" not in body
        common = {"id": "fixture-chat", "created": 1, "model": "gpt-5.5"}
        message = {"role": "assistant", "content": "Done."}
        if not stream:
            return web.json_response(
                {
                    **common,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                }
            )
        chunk = {
            **common,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": message, "finish_reason": "stop"}],
        }
        return web.Response(
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", respond)
    async with TestServer(app) as server:
        base = str(server.make_url("/v1"))
        async with AsyncOpenAI(api_key="synthetic-credential", base_url=base) as client:

            async def complete(**options):
                return await litellm.acompletion(
                    **options,
                    client=client,
                    api_base=base,
                    api_key="synthetic-credential",
                    num_retries=0,
                )

            adapter = LitellmModel(
                SimpleNamespace(acompletion=complete), "openai/gpt-5.5", stream=stream
            )
            async with NativeProvider(model_backend=adapter) as provider:
                agent = Agent(
                    model="gpt-5.5",
                    instructions="Answer.",
                    provider=provider,
                    model_settings={"extra_headers": {"X-Test-Route": "native-litellm-scope"}},
                )
                assert (await agent.run("Start.")).final_output == "Done."

    [headers] = seen
    lower = {name.lower(): value for name, value in headers.items()}
    assert lower["x-test-route"] == "native-litellm-scope"
    assert lower["authorization"] == "Bearer synthetic-credential"

"""Explicit native LiteLLM headers reach its owned loopback transport."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI, omit

from agents import Agent, NativeProvider
from agents.models import LitellmModel


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("removed", [None, omit], ids=["none", "openai-omit"])
async def test_litellm_can_remove_borrowed_default_headers(stream, removed, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    litellm = pytest.importorskip("litellm")
    monkeypatch.setattr(litellm, "suppress_debug_info", True)
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
        async with AsyncOpenAI(
            api_key="synthetic-credential",
            base_url=base,
            default_headers={"X-Test-Remove": "default", "X-Test-Keep": "default-keep"},
        ) as client:
            original = litellm.acompletion

            async def complete(**options):
                return await original(
                    **options,
                    client=client,
                    api_base=base,
                    api_key="synthetic-credential",
                    num_retries=0,
                )

            monkeypatch.setattr(litellm, "acompletion", complete)
            adapter = LitellmModel(litellm, "openai/gpt-5.5", stream=stream)
            async with NativeProvider(model_backend=adapter) as provider:
                agent = Agent(
                    model="gpt-5.5",
                    instructions="Answer.",
                    provider=provider,
                    model_settings={
                        "extra_headers": {
                            "X-Test-Route": "native-litellm-scope",
                            "x-test-remove": removed,
                        }
                    },
                )
                assert (await agent.run("Start.")).final_output == "Done."

    [headers] = seen
    lower = {name.lower(): value for name, value in headers.items()}
    assert lower["x-test-route"] == "native-litellm-scope"
    assert lower["authorization"] == "Bearer synthetic-credential"
    assert lower["x-test-keep"] == "default-keep"
    assert "x-test-remove" not in lower


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "stock", "remapped"),
    [
        ("openai/gpt-4o-mini", False, False),
        ("openai/alias", True, True),
        ("anthropic/claude-sonnet-4-20250514", True, False),
    ],
    ids=["custom-client", "stock-alias", "non-openai"],
)
async def test_litellm_unverified_transports_keep_concrete_headers(
    model, stock, remapped, monkeypatch
):
    from types import SimpleNamespace

    from agents.models import ModelRequest

    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    litellm = pytest.importorskip("litellm")
    if remapped:
        monkeypatch.setattr(
            litellm, "model_alias_map", {model: "anthropic/claude-sonnet-4-20250514"}
        )

    async def complete(**options):
        assert options["extra_headers"] == {"X-Test-Keep": "keep"}
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "fixture-chat",
                "choices": [{"index": 0, "message": {"content": "done"}, "finish_reason": "stop"}],
            }
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    client = litellm if stock else SimpleNamespace(acompletion=complete)
    adapter = LitellmModel(client, model)
    request = ModelRequest({"model": "fixture", "input": []}, None, "fixture", "turn")
    object.__setattr__(
        request,
        "_request_transport",
        {"headers": {"x-test-remove": None, "X-Test-Keep": "keep"}},
    )
    result = await adapter.complete(request)
    assert result.output[0]["content"][0]["text"] == "done"

"""Per-request client controls keep Core's existing native tool/attempt owner."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI, InternalServerError

from agents import (
    Agent,
    ChatCompletionsModel,
    NativeProvider,
    ResponsesModel,
    function_tool,
)


@pytest.mark.parametrize("adapter", [ChatCompletionsModel, ResponsesModel])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_transport_retry_budget_inside_native_loop(adapter, exhausted):
    calls = 0
    factories = 0
    effects = []
    ids = []

    async def handle(req):
        nonlocal calls
        calls += 1
        assert req.headers["x-native-request"] == str(factories)
        assert req.query["request"] == str(factories)
        if exhausted or calls == 1:
            return web.json_response(
                {"error": {"message": "retry"}}, status=500, headers={"retry-after-ms": "1"}
            )
        invoke = calls == 2
        if adapter is ChatCompletionsModel:
            return web.json_response(
                {
                    "id": f"chat_{calls}",
                    "choices": [
                        {
                            "finish_reason": "tool_calls" if invoke else "stop",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_lookup",
                                        "type": "function",
                                        "function": {"name": "lookup", "arguments": '{"value":2}'},
                                    }
                                ],
                            }
                            if invoke
                            else {"role": "assistant", "content": "ok"},
                        }
                    ],
                }
            )
        return web.json_response(
            {
                "id": f"resp_{calls}",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_lookup",
                        "call_id": "call_lookup",
                        "name": "lookup",
                        "arguments": '{"value":2}',
                    }
                ]
                if invoke
                else [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
            }
        )

    def select(request):
        nonlocal factories
        factories += 1
        ids.append(request.session_id)
        return {
            "headers": {"x-native-request": str(factories)},
            "query": {"request": factories},
            "timeout_ms": 1000,
            "max_retries": 1,
        }

    @function_tool
    def lookup(value: int) -> str:
        """Lookup one value."""
        effects.append(value)
        return str(value)

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="fixture", base_url=str(server.make_url("/v1/")), max_retries=0
        ) as client:
            backend = adapter(client, request_options=select)
            async with NativeProvider(
                base_url="https://unused.invalid/v1", model_backend=backend
            ) as provider:
                agent = Agent(
                    model="gpt-4.1",
                    instructions="Use lookup once.",
                    tools=[lookup],
                    provider=provider,
                )
                if exhausted:
                    with pytest.raises(InternalServerError):
                        await agent.run("Lookup2.")
                    assert calls == 2
                    assert factories == 1
                    assert effects == []
                else:
                    assert (await agent.run("Lookup2.")).final_output == "ok"
                    assert calls == 3
                    assert factories == 2
                    assert effects == [2]
                    assert ids[0] == ids[1]
                assert client.max_retries == 0
                assert not client.is_closed()

"""Physical preterminal stream failures retain retry context and measured usage."""

import asyncio
import json
from contextlib import AsyncExitStack

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from tests._asyncio import timeout

from agents import (
    Agent,
    AnyLLMModel,
    ChatCompletionsModel,
    ModelRetrySettings,
    NativeProvider,
    RetryDecision,
    SessionState,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["openai", "anyllm"])
async def test_partial_chat_usage_survives_physical_preterminal_retry_and_cold_state(adapter):
    model_type = AnyLLMModel if adapter == "anyllm" else ChatCompletionsModel
    calls, seen, advice = [], [], []

    def chunk(identifier, choices, usage=None):
        return {
            "id": identifier,
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-5.5",
            "choices": choices,
            "usage": usage,
        }

    def event(value):
        return "data: " + json.dumps(value) + "\n\n"

    async def handle(request):
        calls.append(await request.json())
        if len(calls) == 1:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                event(
                    chunk(
                        "partial",
                        [],
                        {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    )
                ).encode()
            )
            transport = request.transport
            assert transport is not None
            await asyncio.sleep(0.03)
            transport.abort()
            return response
        chunks = [
            chunk(
                "recovered",
                [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "recovered"},
                        "finish_reason": None,
                    }
                ],
            ),
            chunk("recovered", [{"index": 0, "delta": {}, "finish_reason": "stop"}]),
            chunk(
                "recovered", [], {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
            ),
        ]
        return web.Response(
            text="".join(event(value) for value in chunks) + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    class ObservedChat(model_type):
        def get_retry_advice(self, request):
            advice.append(request)
            return super().get_retry_advice(request)

    def retry(context):
        seen.append(context)
        return RetryDecision(not context.response_started, delay_ms=0)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    async with (
        timeout(20),
        TestServer(app, shutdown_timeout=1) as server,
        AsyncExitStack() as clients,
    ):
        base_url = str(server.make_url("/v1"))
        transport = await clients.enter_async_context(DefaultAsyncHttpxClient(trust_env=False))
        if adapter == "anyllm":
            any_llm = pytest.importorskip("any_llm")
            client = any_llm.AnyLLM.create(
                "openai",
                api_base=base_url,
                api_key="synthetic",
                max_retries=0,
                http_client=transport,
            )
            clients.push_async_callback(client.client.close)
        else:
            client = await clients.enter_async_context(
                AsyncOpenAI(
                    base_url=base_url, api_key="synthetic", max_retries=0, http_client=transport
                )
            )
        async with NativeProvider(model_backend=ObservedChat(client, stream=True)) as provider:
            agent = Agent(
                instructions="Reply",
                model="gpt-5.5",
                provider=provider,
                model_settings={"retry": ModelRetrySettings(max_retries=1, policy=retry)},
            )
            async with agent.session() as session:
                turn = await session.submit("go")
                result = await turn.get_result()
                assert result.final_output == "recovered"
                assert result.run_usage.requests == 2
                assert result.run_usage.total_tokens == 21
                assert result.run_usage.missing_usage_requests == 0
                detail = [row async for row in session.turns()][-1]
                entries = detail["request_usage_entries"]
                cold = SessionState.from_json((await session.save_state()).to_json())
            async with agent.session(state=cold) as restored:
                restored_detail = [row async for row in restored.turns()][-1]
                assert restored_detail["request_usage_entries"] == entries

    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(seen) == len(advice) == 1
    (context,) = seen
    assert context.error is advice[0].error
    assert context.stream is advice[0].stream is True
    assert context.usage is not None and context.usage.total_tokens == 7
    assert context.normalized.is_network_error is True
    assert context.response_started is False and context.retry_unsafe_output is False
    assert [(row["scope"], row["total_tokens"]) for row in entries] == [
        ("sdk_model_attempt", 7),
        ("sdk_model_attempt", 14),
    ]
    assert entries[0]["attempt_id"] == context.attempt_id
    print(
        "PHYSICAL_CONTEXT",
        type(context.error).__name__,
        context.normalized,
        context.response_started,
        context.retry_unsafe_output,
        [(row["scope"], row["total_tokens"]) for row in entries],
    )

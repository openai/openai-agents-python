"""Borrowed OpenAI retry advice crosses the public native model boundary."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from agents import (
    Agent,
    ChatCompletionsModel,
    ModelRetrySettings,
    NativeProvider,
    ResponsesModel,
    RetryBackoffSettings,
    RetryDecision,
    retry_policies,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["responses", "chat"])
@pytest.mark.parametrize("suggested", [True, False])
async def test_borrowed_openai_provider_header_advice(kind, suggested):
    attempts = []

    async def handle(request):
        attempts.append(await request.json())
        if len(attempts) == 1:
            return web.json_response(
                {
                    "error": {
                        "message": "synthetic retry advice",
                        "type": "invalid_request_error",
                        "code": "provider_hint",
                    }
                },
                status=400 if suggested else 503,
                headers={"x-should-retry": str(suggested).lower()},
            )
        if kind == "responses":
            return web.json_response(
                {
                    "id": "response-recovered",
                    "object": "response",
                    "created_at": 1,
                    "model": "gpt-5.5",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "message-recovered",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "recovered", "annotations": []}
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            )
        return web.json_response(
            {
                "id": "chat-recovered",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-5.5",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "recovered"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    app.router.add_post("/v1/chat/completions", handle)
    async with (
        TestServer(app) as server,
        AsyncOpenAI(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-key",
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(trust_env=False),
        ) as client,
    ):
        adapter = ResponsesModel(client) if kind == "responses" else ChatCompletionsModel(client)
        async with NativeProvider(model_backend=adapter) as provider:
            agent = Agent(
                model="gpt-5.5",
                instructions="Reply.",
                provider=provider,
                model_settings={
                    "retry": ModelRetrySettings(
                        max_retries=1,
                        policy=retry_policies.provider_suggested(),
                        backoff=RetryBackoffSettings(
                            initial_delay_ms=0,
                            max_delay_ms=0,
                            jitter=False,
                        ),
                    )
                },
            )
            if suggested:
                assert (await agent.run("Reply.")).final_output == "recovered"
            else:
                with pytest.raises(Exception, match="synthetic retry advice"):
                    await agent.run("Reply.")
    assert len(attempts) == (2 if suggested else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["responses", "chat"])
async def test_borrowed_openai_http_date_advice(kind):
    attempts = []
    captured = []

    async def handle(request):
        attempts.append(await request.json())
        deadline = datetime.now(timezone.utc) + timedelta(seconds=120)
        return web.json_response(
            {
                "error": {
                    "message": "synthetic dated advice",
                    "type": "invalid_request_error",
                    "code": "provider_date",
                }
            },
            status=400,
            headers={"Retry-After": format_datetime(deadline, usegmt=True)},
        )

    def policy(context):
        captured.append(context.provider_advice)
        return RetryDecision(False)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    app.router.add_post("/v1/chat/completions", handle)
    async with (
        TestServer(app) as server,
        AsyncOpenAI(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-key",
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(trust_env=False),
        ) as client,
    ):
        adapter = ResponsesModel(client) if kind == "responses" else ChatCompletionsModel(client)
        async with NativeProvider(model_backend=adapter) as provider:
            agent = Agent(
                model="gpt-5.5",
                instructions="Reply.",
                provider=provider,
                model_settings={"retry": ModelRetrySettings(max_retries=1, policy=policy)},
            )
            with pytest.raises(Exception, match="synthetic dated advice"):
                await agent.run("Reply.")
    assert len(attempts) == 1 and len(captured) == 1
    assert captured[0].suggested is None
    assert 100_000 < captured[0].retry_after_ms <= 120_000

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, ModelRetrySettings, NativeError, NativeProvider, RetryDecision


@pytest.mark.parametrize("stream", [False, True])
async def test_retry_policy_observes_failed_model_request_mode(stream: bool) -> None:
    original = RuntimeError("producer failed before a response")
    observed: list[tuple[object, object]] = []
    advised: list[tuple[object, object]] = []

    class Finite:
        def get_retry_advice(self, request):
            advised.append((request.stream, request.error))
            return None

        def complete(self, request):
            raise original

    class Streaming(Finite):
        def stream(self, request):
            raise original

    def policy(context):
        observed.append((getattr(context, "stream", None), context.error))
        return RetryDecision(False)

    async with NativeProvider(model_backend=Streaming() if stream else Finite()) as provider:
        agent = Agent(
            model="fixture",
            instructions="Respond.",
            provider=provider,
            model_settings={"retry": ModelRetrySettings(max_retries=1, policy=policy)},
        )
        with pytest.raises(RuntimeError) as caught:
            await agent.run("Begin.")
    assert caught.value is original
    assert observed == [(stream, original)]
    assert advised == [(stream, original)]


@pytest.mark.parametrize("mode, stream", [("buffered", False), ("streaming", True)])
async def test_direct_http_retry_policy_observes_response_mode(mode, stream):
    attempts = []
    observed = []

    async def handle(request):
        attempts.append(await request.json())
        return web.json_response(
            {"error": {"message": "synthetic failed HTTP request"}}, status=503
        )

    def policy(context):
        observed.append((context.transport, context.stream))
        return RetryDecision(False)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with TestServer(app) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-key",
            supports_websockets=False,
            response_mode=mode,
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider:
            agent = Agent(
                model="gpt-5.5",
                instructions="Reply.",
                provider=provider,
                model_settings={"retry": ModelRetrySettings(max_retries=1, policy=policy)},
            )
            with pytest.raises(NativeError):
                await agent.run("Reply.")
    assert len(attempts) == 1
    assert observed == [("responses_sse" if stream else "responses_json", stream)]

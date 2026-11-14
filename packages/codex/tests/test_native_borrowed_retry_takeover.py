"""Core becomes the only retry owner after the first borrowed model attempt."""

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import DefaultAsyncHttpxClient

from agents import (
    Agent,
    AnyLLMModel,
    LitellmModel,
    ModelRequest,
    ModelRetrySettings,
    NativeProvider,
    RetryBackoffSettings,
    RetryDecision,
)
from tests._asyncio import timeout


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_anyllm_native_retry_reuses_borrowed_transport_without_retrying(api, stream):
    any_llm = pytest.importorskip("any_llm")
    sends = []
    policies = []

    async def handle(request):
        sends.append(request.headers["x-stainless-retry-count"])
        return web.json_response(
            {"error": {"message": "synthetic retry ownership", "type": "server_error"}},
            status=503,
            headers={"retry-after-ms": "1"},
        )

    def policy(context):
        policies.append(context.attempt)
        return RetryDecision(retry=True)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    app.router.add_post("/v1/chat/completions", handle)
    async with timeout(15), TestServer(app) as server:
        client = any_llm.AnyLLM.create(
            "openai",
            api_base=str(server.make_url("/v1")),
            api_key="synthetic",
            max_retries=1,
            http_client=DefaultAsyncHttpxClient(trust_env=False),
        )
        try:
            adapter = AnyLLMModel(client, api=api, stream=stream)
            async with NativeProvider(model_backend=adapter) as provider:
                agent = Agent(
                    model="gpt-5.5",
                    instructions="Reply.",
                    provider=provider,
                    model_settings={
                        "retry": ModelRetrySettings(
                            max_retries=1,
                            policy=policy,
                            backoff=RetryBackoffSettings(
                                initial_delay_ms=0, max_delay_ms=0, jitter=False
                            ),
                        )
                    },
                )
                with pytest.raises(Exception, match="synthetic retry ownership"):
                    await agent.run("Reply.")
            assert client.client.max_retries == 1
            assert not client.client.is_closed()
        finally:
            await client.client.close()

    assert len(policies) == 1
    assert sends == ["0", "1", "0"]


async def test_litellm_overrides_both_retry_controls_only_on_native_replay():
    calls = []

    class BorrowedRouter:
        async def acompletion(self, **options):
            calls.append((options.get("num_retries"), options.get("max_retries")))
            raise TimeoutError("synthetic Router retry ownership")

    async with (
        timeout(15),
        NativeProvider(model_backend=LitellmModel(BorrowedRouter())) as provider,
    ):
        agent = Agent(
            model="gpt-5.5",
            instructions="Reply.",
            provider=provider,
            model_settings={
                "retry": ModelRetrySettings(
                    max_retries=1,
                    policy=lambda _: RetryDecision(retry=True),
                    backoff=RetryBackoffSettings(initial_delay_ms=0, max_delay_ms=0, jitter=False),
                )
            },
        )
        with pytest.raises(Exception, match="synthetic Router retry ownership"):
            await agent.run("Reply.")

    assert calls == [(None, None), (0, 0)]


async def test_anyllm_opaque_provider_is_not_dispatched_when_core_owns_retry():
    class OpaqueProvider:
        async def acompletion(self, **kwargs):
            pytest.fail("An opaque provider must not send the retry")

    adapter = AnyLLMModel(OpaqueProvider())
    request = ModelRequest(
        body={"model": "gpt-5.5", "input": []},
        context=None,
        session_id="session",
        turn_id="turn",
        disable_provider_retries=True,
    )
    object.__setattr__(
        request, "_request_transport", {"headers": {"x-test-private": "owned-request-canary"}}
    )
    with pytest.raises(TypeError, match="cannot suppress borrowed client retries") as failure:
        await adapter.complete(request)

    traceback = failure.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_name == "_anyllm_provider_for_attempt":
            assert (
                not {"request", "provider", "client", "replay_provider", "replay_client"}
                & frame.f_locals.keys()
            )
            assert "owned-request-canary" not in repr(frame.f_locals)
        traceback = traceback.tb_next


async def test_litellm_native_retry_preserves_first_provider_retry_only(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    litellm = pytest.importorskip("litellm")
    monkeypatch.setattr(litellm, "telemetry", False)
    sends = []
    attempts = []

    async def handle(request):
        sends.append((len(attempts) - 1, request.headers.get("x-stainless-retry-count")))
        return web.json_response(
            {"error": {"message": "synthetic LiteLLM retry ownership", "type": "server_error"}},
            status=503,
            headers={"retry-after-ms": "1"},
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    async with timeout(20), TestServer(app) as server:
        base = str(server.make_url("/v1"))

        async def acompletion(**options):
            options.setdefault("num_retries", 1)
            options.setdefault("max_retries", 1)
            attempts.append((options["num_retries"], options["max_retries"]))
            return await litellm.acompletion(**options, api_base=base, api_key="synthetic")

        adapter = LitellmModel(SimpleNamespace(acompletion=acompletion), "openai/gpt-4.1")
        async with NativeProvider(model_backend=adapter) as provider:
            agent = Agent(
                model="gpt-4.1",
                instructions="Reply.",
                provider=provider,
                model_settings={
                    "retry": ModelRetrySettings(
                        max_retries=1,
                        policy=lambda _: RetryDecision(retry=True),
                        backoff=RetryBackoffSettings(
                            initial_delay_ms=0, max_delay_ms=0, jitter=False
                        ),
                    )
                },
            )
            with pytest.raises(Exception, match="synthetic LiteLLM retry ownership"):
                await agent.run("Reply.")
    assert attempts == [(1, 1), (0, 0)]
    first_attempt = [retry for attempt, retry in sends if attempt == 0]
    native_retry = [retry for attempt, retry in sends if attempt == 1]
    # LiteLLM versions may add their own outer retry to the first adapter call.
    # Core's replay must make exactly one HTTP request with SDK retries disabled.
    assert first_attempt[:2] == ["0", "1"]
    assert native_retry == ["0"]

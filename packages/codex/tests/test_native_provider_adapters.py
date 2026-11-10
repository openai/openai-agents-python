"""Borrowed provider libraries remain inside the native turn and tool owner."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    AnyLLMModel,
    ChatCompletionsModel,
    LitellmModel,
    NativeError,
    NativeProvider,
    ResponsesModel,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout


async def _exercise_borrowed_provider(
    adapter,
    outcome,
    monkeypatch,
    *,
    cache_key=None,
    check_cache_key=False,
    penalties=False,
    reasoning=False,
):
    seen = []
    effects = []
    fallback = []
    entered = asyncio.Event()
    release = asyncio.Event()

    @function_tool
    def double(value: int) -> str:
        effects.append(value)
        return str(value * 2)

    async def create(request):
        body = await request.json()
        seen.append(body)
        entered.set()
        if outcome == "cancel":
            await release.wait()
        final = len(seen) > 1
        if final:
            assert "42" in str(body.get("input", body.get("messages")))
            if reasoning:
                replayed = next(item for item in body["input"] if item.get("type") == "reasoning")
                assert replayed["encrypted_content"] == "synthetic-opaque-reasoning"
                assert replayed["id"] == "rs_synthetic"
        if adapter == "responses":
            output = (
                [
                    {
                        "type": "message",
                        "id": "msg_result",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "42", "annotations": []}],
                    }
                ]
                if final
                else [
                    {
                        "type": "function_call",
                        "id": "fc_double",
                        "status": "completed",
                        "call_id": "call_double",
                        "name": "double",
                        "arguments": '{"value":21}',
                    }
                ]
            )
            if reasoning and not final:
                output.insert(
                    0,
                    {
                        "id": "rs_synthetic",
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "synthetic-opaque-reasoning",
                    },
                )
            if outcome == "refusal":
                output.append(
                    {
                        "type": "message",
                        "id": "msg_refusal",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "synthetic-private-refusal"}],
                    }
                )
            return web.json_response(
                {
                    "id": f"resp_{len(seen)}",
                    "object": "response",
                    "created_at": 0,
                    "model": "gpt-4.1",
                    "status": "incomplete" if outcome == "incomplete" else "completed",
                    "output": output,
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "total_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 1},
                        "output_tokens_details": {"reasoning_tokens": 1},
                    },
                }
            )
        message = {"role": "assistant", "content": "42" if final else None}
        if not final:
            message["tool_calls"] = [
                {
                    "id": "call_double",
                    "type": "function",
                    "function": {"name": "double", "arguments": '{"value":21}'},
                }
            ]
        if outcome == "refusal":
            message["refusal"] = "synthetic-private-refusal"
        return web.json_response(
            {
                "id": f"chatcmpl_{len(seen)}",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4.1",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "length"
                        if outcome == "incomplete"
                        else "stop"
                        if final
                        else "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        )

    async def unexpected(request):
        fallback.append(request.path)
        return web.json_response(
            {"error": {"message": "Unexpected native HTTP fallback"}}, status=500
        )

    app = web.Application()
    app.router.add_post("/v1/responses", create)
    app.router.add_post("/v1/chat/completions", create)
    app.router.add_route("*", "/fallback/{tail:.*}", unexpected)
    async with timeout(30), TestServer(app) as server:
        base = str(server.make_url("/v1"))
        async with AsyncOpenAI(api_key="synthetic", base_url=base, max_retries=0) as client:
            options = {} if cache_key is None else {"prompt_cache_key": cache_key}
            if adapter == "responses":
                backend = ResponsesModel(client, **options)
            elif adapter == "chat":
                backend = ChatCompletionsModel(client, **options)
            elif adapter == "litellm":
                monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
                litellm = pytest.importorskip("litellm")
                monkeypatch.setattr(litellm, "telemetry", False)
                monkeypatch.setattr(litellm, "api_base", base)
                monkeypatch.setattr(litellm, "api_key", "synthetic")
                backend = LitellmModel(litellm, "openai/gpt-4.1", **options)
            else:
                any_llm = pytest.importorskip("any_llm")
                borrowed = any_llm.AnyLLM.create(
                    "openai", api_key="synthetic", api_base=base, max_retries=0
                )
                backend = AnyLLMModel(borrowed, "gpt-4.1", **options)
            async with NativeProvider(
                base_url=str(server.make_url("/fallback/v1")), model_backend=backend
            ) as provider:
                agent = Agent(
                    model="gpt-4.1",
                    instructions="Use double.",
                    tools=[double],
                    provider=provider,
                    **(
                        {
                            "model_settings": {
                                "extra_body": {"frequency_penalty": 0.4, "presence_penalty": 0.2}
                            }
                        }
                        if penalties
                        else {}
                    ),
                )
                async with agent.session() as session:
                    turn = await session.submit("Double21.")
                    if outcome == "cancel":
                        await entered.wait()
                        stopping = asyncio.create_task(turn.cancel())
                        try:
                            await asyncio.sleep(0)
                        finally:
                            release.set()
                            await stopping
                        with pytest.raises(TurnStoppedError):
                            await turn.get_result()
                    elif outcome == "incomplete":
                        with pytest.raises(
                            ValueError,
                            match="completed response|finish_reason=length",
                        ):
                            await turn.get_result()
                    elif outcome == "refusal":
                        with pytest.raises(NativeError) as caught:
                            await turn.get_result()
                        assert caught.value.response_integrity_reason == "provider_refusal"
                        assert "synthetic-private-refusal" not in str(caught.value)
                    else:
                        result = await turn.get_result()
                        assert result.final_output == "42"
                        assert result.thread_usage is not None
                        assert result.thread_usage.total_token_usage.input_tokens == 6
                        assert result.thread_usage.total_token_usage.output_tokens == 4
            assert effects == ([21] if outcome == "complete" else [])
            assert len(seen) == (2 if outcome == "complete" else 1)
            assert fallback == []
            assert seen[0].get("stream", False) is False
            if adapter == "responses":
                assert not client.is_closed()

            if penalties:
                assert all(body["frequency_penalty"] == 0.4 for body in seen)
                assert all(body["presence_penalty"] == 0.2 for body in seen)
            if check_cache_key:
                if cache_key is None:
                    # Older LiteLLM releases omit an unsupported inherited keyword.
                    for body in seen:
                        value = body.get("prompt_cache_key")
                        assert (value is None and adapter == "litellm") or (
                            isinstance(value, str) and bool(value)
                        )
                else:
                    assert all(body["prompt_cache_key"] == cache_key for body in seen)


@pytest.mark.parametrize("adapter", ["responses", "litellm", "any_llm"])
@pytest.mark.parametrize("outcome", ["complete", "incomplete", "refusal", "cancel"])
async def test_borrowed_provider_library_retains_native_ownership(adapter, outcome, monkeypatch):
    await _exercise_borrowed_provider(adapter, outcome, monkeypatch)


@pytest.mark.parametrize("adapter", ["chat", "responses", "litellm", "any_llm"])
@pytest.mark.parametrize(
    "cache_key", [None, "application-cache-key", ""], ids=["inherited", "explicit", "empty"]
)
async def test_native_provider_cache_key_retains_native_ownership(adapter, cache_key, monkeypatch):
    await _exercise_borrowed_provider(
        adapter, "complete", monkeypatch, cache_key=cache_key, check_cache_key=True
    )


@pytest.mark.parametrize("adapter", ["chat", "litellm", "any_llm"])
async def test_native_chat_penalties_reach_borrowed_clients(adapter, monkeypatch):
    await _exercise_borrowed_provider(adapter, "complete", monkeypatch, penalties=True)


async def test_responses_reasoning_replays_through_native_tool_continuation(monkeypatch):
    await _exercise_borrowed_provider("responses", "complete", monkeypatch, reasoning=True)

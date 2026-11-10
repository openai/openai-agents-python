"""Borrowed provider controls reach the native turn without lossy conversion."""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    AnyLLMModel,
    ChatCompletionsModel,
    LitellmModel,
    ModelRequest,
    NativeError,
    NativeProvider,
    ResponsesModel,
    function_tool,
)
from tests._asyncio import timeout


@pytest.mark.parametrize("adapter", ["chat", "litellm", "anyllm"])
@pytest.mark.parametrize(
    "case",
    [
        "nullable_usage",
        "extra_body",
        "content_filter",
        "encoder",
        "cache_write",
        "multiple_choices",
        "single_choice",
        "invalid_private_n",
    ],
)
async def test_native_borrowed_provider_controls(adapter, case, monkeypatch):
    seen, fallback = [], []

    async def respond(request):
        body = await request.json()
        if request.path.startswith("/fallback"):
            fallback.append(body)
            return web.json_response({"error": {"message": "Unexpected fallback"}}, status=500)
        seen.append(body)
        choice = {
            "index": 0,
            "finish_reason": "content_filter" if case == "content_filter" else "stop",
            "message": {"role": "assistant", "content": None if case == "content_filter" else "42"},
        }
        if case == "encoder":
            choice["message"]["content"] = "synthetic-private-encoder-output"
            choice["logprobs"] = {
                "content": [
                    {"token": "x", "logprob": float("nan"), "bytes": [120], "top_logprobs": []}
                ]
            }
        return web.json_response(
            {
                "id": "chat_controls",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4.1",
                "choices": [choice, {**choice, "index": 1}]
                if case == "multiple_choices"
                else [choice],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                    "prompt_tokens_details": {
                        "cached_tokens": None if case == "nullable_usage" else 0,
                        "cache_write_tokens": None
                        if case == "nullable_usage"
                        else 7
                        if case == "cache_write"
                        else 0,
                    },
                    "completion_tokens_details": {
                        "reasoning_tokens": None if case == "nullable_usage" else 0
                    },
                },
            }
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", respond)
    async with timeout(25), TestServer(app) as server:
        base = str(server.make_url("/v1"))
        async with (
            AsyncOpenAI(api_key="synthetic", base_url=base, max_retries=0) as client,
            AsyncExitStack() as cleanup,
        ):
            options = {"prompt_cache_key": ""} if case == "extra_body" else {}
            if adapter == "chat":
                backend = ChatCompletionsModel(client, **options)
            elif adapter == "litellm":
                litellm = pytest.importorskip("litellm")
                monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
                monkeypatch.setattr(litellm, "telemetry", False)
                monkeypatch.setattr(litellm, "api_base", base)
                monkeypatch.setattr(litellm, "api_key", "synthetic")
                backend = LitellmModel(litellm, "openai/gpt-4.1", **options)
            else:
                any_llm = pytest.importorskip("any_llm")
                borrowed = any_llm.AnyLLM.create(
                    "openai", api_key="synthetic", api_base=base, max_retries=0
                )
                cleanup.push_async_callback(borrowed.client.close)
                backend = AnyLLMModel(borrowed, "gpt-4.1", **options)
            async with NativeProvider(
                base_url=str(server.make_url("/fallback/v1")), model_backend=backend
            ) as provider:
                agent = Agent(
                    instructions="Reply.",
                    model="gpt-4.1",
                    provider=provider,
                    **(
                        {
                            "model_settings": {
                                "extra_body": {"custom_native_extension": {"enabled": True}}
                            }
                        }
                        if case == "extra_body"
                        else {"model_settings": {"extra_body": {"n": 2}}}
                        if case == "multiple_choices"
                        else {"model_settings": {"extra_body": {"n": 1}}}
                        if case == "single_choice"
                        else {"model_settings": {"extra_body": {"n": "synthetic-private-choice"}}}
                        if case == "invalid_private_n"
                        else {}
                    ),
                )
                if case in ("multiple_choices", "invalid_private_n"):
                    with pytest.raises(ValueError, match="exactly one") as caught:
                        await agent.run("reply")
                    if case == "invalid_private_n":
                        trace = caught.value.__traceback__
                        while trace:
                            frame = trace.tb_frame
                            if frame.f_globals.get("__name__") == "_agents_codex.models":
                                assert "synthetic-private-choice" not in repr(frame.f_locals)
                            trace = trace.tb_next
                elif case == "content_filter":
                    with pytest.raises(NativeError) as caught:
                        await agent.run("reply")
                    assert caught.value.response_integrity_reason == "provider_refusal"
                elif case == "encoder":
                    with pytest.raises(ValueError) as caught:
                        await agent.run("reply")
                    error = caught.value
                    assert error.__context__ is None
                    trace = error.__traceback__
                    while trace:
                        assert trace.tb_frame.f_globals.get("__name__") not in {
                            "json",
                            "json.encoder",
                        }
                        trace = trace.tb_next
                    assert "synthetic-private" not in str(error)
                else:
                    result = await agent.run("reply")
                    assert result.final_output == "42"
                    if case == "cache_write":
                        assert result.thread_usage.total_token_usage.cache_write_input_tokens == 7
        assert not fallback
        assert len(seen) == (0 if case in ("multiple_choices", "invalid_private_n") else 1)
        if case == "single_choice":
            assert seen[0]["n"] == 1
        if case == "extra_body":
            assert seen[0]["custom_native_extension"] == {"enabled": True}
            assert seen[0]["prompt_cache_key"] == ""


@pytest.mark.parametrize("cache_override", [None, ""])
async def test_anyllm_anthropic_native_output_cap(cache_override):
    any_llm = pytest.importorskip("any_llm")
    seen = []

    async def respond(request):
        seen.append(await request.json())
        return web.json_response(
            {
                "id": "msg_cap",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "42"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", respond)
    async with timeout(25), TestServer(app) as server, AsyncExitStack() as cleanup:
        borrowed = any_llm.AnyLLM.create(
            "anthropic", api_key="synthetic", api_base=str(server.make_url("/")), max_retries=0
        )
        cleanup.push_async_callback(borrowed.client.close)
        async with NativeProvider(
            model_backend=AnyLLMModel(
                borrowed, "claude-sonnet-4-5", prompt_cache_key=cache_override
            )
        ) as provider:
            agent = Agent(
                instructions="Reply.",
                model="gpt-4.1",
                provider=provider,
                model_settings={"max_output_tokens": 7},
            )
            if cache_override is None:
                assert (await agent.run("reply")).final_output == "42"
            else:
                from any_llm.exceptions import UnsupportedParameterError

                with pytest.raises(UnsupportedParameterError, match="prompt_cache_key"):
                    await agent.run("reply")
        assert len(seen) == (1 if cache_override is None else 0)
        if seen:
            assert seen[0]["max_tokens"] == 7
            assert "max_completion_tokens" not in seen[0]
            assert "store" not in seen[0]


@pytest.mark.parametrize("configured", [False, True])
async def test_litellm_reasoning_rejected_before_native_tool_effect(configured, monkeypatch):
    litellm = pytest.importorskip("litellm")
    seen, effects = [], []

    @function_tool
    def record() -> str:
        effects.append("entered")
        return "done"

    async def respond(request):
        seen.append(await request.json())
        first = len(seen) == 1
        message = {"role": "assistant", "content": None if first else "done"}
        if first:
            message.update(
                reasoning_content="synthetic-private-reasoning",
                thinking_blocks=[
                    {"type": "thinking", "thinking": "private", "signature": "opaque"}
                ],
                tool_calls=[
                    {
                        "id": "call_record",
                        "type": "function",
                        "function": {"name": "record", "arguments": "{}"},
                    }
                ],
            )
        return web.json_response(
            {
                "id": "chat_reasoning",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4.1",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if first else "stop",
                        "message": message,
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", respond)
    async with timeout(25), TestServer(app) as server:
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        monkeypatch.setattr(litellm, "telemetry", False)
        monkeypatch.setattr(litellm, "api_base", str(server.make_url("/v1")))
        monkeypatch.setattr(litellm, "api_key", "synthetic")
        async with NativeProvider(
            model_backend=LitellmModel(litellm, "openai/gpt-4.1")
        ) as provider:
            agent = Agent(
                instructions="Use record.",
                model="gpt-4.1",
                tools=[record],
                provider=provider,
                **(
                    {
                        "model_settings": {
                            "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 128}}
                        }
                    }
                    if configured
                    else {}
                ),
            )
            with pytest.raises(ValueError, match="thinking|reasoning"):
                await agent.run("go")
    assert not effects
    assert len(seen) == (0 if configured else 1)


async def test_chat_reasoning_input_rejected_before_client_entry():
    entered = []

    async def create(**kwargs):
        entered.append(kwargs)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}

    model = ChatCompletionsModel(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    with pytest.raises(ValueError, match="reasoning replay"):
        await model.complete(
            ModelRequest(
                {
                    "model": "fixture",
                    "input": [
                        {
                            "type": "reasoning",
                            "id": "rs_fixture",
                            "summary": [],
                            "encrypted_content": "opaque",
                        }
                    ],
                },
                None,
                "session",
                "turn",
            )
        )
    assert not entered


@pytest.mark.parametrize(
    "incoming,override,expected",
    [
        ("session", None, None),
        ("compact:parent", None, None),
        ("chosen", None, None),
        ("session", "", ""),
    ],
)
async def test_anyllm_cache_capability_preserves_explicit_keys(incoming, override, expected):
    seen = []

    async def create(**options):
        seen.append(options)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}

    model = AnyLLMModel(
        SimpleNamespace(acompletion=create, PROMPT_CACHE_KEY_SUPPORT="unsupported"),
        prompt_cache_key=override,
    )
    await model.complete(
        ModelRequest(
            {"model": "fixture", "input": [], "prompt_cache_key": incoming}, None, "session", "turn"
        )
    )
    assert seen[0].get("prompt_cache_key") == expected
    assert ("prompt_cache_key" in seen[0]) is (expected is not None)


@pytest.mark.parametrize("store", [False, True])
async def test_anyllm_generic_storage_control(store):
    seen = []

    async def create(**options):
        seen.append(options)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}

    model = AnyLLMModel(SimpleNamespace(acompletion=create))
    await model.complete(
        ModelRequest({"model": "fixture", "input": [], "store": store}, None, "session", "turn")
    )
    assert ("store" in seen[0]) is store
    if store:
        assert seen[0]["store"] is True


@pytest.mark.parametrize("responses", [False, True])
async def test_mandatory_null_usage_remains_invalid(responses):
    async def create(**options):
        if not responses:
            return {
                "id": "resp_null",
                "choices": [{"finish_reason": "stop", "message": {"content": "42"}}],
                "usage": {"prompt_tokens": None, "completion_tokens": 1, "total_tokens": 1},
            }
        return {
            "id": "resp_null",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "42"}],
                }
            ],
            "usage": {"input_tokens": None, "output_tokens": 1, "total_tokens": 1},
        }

    model = (
        ResponsesModel(SimpleNamespace(responses=SimpleNamespace(create=create)))
        if responses
        else ChatCompletionsModel(
            SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        )
    )
    async with NativeProvider(model_backend=model) as provider:
        with pytest.raises(ValueError, match="nonnegative native token counters"):
            await Agent(instructions="Reply.", model="gpt-4.1", provider=provider).run("reply")

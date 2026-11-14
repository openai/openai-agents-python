"""Borrowed provider libraries remain inside the native turn and tool owner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
    ModelResponse,
    NativeError,
    NativeProvider,
    ResponsesModel,
    TurnStoppedError,
    function_tool,
)
from agents.observation import OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import text_response


def _chat_event_frames(completion):
    choice = completion["choices"][0]
    delta = dict(choice["message"])
    if "tool_calls" in delta:
        delta["tool_calls"] = [
            {**call, "index": index} for index, call in enumerate(delta["tool_calls"])
        ]
    header = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }
    chunks = [
        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
        {"choices": [], "usage": completion["usage"]},
    ]
    return [
        *(f"data: {json.dumps({**header, **chunk})}\n\n".encode() for chunk in chunks),
        b"data: [DONE]\n\n",
    ]


async def _exercise_borrowed_provider(
    adapter,
    outcome,
    monkeypatch,
    *,
    cache_key=None,
    check_cache_key=False,
    penalties=False,
    reasoning=False,
    streaming=False,
    anyllm_api=None,
    anyllm_provider="openai",
):
    seen = []
    effects = []
    fallback = []
    routes = []
    entered = asyncio.Event()
    release = asyncio.Event()

    @function_tool
    def double(value: int) -> str:
        effects.append(value)
        return str(value * 2)

    async def create(request):
        body = await request.json()
        seen.append(body)
        routes.append(request.path)
        if anyllm_provider == "azureopenai":
            assert request.headers["Authorization"] == "Bearer synthetic"
        entered.set()
        if outcome == "cancel" and not streaming:
            await release.wait()
        final = len(seen) > 1
        if final:
            assert "42" in str(body.get("input", body.get("messages")))
            if reasoning:
                replayed = next(item for item in body["input"] if item.get("type") == "reasoning")
                assert replayed["encrypted_content"] == "synthetic-opaque-reasoning"
                assert replayed["id"] == "rs_synthetic"
        if adapter in ("responses", "any_llm_responses"):
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
            completed = {
                "id": f"resp_{len(seen)}",
                "object": "response",
                "created_at": 0,
                "model": "gpt-4.1",
                "status": "incomplete" if outcome == "incomplete" else "completed",
                "output": output,
                "usage": {
                    "input_tokens": "synthetic-private-output" if outcome == "invalid_usage" else 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 1},
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            }
            if anyllm_provider == "huggingface":
                # The OpenResponses router validates a complete response resource.
                completed.update(
                    tools=[],
                    tool_choice="auto",
                    truncation="disabled",
                    parallel_tool_calls=False,
                    text={"format": {"type": "text"}},
                    top_p=1.0,
                    presence_penalty=0.0,
                    frequency_penalty=0.0,
                    top_logprobs=0,
                    temperature=1.0,
                    store=False,
                    background=False,
                    service_tier="default",
                )
            if not streaming:
                return web.json_response(completed)
            frames = [{"type": "response.created", "response": {"id": completed["id"]}}]
            if outcome == "cancel":
                frames.extend(text_response("msg_cancel", "partial")[:2])
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                for frame in frames:
                    await response.write(f"data: {json.dumps(frame)}\n\n".encode())
                await release.wait()
                return response
            if final:
                frames.extend(text_response("msg_result", "42"))
            else:
                frames.append(
                    {"type": "response.output_item.done", "output_index": 0, "item": output[0]}
                )
            frames.append({"type": "response.completed", "response": completed})
            return web.Response(
                body="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames).encode(),
                content_type="text/event-stream",
            )
        message = {"role": "assistant", "content": "42" if final else None}
        if streaming and outcome == "cancel":
            message["content"] = "partial"
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
        completion = {
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
            "usage": {
                "prompt_tokens": "synthetic-private-output" if outcome == "invalid_usage" else 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }
        if not streaming:
            return web.json_response(completion)
        frames = _chat_event_frames(completion)
        if final or outcome == "cancel":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(frames[0])
            await release.wait()
            if outcome != "cancel":
                await response.write(b"".join(frames[1:]))
            return response
        return web.Response(body=b"".join(frames), content_type="text/event-stream")

    async def unexpected(request):
        fallback.append(request.path)
        return web.json_response(
            {"error": {"message": "Unexpected native HTTP fallback"}}, status=500
        )

    app = web.Application()
    app.router.add_post("/api/v1/responses", create)
    app.router.add_post("/v1/responses", create)
    app.router.add_post("/openai/v1/responses", create)
    app.router.add_post("/v1/chat/completions", create)
    app.router.add_route("*", "/fallback/{tail:.*}", unexpected)
    groq_response_clients: list[AsyncOpenAI] = []
    async with timeout(30), TestServer(app) as server:
        base = str(server.make_url("/v1"))
        async with AsyncOpenAI(api_key="synthetic", base_url=base, max_retries=0) as client:
            options = {} if cache_key is None else {"prompt_cache_key": cache_key}
            if streaming:
                options["stream"] = True
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
                if anyllm_provider == "fireworks":
                    pytest.importorskip("fireworks")
                if anyllm_provider == "huggingface":
                    from any_llm.providers.huggingface.huggingface import HuggingfaceProvider

                    monkeypatch.setattr(HuggingfaceProvider, "OPENRESPONSES_ROUTER_URL", base)
                    borrowed = any_llm.AnyLLM.create(
                        anyllm_provider, api_key="synthetic", api_base=base
                    )
                elif anyllm_provider == "groq":
                    pytest.importorskip("groq")
                    from any_llm.providers.groq import groq as groq_adapter

                    def local_responses_client(**kwargs):
                        kwargs["base_url"] = base
                        local = AsyncOpenAI(**kwargs)
                        groq_response_clients.append(local)
                        return local

                    monkeypatch.setattr(groq_adapter, "AsyncOpenAI", local_responses_client)
                    borrowed = any_llm.AnyLLM.create("groq", api_key="synthetic", max_retries=0)
                elif anyllm_provider == "otari":
                    pytest.importorskip("otari")
                    borrowed = any_llm.AnyLLM.create(
                        "otari",
                        api_key="synthetic",
                        api_base=str(server.make_url("/")).rstrip("/"),
                        platform_mode=False,
                    )
                else:
                    borrowed = any_llm.AnyLLM.create(
                        anyllm_provider,
                        api_key="synthetic",
                        api_base=str(server.make_url("/"))
                        if anyllm_provider == "azureopenai"
                        else base,
                        max_retries=0,
                    )
                if anyllm_api is not None:
                    options["api"] = anyllm_api
                backend = AnyLLMModel(borrowed, "gpt-4.1", **options)
            async with NativeProvider(
                base_url=str(server.make_url("/fallback/v1")), model_backend=backend
            ) as provider:
                agent = Agent(
                    model="gpt-4.1",
                    instructions="Use double. synthetic-private-prompt",
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
                    if streaming and outcome in ("complete", "cancel"):
                        try:
                            async for event in turn.events():
                                if isinstance(event, OutputTextDelta):
                                    assert event.delta == (
                                        "42" if outcome == "complete" else "partial"
                                    )
                                    break
                            if outcome == "cancel":
                                await turn.cancel()
                        finally:
                            release.set()
                    if outcome == "cancel":
                        if not streaming:
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
                        ) as caught:
                            await turn.get_result()
                        _assert_adapter_frames_released(caught.value, sdk_chain=True)
                    elif outcome == "invalid_usage":
                        with pytest.raises(Exception) as caught:
                            await turn.get_result()
                        _assert_adapter_frames_released(caught.value, sdk_chain=True)
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
            assert seen[0].get("stream", False) is streaming
            if adapter == "responses":
                assert not client.is_closed()
            if adapter == "any_llm_responses" and anyllm_provider == "otari":
                assert routes == ["/api/v1/responses"] * len(seen)
                assert all(body["tool_choice"] == "auto" for body in seen)
                await borrowed.client.close()
            elif adapter == "any_llm_responses":
                assert routes == [
                    "/openai/v1/responses" if anyllm_provider == "azureopenai" else "/v1/responses"
                ] * len(seen)
                assert all(body["client_metadata"]["session_id"] for body in seen)
                if anyllm_provider == "huggingface":
                    assert not borrowed.responses_client.is_closed()
                    await borrowed.responses_client.close()
                else:
                    assert not borrowed.client.is_closed()
                await borrowed.client.close()
                for local in groq_response_clients:
                    await local.close()
                if anyllm_provider == "fireworks":
                    await borrowed.fireworks_client.close()

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
@pytest.mark.parametrize("outcome", ["incomplete", "refusal", "cancel"])
async def test_borrowed_provider_library_retains_native_ownership(adapter, outcome, monkeypatch):
    await _exercise_borrowed_provider(adapter, outcome, monkeypatch)


@pytest.mark.parametrize("adapter", ["litellm", "any_llm"])
@pytest.mark.parametrize("outcome", ["complete", "incomplete", "refusal", "cancel"])
async def test_borrowed_chat_stream_retains_native_ownership(adapter, outcome, monkeypatch):
    await _exercise_borrowed_provider(adapter, outcome, monkeypatch, streaming=True)


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


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("api", ["responses", "auto"])
@pytest.mark.parametrize(
    "provider_name",
    [
        "openai",
        "openrouter",
        "azureopenai",
        "gmi",
        "deepseek",
        "meta",
        "fireworks",
        "otari",
        "groq",
    ],
)
async def test_anyllm_responses_uses_native_tool_continuation(
    api, streaming, monkeypatch, provider_name
):
    await _exercise_borrowed_provider(
        "any_llm_responses",
        "complete",
        monkeypatch,
        streaming=streaming,
        anyllm_api=api,
        anyllm_provider=provider_name,
    )


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("api", ["responses", "auto"])
async def test_anyllm_huggingface_responses_uses_native_tool_continuation(
    api, streaming, monkeypatch
):
    await _exercise_borrowed_provider(
        "any_llm_responses",
        "complete",
        monkeypatch,
        streaming=streaming,
        anyllm_api=api,
        anyllm_provider="huggingface",
    )


def test_anyllm_huggingface_streaming_requires_direct_close_owner():
    async def aresponses(model, input_data, *, extra_body=None):
        raise AssertionError("the provider must not be called")

    provider = SimpleNamespace(
        PROVIDER_NAME="huggingface", SUPPORTS_RESPONSES=True, aresponses=aresponses
    )
    with pytest.raises(ValueError, match="requires its OpenAI client"):
        AnyLLMModel(provider, api="responses", stream=True)
    assert AnyLLMModel(provider, api="auto", stream=True) == AnyLLMModel(provider, stream=True)


@pytest.mark.parametrize(
    "provider_name",
    ["openai", "openrouter", "azureopenai", "gmi", "deepseek", "meta", "fireworks", "huggingface"],
)
async def test_anyllm_responses_cancel_keeps_borrowed_client_owned(monkeypatch, provider_name):
    await _exercise_borrowed_provider(
        "any_llm_responses",
        "cancel",
        monkeypatch,
        streaming=True,
        anyllm_api="responses",
        anyllm_provider=provider_name,
    )


@pytest.mark.parametrize("provider_name", ["openai", "gemini"])
def test_anyllm_responses_rejects_unqualified_provider_before_io(provider_name):
    async def old_openai_responses(model, input_data, **kwargs):
        raise AssertionError("the provider must not be called")

    async def gemini_responses(model, input_data, *, extra_body=None):
        raise AssertionError("the provider must not be called")

    provider = SimpleNamespace(
        PROVIDER_NAME=provider_name,
        SUPPORTS_RESPONSES=True,
        aresponses=old_openai_responses if provider_name == "openai" else gemini_responses,
    )
    with pytest.raises(
        ValueError,
        match="supported OpenAI-compatible provider with public aresponses extra_body",
    ):
        AnyLLMModel(provider, api="responses")
    assert AnyLLMModel(provider, api="auto") == AnyLLMModel(provider)


@pytest.mark.parametrize("release", [None, "1.27.0", "1.28.0rc1", "1.28.0"])
def test_anyllm_streaming_responses_requires_close_propagation(release, monkeypatch):
    any_llm = pytest.importorskip("any_llm")
    if release is None:
        monkeypatch.delattr(any_llm, "__version__")
    else:
        monkeypatch.setattr(any_llm, "__version__", release)

    async def aresponses(model, input_data, *, extra_body=None):
        raise AssertionError("the provider must not be called during routing")

    provider = any_llm.AnyLLM.create("openai", api_key="synthetic")
    monkeypatch.setattr(provider, "aresponses", aresponses)
    chat = AnyLLMModel(provider, stream=True)
    auto = AnyLLMModel(provider, stream=True, api="auto")
    if release == "1.28.0":
        assert auto == AnyLLMModel(provider, stream=True, api="responses")
        assert auto != chat
    else:
        with pytest.raises(ValueError, match="requires any-llm-sdk>=1.28.0"):
            AnyLLMModel(provider, stream=True, api="responses")
        assert auto == chat

    assert AnyLLMModel(provider, api="auto") == AnyLLMModel(provider, api="responses")
    assert AnyLLMModel(provider, api="responses") != AnyLLMModel(provider)
    duck = SimpleNamespace(PROVIDER_NAME="openai", SUPPORTS_RESPONSES=True, aresponses=aresponses)
    assert AnyLLMModel(duck, stream=True, api="auto") == AnyLLMModel(
        duck, stream=True, api="responses"
    )


async def test_responses_reasoning_replays_through_native_tool_continuation(monkeypatch):
    await _exercise_borrowed_provider("responses", "complete", monkeypatch, reasoning=True)


def _assert_adapter_frames_released(error: BaseException, *, sdk_chain: bool = False) -> None:
    frames = []
    owner = ResponsesModel.complete.__code__.co_filename
    owner_root = Path(owner).parent
    trace = error.__traceback__
    while trace is not None:
        filename = trace.tb_frame.f_code.co_filename
        if filename == owner or (sdk_chain and owner_root in Path(filename).parents):
            frames.append((filename, trace.tb_frame.f_locals))
        trace = trace.tb_next
    assert frames
    # Application/client frames retain their own values. SDK adapter frames must
    # release the request and provider payloads before exposing their failure.
    payload_names = {
        "request",
        "messages",
        "native_tools",
        "content",
        "tools",
        "declaration",
        "leaves",
        "tool",
        "options",
        "text",
        "output_format",
        "reasoning",
        "choice",
        "message",
        "output_content",
        "text_part",
        "probabilities",
        "call",
        "body",
        "response",
        "value",
        "output",
        "raw_usage",
        "usage",
        "item",
        "part",
        ".0",
    }
    assert all(
        not payload_names.intersection(frame) for filename, frame in frames if filename == owner
    )
    # SDK callback wrappers own their argument tuples, including repr-hidden requests.
    for _, frame in frames:
        for value in frame.values():
            values = value if type(value) in (list, tuple) else (value,)
            assert not any(isinstance(item, (ModelRequest, ModelResponse)) for item in values)

    def payload_contains_canary(value):
        # Inspect SDK-owned JSON copies, not borrowed exceptions/clients or Session state.
        if type(value) is str:
            return "synthetic-private-output" in value or "synthetic-private-prompt" in value
        if type(value) is dict:
            return any(
                payload_contains_canary(k) or payload_contains_canary(v) for k, v in value.items()
            )
        if type(value) in (list, tuple):
            return any(payload_contains_canary(item) for item in value)
        return False

    assert not any(
        payload_contains_canary(value) for _, frame in frames for value in frame.values()
    )


@pytest.mark.parametrize("adapter", ["responses", "anyllm"])
@pytest.mark.parametrize(
    "input",
    [
        "synthetic-private-prompt",
        [{"role": "user", "content": "synthetic-private-prompt"}],
        [{"type": "message", "role": "user", "content": "synthetic-private-prompt"}],
    ],
)
async def test_responses_text_input_reaches_provider(adapter, input):
    received = []

    async def create(*, extra_body=None, **options):
        received.append(extra_body["input"] if adapter == "responses" else options["input_data"])
        return {"status": "completed", "output": []}

    client = SimpleNamespace(
        responses=SimpleNamespace(create=create),
        aresponses=create,
        PROVIDER_NAME="openai",
        SUPPORTS_RESPONSES=True,
    )
    model = (
        ResponsesModel(client) if adapter == "responses" else AnyLLMModel(client, api="responses")
    )
    result = await model.complete(
        ModelRequest({"model": "fixture", "input": input}, None, "s", "t")
    )
    assert result.output == []
    assert received == [input]


@pytest.mark.parametrize("adapter", ["responses", "anyllm"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("typed_message", [False, True])
async def test_responses_video_rejection_releases_payloads(adapter, stream, typed_message):
    calls = []

    async def create(*, extra_body=None, **options):
        calls.append(options)
        raise AssertionError("Video input reached the provider")

    client = SimpleNamespace(
        responses=SimpleNamespace(create=create),
        aresponses=create,
        PROVIDER_NAME="openai",
        SUPPORTS_RESPONSES=True,
    )
    model = (
        ResponsesModel(client, stream=stream)
        if adapter == "responses"
        else AnyLLMModel(client, api="responses", stream=stream)
    )
    message = {
        "role": "user",
        "content": [
            {"type": "input_video", "video_url": "https://synthetic-private-prompt.invalid/v.mp4"}
        ],
        **({"type": "message"} if typed_message else {}),
    }
    request = ModelRequest({"model": "fixture", "input": [message]}, None, "s", "t")
    with pytest.raises(ValueError, match="Video input requires") as caught:
        if stream:
            assert model.stream is not None
            async for _ in model.stream(request):
                pass
        else:
            await model.complete(request)
    assert calls == []
    _assert_adapter_frames_released(caught.value)


@pytest.mark.parametrize("status", ["incomplete", "failed"])
async def test_responses_failure_traceback_releases_sdk_payloads(status):
    async def create(**options):
        return {
            "status": status,
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "synthetic-private-output"}],
                }
            ],
        }

    model = ResponsesModel(SimpleNamespace(responses=SimpleNamespace(create=create)))
    with pytest.raises(ValueError, match="requires a completed response") as caught:
        await model.complete(
            ModelRequest(
                {"model": "fixture", "input": "synthetic-private-prompt"}, None, "session", "turn"
            )
        )
    _assert_adapter_frames_released(caught.value)
    assert "synthetic-private" not in str(caught.value)


async def test_responses_client_failure_keeps_original_exception():
    failure = LookupError("synthetic-client-error")

    async def create(**options):
        raise failure

    model = ResponsesModel(SimpleNamespace(responses=SimpleNamespace(create=create)))
    with pytest.raises(LookupError) as caught:
        await model.complete(ModelRequest({"model": "fixture", "input": "prompt"}, None, "s", "t"))
    assert caught.value is failure
    _assert_adapter_frames_released(caught.value)


async def test_responses_malformed_output_traceback_releases_all_sdk_frames():
    async def create(**options):
        return {
            "status": "completed",
            "output": [
                {"type": "message", "content": [None], "private": "synthetic-private-output"}
            ],
        }

    model = ResponsesModel(SimpleNamespace(responses=SimpleNamespace(create=create)))
    with pytest.raises(AttributeError) as caught:
        await model.complete(ModelRequest({"model": "fixture", "input": "prompt"}, None, "s", "t"))
    _assert_adapter_frames_released(caught.value)


@pytest.mark.parametrize("adapter", ["responses", "chat", "litellm", "anyllm"])
@pytest.mark.parametrize(
    "outcome",
    ["incomplete", "failed", "malformed_output", "malformed_usage", "client_error", "cancel"],
)
async def test_borrowed_adapter_failure_releases_all_sdk_frames(adapter, outcome):
    failure = asyncio.CancelledError() if outcome == "cancel" else LookupError("client failure")

    async def create(**options):
        if outcome in ("client_error", "cancel"):
            raise failure
        if adapter == "responses":
            value = {
                "status": outcome if outcome in ("incomplete", "failed") else "completed",
                "output": [{"type": "message", "content": []}],
                "private": "synthetic-private-output",
            }
            if outcome == "malformed_output":
                value["output"][0]["content"] = [None]
            elif outcome == "malformed_usage":
                value["usage"] = {"input_tokens_details": "synthetic-private-output"}
        else:
            value = {
                "choices": [
                    {
                        "finish_reason": "length"
                        if outcome == "incomplete"
                        else "failed"
                        if outcome == "failed"
                        else "stop",
                        "message": {"content": "synthetic-private-output"},
                    }
                ],
            }
            if outcome == "malformed_output":
                value["choices"][0]["message"] = None
            elif outcome == "malformed_usage":
                value["usage"] = {"prompt_tokens_details": "synthetic-private-output"}
        return SimpleNamespace(model_dump=lambda: value) if adapter == "litellm" else value

    if adapter == "responses":
        model = ResponsesModel(SimpleNamespace(responses=SimpleNamespace(create=create)))
    elif adapter == "chat":
        model = ChatCompletionsModel(
            SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        )
    else:
        model = {"litellm": LitellmModel, "anyllm": AnyLLMModel}[adapter](
            SimpleNamespace(acompletion=create)
        )
    request = ModelRequest(
        {
            "model": "fixture",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "synthetic-private-prompt"}],
                }
            ],
        },
        None,
        "session",
        "turn",
    )
    expected = (
        type(failure)
        if outcome in ("client_error", "cancel")
        else ValueError
        if outcome in ("incomplete", "failed")
        else AttributeError
    )
    with pytest.raises(expected) as caught:
        await model.complete(request)
    if outcome in ("client_error", "cancel"):
        assert caught.value is failure
    _assert_adapter_frames_released(caught.value)
    assert "synthetic-private" not in str(caught.value)


@pytest.mark.parametrize("adapter", ["responses", "chat", "litellm", "anyllm"])
async def test_borrowed_adapter_invalid_usage_releases_sdk_frames_natively(adapter, monkeypatch):
    await _exercise_borrowed_provider(adapter, "invalid_usage", monkeypatch)

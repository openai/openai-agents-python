"""Native application model producers retain Core tool and cancellation ownership."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import Awaitable
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    ChatCompletionsModel,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    NativeProvider,
    ToolOutput,
    ToolOutputText,
    TurnStoppedError,
    function_tool,
    telemetry,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output


async def test_custom_model_core_executes_tool_and_supplies_result_to_next_response() -> None:
    seen: list[ModelRequest] = []
    effects: list[int] = []

    @function_tool
    def double(value: int) -> str:
        effects.append(value)
        return str(value * 2)

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request)
            if len(seen) == 1:
                return ModelResponse(
                    [
                        {
                            "type": "function_call",
                            "name": "double",
                            "call_id": "call_double",
                            "arguments": '{"value":21}',
                        }
                    ],
                    {"input_tokens": 3, "output_tokens": 2},
                )
            assert any(
                item.get("type") == "function_call_output" and "42" in str(item.get("output"))
                for item in request.body["input"]
            )
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "42"}],
                    }
                ],
                {"input_tokens": 5, "output_tokens": 1},
            )

    async with (
        timeout(20),
        NativeProvider(base_url="https://example.invalid/v1", model_backend=Model()) as provider,
    ):
        result = await Agent(
            model="custom-model", instructions="Use double.", tools=[double], provider=provider
        ).run("Double 21.", context={"tenant": "synthetic"})
    assert result.final_output == "42"
    assert effects == [21]
    assert len(seen) == 2
    assert seen[0].context == {"tenant": "synthetic"}


@pytest.mark.parametrize("native_model", [None, "gpt-5.2"])
@pytest.mark.parametrize("parallel", [False, True])
async def test_chat_client_adapter_stays_inside_native_tool_loop(
    native_model: str | None, parallel: bool
) -> None:
    seen = []
    effects = []

    async def create(raw_request):
        request = await raw_request.json()
        seen.append(request)
        pending = set()
        for history in request["messages"]:
            if history["role"] == "tool":
                assert history["tool_call_id"] in pending
                pending.remove(history["tool_call_id"])
            else:
                if pending:
                    return web.json_response(
                        {
                            "error": {
                                "message": "Missing tool outputs before next Chat message",
                                "type": "invalid_request_error",
                            }
                        },
                        status=400,
                    )
                pending.update(call["id"] for call in history.get("tool_calls", []))
        assert not pending
        if len(seen) == 1:
            message = {
                "content": "Calling echo twice." if parallel else None,
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "call_echo",
                        "function": {"name": "echo", "arguments": '{"value":"from-tool"}'},
                    },
                    *(
                        [
                            {
                                "type": "function",
                                "id": "call_echo_two",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"value":"second-tool"}',
                                },
                            }
                        ]
                        if parallel
                        else []
                    ),
                ],
            }
        else:
            assert any(
                item.get("role") == "tool" and "from-tool" in item["content"]
                for item in request["messages"]
            )
            message = {"content": "native chat finished"}
        return web.json_response(
            {
                "id": f"chat_{len(seen)}",
                "object": "chat.completion",
                "created": 0,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", **message},
                        "finish_reason": "tool_calls" if len(seen) == 1 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        )

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        return value

    app = web.Application()
    app.router.add_post("/v1/chat/completions", create)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic-key", base_url=str(server.make_url("/v1"))
        ) as client:
            async with NativeProvider(
                model_backend=ChatCompletionsModel(client, "chat-model")
            ) as provider:
                result = await Agent(
                    model=native_model, instructions="Use echo.", tools=[echo], provider=provider
                ).run("Call echo.")
    assert result.final_output == "native chat finished"
    assert len(seen) == 2
    assert sorted(effects) == (["from-tool", "second-tool"] if parallel else ["from-tool"])
    if parallel:
        assistants = [message for message in seen[1]["messages"] if message["role"] == "assistant"]
        assert len(assistants) == 1
        assert [call["id"] for call in assistants[0]["tool_calls"]] == [
            "call_echo",
            "call_echo_two",
        ]
        assert "Calling echo twice." in json.dumps(assistants[0]["content"])
    assert seen[0]["model"] == "chat-model"
    assert seen[0]["stream"] is False


@pytest.mark.parametrize("returns_awaitable", [False, True])
async def test_cancellation_joins_admitted_sync_model_and_discards_late_tool_call(
    returns_awaitable: bool,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    effects = []

    @function_tool
    def forbidden() -> str:
        effects.append("called")
        return "bad"

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse | Awaitable[ModelResponse]:
            entered.set()
            release.wait(10)
            if returns_awaitable:

                async def late():
                    effects.append("late-model-body")
                    return ModelResponse([])

                return late()
            return ModelResponse(
                [
                    {
                        "type": "function_call",
                        "name": "forbidden",
                        "call_id": "late",
                        "arguments": "{}",
                    }
                ]
            )

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        async with Agent(
            instructions="Answer.", tools=[forbidden], provider=provider
        ).session() as session:
            turn = await session.submit("Begin.")
            await asyncio.to_thread(entered.wait, 5)
            assert entered.is_set()
            stopping = asyncio.create_task(turn.cancel())
            try:
                await asyncio.sleep(0)
                assert not stopping.done()
            finally:
                release.set()
                await stopping
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
    assert effects == []


@pytest.mark.parametrize("refusal", [False, True])
async def test_chat_completed_response_preserves_native_facts_and_controls(refusal: bool) -> None:
    from agents import NativeError

    requests = []
    captured_instructions = []
    effects = []
    private = "private-model-refusal"
    token = {"token": "ok", "bytes": [111, 107], "logprob": -0.25, "top_logprobs": []}

    async def respond(http: web.Request) -> web.Response:
        requests.append(await http.json())
        return web.json_response(
            {
                "id": "chat_facts",
                "object": "chat.completion",
                "created": 0,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": None if refusal else "ok",
                            "refusal": private if refusal else None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "never",
                                    "function": {"name": "echo", "arguments": '{"value":"never"}'},
                                }
                            ]
                            if refusal
                            else None,
                        },
                        "logprobs": {"content": [token]} if not refusal else None,
                    }
                ],
            }
        )

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        return value

    application = web.Application()
    application.router.add_post("/v1/chat/completions", respond)
    async with (
        TestServer(application) as server,
        AsyncOpenAI(api_key="synthetic-key", base_url=str(server.make_url("/v1"))) as client,
    ):
        adapter = ChatCompletionsModel(client, "chat-model")

        class CaptureModel:
            async def complete(self, request: ModelRequest) -> ModelResponse:
                captured_instructions.append(
                    [
                        {
                            "role": item["role"],
                            "content": [
                                {"type": "text", "text": part["text"]} for part in item["content"]
                            ],
                        }
                        for item in request.body["input"]
                        if item.get("type") == "message" and item["role"] in ("system", "developer")
                    ]
                )
                return await adapter.complete(request)

        async with (
            NativeProvider(model_backend=CaptureModel()) as provider,
            Agent(
                model="gpt-5.2",
                instructions="Preserve this exact instruction.",
                model_settings={
                    "verbosity": "high",
                    "top_logprobs": 1,
                    "service_tier": "flex",
                    "prompt_cache_retention": "24h",
                    "prompt_cache_ttl": "12h",
                },
                tools=[echo],
                provider=provider,
            ).session() as session,
        ):
            turn = await session.submit("answer")
            if refusal:
                with pytest.raises(NativeError) as caught:
                    await turn.get_result()
                assert caught.value.response_integrity_reason == "provider_refusal"
                assert private not in str(caught.value)
            else:
                assert (await turn.get_result()).final_output == "ok"
            recorded = [row async for row in session.turns()][0]
            facts = [row["fact"] for row in recorded.get("response_facts", [])]
            if refusal:
                assert any(fact["kind"] == "refusal" and fact["text"] == private for fact in facts)
                assert recorded["status"] == "failed"
            else:
                assert any(
                    fact["kind"] == "text_probabilities" and fact["tokens"] == [token]
                    for fact in facts
                )
    assert effects == []
    assert len(requests) == 1
    assert [
        message for message in requests[0]["messages"] if message["role"] in ("system", "developer")
    ] == captured_instructions[0]
    assert "Preserve this exact instruction." in json.dumps(captured_instructions[0])
    assert requests[0]["verbosity"] == "high"
    assert requests[0]["service_tier"] == "flex"
    assert requests[0]["prompt_cache_retention"] == "24h"
    assert requests[0]["prompt_cache_options"] == {"ttl": "12h"}
    assert requests[0]["logprobs"] is True
    assert requests[0]["top_logprobs"] == 1


@pytest.mark.parametrize("later", ["refusal", "malformed"])
async def test_completed_model_validates_whole_output_before_any_tool(later: str) -> None:
    from agents import NativeError

    effects = []

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        return value

    class Producer:
        def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                [
                    {
                        "type": "function_call",
                        "call_id": "never",
                        "name": "echo",
                        "arguments": '{"value":"never"}',
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "private-later-refusal"}],
                    }
                    if later == "refusal"
                    else {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": 7}],
                    },
                ]
            )

    async with (
        NativeProvider(model_backend=Producer()) as provider,
        Agent(instructions="Answer.", tools=[echo], provider=provider).session() as session,
    ):
        turn = await session.submit("answer")
        with pytest.raises(NativeError) as caught:
            await turn.get_result()
        assert caught.value.response_integrity_reason == (
            "provider_refusal" if later == "refusal" else "undecodable_output_item"
        )
    assert effects == []


@pytest.mark.parametrize(
    "finish_reason",
    ["length", "content_filter", "function_call", None, "N01_SYNTHETIC_PRIVATE_FINISH_REASON"],
)
@pytest.mark.parametrize("refusal", [False, True])
async def test_chat_unsuccessful_finish_has_no_tool_effect(
    finish_reason: str | None, refusal: bool
) -> None:
    from agents import NativeError

    requests = []
    effects = []
    private = "private-terminal-refusal"

    async def respond(http: web.Request) -> web.Response:
        requests.append(await http.json())
        first = len(requests) == 1
        return web.json_response(
            {
                "id": f"chat_terminal_{len(requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        **(
                            {"finish_reason": finish_reason if first else "stop"}
                            if finish_reason is not None or not first
                            else {}
                        ),
                        "message": {
                            "role": "assistant",
                            "content": "private-partial-answer" if first else "finished",
                            "refusal": private if first and refusal else None,
                            "function_call": {"name": "echo", "arguments": '{"value":"never"}'}
                            if first and finish_reason == "function_call"
                            else None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "never",
                                    "function": {"name": "echo", "arguments": '{"value":"never"}'},
                                }
                            ]
                            if first and finish_reason != "function_call"
                            else None,
                        },
                    }
                ],
            }
        )

    @function_tool
    def echo(value: str) -> str:
        effects.append(value)
        return value

    application = web.Application()
    application.router.add_post("/v1/chat/completions", respond)
    async with (
        timeout(30),
        TestServer(application) as server,
        AsyncOpenAI(api_key="synthetic-key", base_url=str(server.make_url("/v1"))) as client,
    ):
        async with (
            NativeProvider(model_backend=ChatCompletionsModel(client, "chat-model")) as provider,
            Agent(
                model="gpt-5.2", instructions="Answer.", tools=[echo], provider=provider
            ).session() as session,
        ):
            turn = await session.submit("answer")
            with pytest.raises((ValueError, NativeError)) as caught:
                await turn.get_result()
            assert "private-partial-answer" not in str(caught.value)
            assert "N01_SYNTHETIC_PRIVATE_FINISH_REASON" not in str(caught.value)
            recorded = [row async for row in session.turns()][0]
            assert recorded["status"] in ("failed", "cancelled")
            if refusal:
                assert recorded["status"] == "failed"
                assert isinstance(caught.value, NativeError)
                assert caught.value.response_integrity_reason == "provider_refusal"
                assert any(
                    row["fact"]["kind"] == "refusal" and row["fact"]["text"] == private
                    for row in recorded["response_facts"]
                )
            else:
                reason = (
                    finish_reason
                    if finish_reason in ("length", "content_filter")
                    else "unsupported"
                )
                assert f"finish_reason={reason}" in str(caught.value)
    assert effects == []
    assert len(requests) == 1


@pytest.mark.parametrize("source", ["include", "extra_body"])
async def test_chat_admitted_text_options_and_logprob_request(source: str) -> None:
    requests = []
    token = {"token": "ok", "bytes": [111, 107], "logprob": -0.5, "top_logprobs": []}
    extras = {
        "seed": 7,
        "stop": ["stop-marker"],
        "logit_bias": {"42": -1},
        "user": "synthetic-user",
        "safety_identifier": "synthetic-safe",
        "prediction": {"type": "content", "content": "ok"},
    }

    async def respond(http: web.Request) -> web.Response:
        body = await http.json()
        requests.append(body)
        return web.json_response(
            {
                "id": "chat_options",
                "object": "chat.completion",
                "created": 0,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                        "logprobs": {"content": [token]} if body.get("logprobs") else None,
                    }
                ],
            }
        )

    application = web.Application()
    application.router.add_post("/v1/chat/completions", respond)
    settings: ModelSettings = {
        "extra_body": {**extras, **({"logprobs": True} if source == "extra_body" else {})}
    }
    if source == "include":
        settings["include"] = ["message.output_text.logprobs"]
    async with (
        timeout(30),
        TestServer(application) as server,
        AsyncOpenAI(api_key="synthetic-key", base_url=str(server.make_url("/v1"))) as client,
    ):
        async with (
            NativeProvider(model_backend=ChatCompletionsModel(client, "chat-model")) as provider,
            Agent(
                model="gpt-5.2", instructions="Answer.", model_settings=settings, provider=provider
            ).session() as session,
        ):
            assert (await (await session.submit("answer")).get_result()).final_output == "ok"
            recorded = [row async for row in session.turns()][0]
            # Core requires top_logprobs opt-in before persisting sensitive probabilities.
            assert recorded.get("response_facts", []) == []
    assert len(requests) == 1
    assert requests[0]["logprobs"] is True
    assert "top_logprobs" not in requests[0]
    assert all(requests[0][key] == value for key, value in extras.items())


@pytest.mark.parametrize(
    "kind", ["string", "text-parts", "image", "file", "mixed-image", "mixed-file"]
)
async def test_chat_tool_output_rejects_media_before_next_client_call(kind: str) -> None:
    requests = []
    effects = []

    async def create(raw_request):
        requests.append(await raw_request.json())
        return web.json_response(
            {
                "id": "chat-tool-output",
                "object": "chat.completion",
                "created": 1,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_content",
                                    "type": "function",
                                    "function": {"name": "content", "arguments": "{}"},
                                }
                            ],
                        }
                        if len(requests) == 1
                        else {"role": "assistant", "content": "finished"},
                    }
                ],
            }
        )

    @function_tool
    def content() -> ToolOutput | str:
        effects.append("called")
        if kind == "string":
            return "first\n second"
        if kind == "text-parts":
            return ToolOutput([ToolOutputText("first\n"), ToolOutputText(" second")])
        media = (
            ToolOutput.image("https://example.invalid/synthetic-image.png")
            if kind.endswith("image")
            else ToolOutput.file(file_id="file_synthetic")
        )
        return (
            ToolOutput([ToolOutputText("visible text"), *media.content_items])
            if kind.startswith("mixed")
            else media
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", create)
    async with (
        TestServer(app) as server,
        AsyncOpenAI(
            base_url=str(server.make_url("/v1")), api_key="synthetic", max_retries=0
        ) as client,
    ):
        async with NativeProvider(
            model_backend=ChatCompletionsModel(client, "chat-model")
        ) as provider:
            pending = Agent(
                model="gpt-5.2", instructions="Use content.", tools=[content], provider=provider
            ).run("Get content.")
            if kind in ("string", "text-parts"):
                assert (await pending).final_output == "finished"
                assert len(requests) == 2
                assert (
                    next(
                        message for message in requests[1]["messages"] if message["role"] == "tool"
                    )["content"]
                    == "first\n second"
                )
            else:
                with pytest.raises(ValueError, match="only text tool outputs"):
                    await pending
                assert len(requests) == 1
            assert effects == ["called"]


@pytest.mark.parametrize("supplied", [False, True])
async def test_model_backend_survives_owned_compaction(supplied: bool) -> None:
    requests = []
    provider_requests = []

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            requests.append(request)
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Owned model summary or answer."}
                        ],
                    }
                ]
            )

    async def unexpected_provider(request):
        provider_requests.append(await request.json())
        return web.json_response({"error": {"message": "Unexpected provider fallback"}}, status=400)

    app = web.Application()
    app.router.add_post("/v1/responses", unexpected_provider)
    async with TestServer(app) as server, timeout(20):
        async with NativeProvider(
            base_url=str(server.make_url("/v1")), model_backend=Model(), request_max_retries=0
        ) as provider:
            async with Agent(
                model="gpt-5.2", instructions="Answer.", provider=provider
            ).session() as session:
                assert (
                    await (await session.submit("Before compaction.")).get_result()
                ).final_output == "Owned model summary or answer."
                await session.compact(**({"summary": "Supplied summary."} if supplied else {}))
                assert (
                    await (await session.submit("After compaction.")).get_result()
                ).final_output == "Owned model summary or answer."
    assert len(requests) == (2 if supplied else 3)
    assert provider_requests == []


async def test_cancellation_during_model_resolution_does_not_start_completion() -> None:
    entered = threading.Event()
    release = threading.Event()
    effects = []

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            effects.append("complete")
            return ModelResponse([])

    class Provider:
        def get_model(self, model_name: str) -> Model:
            entered.set()
            release.wait(10)
            return Model()

    async with timeout(20), NativeProvider(model_backend=Provider()) as provider:
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            turn = await session.submit("Begin.")
            await asyncio.to_thread(entered.wait, 5)
            assert entered.is_set()
            stopping = asyncio.create_task(turn.cancel())
            try:
                await asyncio.sleep(0)
                assert not stopping.done()
            finally:
                release.set()
                await stopping
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
    assert effects == []


async def test_cancel_model_compaction_joins_callback_and_allows_next_submission() -> None:
    entered = threading.Event()
    release = threading.Event()
    requests = []

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            requests.append(request)
            if len(requests) == 2:
                entered.set()
                release.wait(10)
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Model answer."}],
                    }
                ]
            )

    async with (
        timeout(20),
        NativeProvider(
            base_url="http://127.0.0.1:1/v1", model_backend=Model(), request_max_retries=0
        ) as provider,
    ):
        async with Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider
        ).session() as session:
            await (await session.submit("Before compaction.")).get_result()
            compacting = asyncio.create_task(session.compact())
            await asyncio.to_thread(entered.wait, 5)
            assert entered.is_set()
            compacting.cancel()
            try:
                await asyncio.sleep(0)
                assert not compacting.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await compacting
            assert (
                await (await session.submit("After cancellation.")).get_result()
            ).final_output == "Model answer."
    assert len(requests) == 3


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("warm", [False, True])
async def test_private_run_keeps_custom_backend_before_first_request(
    cancel: bool, warm: bool
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    model_calls = []
    http_calls = []

    class Model:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            model_calls.append(request)
            entered.set()
            if cancel and len(model_calls) == (2 if warm else 1):
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Application model answer."}],
                    }
                ]
            )

    async def unexpected_provider(request):
        http_calls.append(await request.json())
        return web.json_response({"error": {"message": "Unexpected provider fallback"}}, status=400)

    app = web.Application()
    app.router.add_post("/v1/responses", unexpected_provider)
    async with timeout(20), TestServer(app) as server:
        async with NativeProvider(
            base_url=str(server.make_url("/v1")), model_backend=Model(), request_max_retries=0
        ) as provider:
            if warm:
                assert (
                    await Agent(instructions="Answer.", provider=provider).run("Warm.")
                ).final_output == "Application model answer."
                entered.clear()
            running = asyncio.create_task(
                Agent(instructions="Answer.", provider=provider).run("Hello.")
            )
            admission = None
            try:
                if cancel:
                    admission = asyncio.create_task(entered.wait())
                    await asyncio.wait((running, admission), return_when=asyncio.FIRST_COMPLETED)
                    if running.done():
                        await running
                    await admission
                    running.cancel()
                    await cancelled.wait()
                    assert not running.done()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await running
                else:
                    assert (await running).final_output == "Application model answer."
            finally:
                if admission is not None:
                    admission.cancel()
                    await asyncio.gather(admission, return_exceptions=True)
                release.set()
                if not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
    assert len(model_calls) == (2 if warm else 1)
    assert http_calls == []


@pytest.mark.parametrize("control", ["instructions", "extra_controls", "unknown"])
async def test_chat_effective_request_controls(control: str) -> None:
    seen = []
    prepared = []
    extras = {"response_format": {"type": "json_object"}}

    async def respond(http):
        seen.append(await http.json())
        return web.json_response(
            {
                "id": "chat_prepared",
                "object": "chat.completion",
                "created": 0,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"ok"}'},
                    }
                ],
            }
        )

    async def edit(turn, request, **kwargs):
        prepared.append(request["instructions"])
        request["instructions"] = "Effective prepared instructions."
        return request

    settings: ModelSettings = {
        "extra_body": extras
        if control == "extra_controls"
        else {"unsupported_chat_control": "synthetic"}
        if control == "unknown"
        else {}
    }
    app = web.Application()
    app.router.add_post("/v1/chat/completions", respond)
    async with (
        TestServer(app) as server,
        AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client,
        NativeProvider(model_backend=ChatCompletionsModel(client, "chat-model")) as provider,
    ):
        agent = Agent(
            model="gpt-5.2",
            instructions="Original instructions.",
            model_settings=settings,
            provider=provider,
        )
        if control == "unknown":
            with pytest.raises(ValueError, match="unsupported request controls"):
                await agent.run("Answer.")
            assert seen == []
            return
        result = await agent.run(
            "Answer.", **({"prepare_request": edit} if control == "instructions" else {})
        )
        assert result.final_output == '{"answer":"ok"}'
    assert len(seen) == 1
    if control == "instructions":
        assert len(prepared) == 1
        assert seen[0]["messages"][0] == {
            "role": "developer",
            "content": [{"type": "text", "text": "Effective prepared instructions."}],
        }
        wire = json.dumps(seen[0]["messages"])
        assert wire.count("Effective prepared instructions.") == 1
        content = [
            part["text"]
            for message in seen[0]["messages"]
            for part in message.get("content", [])
            if part.get("type") == "text"
        ]
        assert prepared[0] not in content
        assert any("Original instructions." in text for text in content)
    else:
        assert all(seen[0][key] == value for key, value in extras.items())


@pytest.mark.parametrize("tracing_disabled", [False, True])
def test_no_tool_one_shot_run_selects_custom_model_before_native_sampling(tracing_disabled):
    # Native telemetry configuration belongs to a fresh process, before its first Session.
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[3]
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(root / "src"),
            str(root / "packages/codex/src"),
            str(root / "packages/codex"),
            env.get("PYTHONPATH", ""),
        ]
    )
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; "
            "from tests.test_native_model_backend import _no_tool_one_shot_case; "
            "asyncio.run(_no_tool_one_shot_case(sys.argv[1] == 'True'))",
            str(tracing_disabled),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _no_tool_one_shot_case(tracing_disabled: bool) -> None:
    await telemetry.configure()
    telemetry.set_tracing_disabled(tracing_disabled)
    try:
        seen: list[ModelRequest] = []

        class Model:
            def complete(self, request: ModelRequest) -> ModelResponse:
                seen.append(request)
                return ModelResponse(
                    [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "custom one-shot result"}],
                        }
                    ]
                )

        responses = ResponsesFixture()
        responses.respond(assistant_output("unexpected HTTP fallback"))
        app = web.Application()
        app.router.add_post("/v1/responses", responses.handle)
        async with TestServer(app) as server, timeout(20):
            async with NativeProvider(
                base_url=str(server.make_url("/v1")), model_backend=Model()
            ) as provider:
                agent = Agent(instructions="Answer briefly.", provider=provider)
                result = await agent.run("First no-tool request.")
                assert result.final_output == "custom one-shot result"
                assert len(seen) == 1
                assert any("First no-tool request." in str(item) for item in seen[0].body["input"])
                assert responses.requests == []
                repeated = await agent.run("Second no-tool request.")
                assert repeated.final_output == "custom one-shot result"
                assert len(seen) == 2
                assert responses.requests == []
    finally:
        telemetry.set_tracing_disabled(False)

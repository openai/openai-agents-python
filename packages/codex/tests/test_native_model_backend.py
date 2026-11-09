"""Native application model producers retain Core tool and cancellation ownership."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    ChatCompletionsModel,
    ModelRequest,
    ModelResponse,
    NativeProvider,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout


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
                model_settings={"verbosity": "high", "top_logprobs": 1},
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

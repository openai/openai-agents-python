"""Native application model producers retain Core tool and cancellation ownership."""

from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize("native_model", [None, "fixture-chat-standard"])
async def test_chat_client_adapter_stays_inside_native_tool_loop(native_model: str | None) -> None:
    seen = []

    async def create(raw_request):
        request = await raw_request.json()
        seen.append(request)
        if len(seen) == 1:
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "call_echo",
                        "function": {"name": "echo", "arguments": '{"value":"from-tool"}'},
                    }
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

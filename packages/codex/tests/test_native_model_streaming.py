"""Native model streams exercise the normal Session, turn, and tool boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agents import (
    Agent,
    ModelRequest,
    ModelResponse,
    NativeProvider,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, text_response


def completed(response_id: str) -> dict[str, Any]:
    return {"type": "response.completed", "response": {"id": response_id}}


async def test_model_stream_delivers_text_before_completion() -> None:
    allow_completion = asyncio.Event()

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming request entered complete()")

        async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "response.created", "response": {"id": "stream-response"}}
            added, delta, done = text_response("stream-item", "incremental")
            yield added
            yield delta
            await allow_completion.wait()
            yield done
            yield completed("stream-response")

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        async with Agent(
            instructions="Respond to the user.", model="custom-model", provider=provider
        ).session() as session:
            turn = await session.submit("Begin.")
            async for event in turn.events():
                if event.type == "output_text_delta":
                    assert (event.item_id, event.delta) == ("stream-item", "incremental")
                    break
            else:
                pytest.fail("the stream ended before the text delta")
            allow_completion.set()
            assert (await turn.get_result()).final_output == "incremental"


@pytest.mark.parametrize("terminal", ["complete", "missing", "duplicate"])
async def test_model_stream_admits_tools_after_one_terminal(terminal: str) -> None:
    effects: list[str] = []

    @function_tool
    def record(value: str) -> str:
        effects.append(value)
        return "recorded"

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming request entered complete()")

        async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
            if effects:
                assert any(
                    item.get("type") == "function_call_output" and item.get("call_id") == "one-call"
                    for item in request.body["input"]
                )
                yield assistant_output("recorded")
                yield completed("after-tool")
                return

            yield function_call("record", {"value": "once"}, "one-call")
            if terminal != "missing":
                yield completed("before-tool")
            if terminal == "duplicate":
                yield completed("duplicate")

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(
            instructions="Use the requested tool.",
            model="custom-model",
            tools=[record],
            provider=provider,
        )
        if terminal == "complete":
            assert (await agent.run("Record once.")).final_output == "recorded"
        else:
            with pytest.raises(RuntimeError):
                await agent.run("Record once.")
    assert effects == (["once"] if terminal == "complete" else [])


async def test_model_stream_cancellation_joins_iterator_before_reusing_session() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()
    never = asyncio.Event()

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming request entered complete()")

        async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
            if started.is_set():
                yield assistant_output("next turn")
                yield completed("next-turn")
                return
            try:
                yield {"type": "response.created", "response": {"id": "cancelled-stream"}}
                started.set()
                await never.wait()
            finally:
                closed.set()

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        async with Agent(
            instructions="Respond to the user.", model="custom-model", provider=provider
        ).session() as session:
            turn = await session.submit("Begin.")
            await started.wait()
            await turn.cancel()
            assert closed.is_set()
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            next_turn = await session.submit("Continue.")
            assert (await next_turn.get_result()).final_output == "next turn"

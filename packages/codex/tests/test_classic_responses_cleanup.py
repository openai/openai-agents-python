"""Classic Responses retains its existing stream cleanup behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import pytest
from openai.types.responses import ResponseCompletedEvent, ResponseTextDeltaEvent

from agents.classic import Agent, ModelSettings, RunConfig, Runner
from agents.classic.items import TResponseStreamEvent
from agents.classic.models.interface import ModelTracing
from agents.classic.testing import ScriptedModel, assistant_message


async def terminal() -> ResponseCompletedEvent:
    source = ScriptedModel([[assistant_message("done")]])
    completed: ResponseCompletedEvent | None = None
    async for event in source.stream_response(
        system_instructions=None,
        input="hello",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        if isinstance(event, ResponseCompletedEvent):
            completed = event
    assert completed is not None, "ScriptedModel must complete"
    return completed


def delta(text: str = "live") -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        type="response.output_text.delta",
        delta=text,
        item_id="message",
        output_index=0,
        content_index=0,
        sequence_number=0,
        logprobs=[],
    )


@asynccontextmanager
async def _gated_builtin_responses_transport(
    *,
    fail_close: bool = False,
    read_end: Literal["blocked", "error"] = "blocked",
    completion: ResponseCompletedEvent | None = None,
) -> AsyncIterator[Any]:
    """Exercise the responses.create fallback with an HTTP response close gate."""
    from types import SimpleNamespace

    import httpx2

    from agents.classic.models.openai_responses import OpenAIResponsesModel

    state = SimpleNamespace(
        reading=asyncio.Event(),
        close_started=asyncio.Event(),
        cleanup_release=asyncio.Event(),
        close_finished=asyncio.Event(),
        transport_closed=asyncio.Event(),
        close_calls=0,
        requests=0,
    )

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise AssertionError("The API boundary supplies parsed events")
            yield b""

        async def aclose(self) -> None:
            state.close_calls += 1
            state.close_started.set()
            await state.cleanup_release.wait()
            state.close_finished.set()
            if fail_close:
                try:
                    raise OSError("inner transport close diagnostic")
                except OSError as error:
                    raise RuntimeError("synthetic HTTP close failure") from error

    async def handle(request: httpx2.Request) -> httpx2.Response:
        state.requests += 1
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    class Transport(httpx2.MockTransport):
        async def aclose(self) -> None:
            state.transport_closed.set()
            await super().aclose()

    http = httpx2.AsyncClient(transport=Transport(handle), trust_env=False)

    class TypedEvents:
        def __init__(self, response: httpx2.Response) -> None:
            self.response = response
            self.first = True

        def __aiter__(self) -> TypedEvents:
            return self

        async def __anext__(self) -> TResponseStreamEvent:
            if self.first:
                self.first = False
                return completion if completion is not None else delta()
            if completion is not None:
                raise StopAsyncIteration
            state.reading.set()
            if read_end == "error":
                raise ValueError("synthetic noncancel read failure")
            await asyncio.Future()
            raise AssertionError("A cancelled read must not produce another event")

        async def aclose(self) -> None:
            await self.response.aclose()

    async def create(**kwargs: Any) -> TypedEvents:
        assert kwargs["stream"] is True
        response = await http.send(
            http.build_request("POST", "https://synthetic.invalid/responses"), stream=True
        )
        return TypedEvents(response)

    client = SimpleNamespace(
        responses=SimpleNamespace(create=create),
        base_url="https://synthetic.invalid",
        is_closed=lambda: http.is_closed,
        close=http.aclose,
    )
    assert not hasattr(client.responses, "with_streaming_response")
    state.client = client
    state.model = OpenAIResponsesModel(model="gpt-5-chat-latest", openai_client=client)
    try:
        yield state
    finally:
        state.cleanup_release.set()
        if state.close_started.is_set():
            await asyncio.wait_for(state.close_finished.wait(), 15)
        await client.close()


@pytest.mark.parametrize("read_end", ["blocked", "error"])
async def test_classic_cancellation_keeps_background_http_close(
    read_end: Literal["blocked", "error"],
) -> None:
    async with _gated_builtin_responses_transport(read_end=read_end) as state:
        running = Runner.run_streamed(
            Agent("Classic cleanup", model=state.model),
            "hello",
            run_config=RunConfig(tracing_disabled=True),
        )

        async def consume() -> None:
            async for _event in running.stream_events():
                pass

        reader = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(state.reading.wait(), 15)
            if read_end == "error":
                await asyncio.wait_for(state.close_started.wait(), 15)
            running.cancel()
            await asyncio.wait_for(state.close_started.wait(), 15)
            await asyncio.wait_for(reader, 15)
            assert running.is_complete
            assert not state.close_finished.is_set()
            assert state.close_calls == 1 and not state.client.is_closed()
            state.cleanup_release.set()
            await asyncio.wait_for(state.close_finished.wait(), 15)
            assert not state.transport_closed.is_set()
        finally:
            state.cleanup_release.set()
            running.cancel()
            await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), 15)


async def test_classic_terminal_close_failure_preserves_completed_answer() -> None:
    async with _gated_builtin_responses_transport(
        fail_close=True, completion=await terminal()
    ) as state:
        running = Runner.run_streamed(
            Agent("Classic terminal cleanup", model=state.model),
            "hello",
            run_config=RunConfig(tracing_disabled=True),
        )

        async def consume() -> str:
            async for _event in running.stream_events():
                pass
            return running.final_output

        reader = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(state.close_started.wait(), 15)
            assert not reader.done() and state.close_calls == 1
            state.cleanup_release.set()
            assert await asyncio.wait_for(reader, 15) == "done"
            assert state.close_finished.is_set()
            assert state.close_calls == state.requests == 1
            assert not state.client.is_closed() and not state.transport_closed.is_set()
        finally:
            state.cleanup_release.set()
            await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), 15)

"""Native model streams exercise the normal Session, turn, and tool boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agents import (
    Agent,
    ModelRequest,
    ModelResponse,
    NativeError,
    NativeProvider,
    Session,
    TurnStoppedError,
    function_tool,
)
from agents.observation import ResponseFactReceived
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, text_response


def completed(response_id: str) -> dict[str, Any]:
    return {"type": "response.completed", "response": {"id": response_id}}


async def test_model_stream_rechecks_admission_before_next_iterator_entry() -> None:
    admitted = True
    closed = False

    async def stream(request: object) -> AsyncIterator[dict[str, Any]]:
        nonlocal closed
        try:
            yield {"type": "response.created", "response": {"id": "revoked"}}
            pytest.fail("producer advanced after losing its request")
        finally:
            closed = True

    async def accept_last_frame(request_id: int, data: str) -> bool:
        nonlocal admitted
        admitted = False
        return True

    session = cast(
        Session,
        SimpleNamespace(
            _native=SimpleNamespace(
                _start_model_producer_stream=lambda request_id: True,
                _push_model_producer_event=accept_last_frame,
            )
        ),
    )
    assert not await Session._produce_model_stream(session, stream, object(), 1, lambda: admitted)
    assert closed


@pytest.mark.parametrize("top_logprobs", [None, 0])
async def test_model_stream_uses_native_probability_retention(top_logprobs: int | None) -> None:
    private_token = "application-stream-private-probability"

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming request entered complete()")

        async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "response.created", "response": {"id": "stream-facts"}}
            added, delta, done = text_response("item-with-probability", "readable")
            delta["logprobs"] = [{"token": private_token, "logprob": -0.5, "top_logprobs": []}]
            yield added
            yield delta
            yield done
            yield completed("stream-facts")

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        result = await Agent(
            instructions="Respond.",
            model="custom-model",
            provider=provider,
            model_settings={} if top_logprobs is None else {"top_logprobs": top_logprobs},
        ).run("Read.")
        assert result.final_output == "readable"
        ordinary = [event async for event in result.turn.events()]
        assert private_token not in json.dumps([asdict(event) for event in ordinary])
        facts = [
            event.record
            async for event in result.turn.events(include_execution_details=True)
            if isinstance(event, ResponseFactReceived)
        ]
        assert (private_token in json.dumps(facts)) == (top_logprobs is not None)


@pytest.mark.parametrize(
    ("failed_event", "reason"),
    [
        (
            {"type": "response.refusal.done", "refusal": "application-private-refusal"},
            "provider_refusal",
        ),
        (
            {"type": "response.output_item.done", "item": {"type": "message", "role": 3}},
            "undecodable_output_item",
        ),
    ],
)
async def test_model_stream_reports_typed_integrity_without_running_tools(
    failed_event: dict[str, Any], reason: str
) -> None:
    effects: list[str] = []
    closed = False

    @function_tool
    def record(value: str) -> str:
        effects.append(value)
        return "recorded"

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming request entered complete()")

        async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
            nonlocal closed
            try:
                yield {"type": "response.created", "response": {"id": "invalid-output"}}
                yield function_call("record", {"value": "must not run"}, "held-call")
                yield failed_event
                pytest.fail("producer advanced after a terminal integrity error")
            finally:
                closed = True

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        async with Agent(
            instructions="Respond.",
            model="custom-model",
            provider=provider,
            tools=[record],
        ).session() as session:
            turn = await session.submit("Record.")
            with pytest.raises(NativeError) as caught:
                await turn.get_result()
            assert caught.value.response_integrity_reason == reason
            assert "application-private-refusal" not in str(caught.value)
    assert effects == []
    assert closed


async def test_model_stream_records_incomplete_usage_before_content_filter() -> None:
    effects: list[str] = []
    response_ids: list[str] = []

    @function_tool
    def record(value: str) -> str:
        """Record a side effect when the tool is admitted."""
        effects.append(value)
        return "recorded"

    class Model:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming request entered complete()")

        async def stream(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
            response_id = f"filtered-application-stream-{len(response_ids)}"
            response_ids.append(response_id)
            yield {"type": "response.created", "response": {"id": response_id}}
            yield function_call("record", {"value": "must not run"}, "held-call")
            yield {
                "type": "response.incomplete",
                "response": {
                    "id": response_id,
                    "status": "incomplete",
                    "incomplete_details": {"reason": "content_filter"},
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 3,
                        "total_tokens": 14,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            }

    async with timeout(20), NativeProvider(model_backend=Model()) as provider:
        async with Agent(
            instructions="Respond.",
            model="custom-model",
            provider=provider,
            tools=[record],
        ).session() as session:
            turn = await session.submit("Record.")
            with pytest.raises(NativeError):
                await turn.get_result()
            [recorded] = [entry async for entry in session.turns()]
            assert recorded["id"] == turn.turn_id
            observations = recorded.get("responses")
            assert observations
            assert [observation.get("response_id") for observation in observations] == response_ids
            for observation in observations:
                assert observation.get("status") == "incomplete"
                assert observation.get("incomplete_reason") == "content_filter"
                usage = observation.get("usage")
                assert usage is not None
                assert usage.get("input_tokens") == 11
                assert usage.get("output_tokens") == 3
    assert effects == []


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

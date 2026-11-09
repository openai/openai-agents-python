"""Public terminal functions use accepted native receipts and one durable result."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel, field_serializer, field_validator

from agents import (
    Agent,
    CompletionContext,
    ContinueTurn,
    FunctionTool,
    NativeProvider,
    OutputValidationError,
    SessionState,
    StopTurn,
    ToolRef,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import LookupArgs, next_request


def provider(peer: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=peer.url, api_key="synthetic-terminal-tools")


@pytest.mark.parametrize("finish", [False, True])
async def test_default_off_and_one_request_terminal_result(
    responses: ResponsesFixture, finish: bool
) -> None:
    effects: list[str] = []

    @function_tool(finish_turn=finish)
    def done() -> str:
        effects.append("once")
        return "tool result"

    responses.respond(function_call("done", {}, "done-call"))
    if not finish:
        responses.respond(assistant_output("assistant result"))
    async with (
        timeout(30),
        Agent(
            instructions="Use done.", model="gpt-5.5", tools=[done], provider=provider(responses)
        ).session() as session,
    ):
        result = await (await session.submit("go")).get_result()
        assert result.final_output == ("tool result" if finish else "assistant result")
        if not finish:
            assert result.tool_call is None
        if finish:
            assert result.tool_call is not None
            assert result.tool_call.tool == ToolRef("done")
            assert result.tool_call.call_id == "done-call"
            assert result.tool_call.route == "direct"
            state = await session.save_state()
            terminal = [
                item["payload"]
                for item in state.history
                if item.get("type") == "event_msg"
                and item.get("payload", {}).get("tool_completion")
            ]
            assert len(terminal) == 1
            assert terminal[0]["tool_completion"]["final_output"] == "tool result"
            assert terminal[0].get("last_agent_message") is None
    assert effects == ["once"]
    assert "finish_turn" not in json.dumps(responses.requests[0]["tools"])
    assert len(responses.requests) == (1 if finish else 2)


async def test_structured_terminal_validates_serializes_and_calls_policy_once(
    responses: ResponsesFixture,
) -> None:
    counts = {"validation": 0, "serialization": 0, "policy": 0}

    class Final(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def validate_value(cls, value: int) -> int:
            counts["validation"] += 1
            return value

        @field_serializer("value")
        def serialize_value(self, value: int) -> int:
            counts["serialization"] += 1
            return value

    @function_tool(finish_turn=True, namespace="billing")
    def done() -> dict[str, int]:
        return {"value": 7}

    async def before_complete(ctx: CompletionContext[Any]) -> None:
        counts["policy"] += 1
        assert ctx.text is None and ctx.tool_call is not None
        assert ctx.tool_call.tool == ToolRef("done", "billing")
        assert isinstance(ctx.final_output, Final)
        ctx.final_output.value = 999  # Policy receives a detached observation.

    event = function_call("done", {}, "qualified-done")
    event["item"]["namespace"] = "billing"
    responses.respond(event)
    agent = Agent(
        instructions="Use done.",
        model="gpt-5.5",
        output_type=Final,
        tools=[done],
        provider=provider(responses),
        before_complete=before_complete,
    )
    async with timeout(30), agent.session() as session:
        completed = await (await session.submit("finish")).get_result()
        assert isinstance(completed.final_output, Final)
        assert completed.final_output.value == 7
        assert completed.tool_call is not None and completed.tool_call.tool == ToolRef(
            "done", "billing"
        )
    assert counts == {"validation": 1, "serialization": 1, "policy": 1}
    assert len(responses.requests) == 1


async def test_original_call_order_wins_after_reversed_callback_completion(
    responses: ResponsesFixture,
) -> None:
    second_finished = asyncio.Event()
    effects: list[str] = []

    @function_tool(finish_turn=True)
    async def first() -> str:
        await second_finished.wait()
        effects.append("first")
        return "first result"

    @function_tool(finish_turn=True)
    def second() -> str:
        effects.append("second")
        second_finished.set()
        return "second result"

    responses.respond(
        function_call("first", {}, "first-call"), function_call("second", {}, "second-call")
    )
    async with (
        timeout(30),
        Agent(
            instructions="Use both.",
            model="gpt-5.5",
            tools=[first, second],
            provider=provider(responses),
        ).session(max_function_concurrency=2) as session,
    ):
        completed = await (await session.submit("finish")).get_result()
        assert completed.final_output == "first result"
        assert completed.tool_call is not None and completed.tool_call.call_id == "first-call"
    assert effects == ["second", "first"]
    assert len(responses.requests) == 1


@pytest.mark.parametrize("decision", ["continue", "stop"])
async def test_completion_policy_owns_tool_proposals(
    responses: ResponsesFixture, decision: str
) -> None:
    calls: list[tuple[str | None, bool]] = []

    @function_tool(finish_turn=True)
    def done() -> str:
        return "tool candidate"

    def policy(ctx: CompletionContext[Any]) -> ContinueTurn | StopTurn | None:
        calls.append((ctx.text, ctx.tool_call is not None))
        if ctx.tool_call is not None:
            return (
                ContinueTurn("answer naturally")
                if decision == "continue"
                else StopTurn("policy stopped")
            )
        return None

    responses.respond(function_call("done", {}, "done"))
    if decision == "continue":
        responses.respond(assistant_output("natural"))
    async with (
        timeout(30),
        Agent(
            instructions="Use done.",
            model="gpt-5.5",
            tools=[done],
            provider=provider(responses),
            before_complete=policy,
        ).session() as session,
    ):
        handle = await session.submit("go")
        if decision == "stop":
            with pytest.raises(TurnStoppedError, match="policy"):
                await handle.get_result()
        else:
            result = await handle.get_result()
            assert result.final_output == "natural" and result.tool_call is None
    assert calls == (
        [(None, True), ("natural", False)] if decision == "continue" else [(None, True)]
    )
    assert len(responses.requests) == (2 if decision == "continue" else 1)


async def test_bad_terminal_output_preserves_owning_validation_error_without_repair(
    responses: ResponsesFixture,
) -> None:
    class Final(BaseModel):
        value: int

    @function_tool(finish_turn=True)
    def done() -> str:
        return '{"value":"invalid"}'

    responses.respond(function_call("done", {}, "done"))
    async with (
        timeout(30),
        Agent(
            instructions="Use done.",
            model="gpt-5.5",
            output_type=Final,
            tools=[done],
            provider=provider(responses),
        ).session() as session,
    ):
        with pytest.raises(OutputValidationError):
            await (await session.submit("go")).get_result()
    assert len(responses.requests) == 1


@pytest.mark.parametrize("remove_outcomes", [False, True])
async def test_fresh_manual_finisher_after_json_restore_needs_no_model_request(
    responses: ResponsesFixture, remove_outcomes: bool
) -> None:
    manual = FunctionTool(
        name="finish",
        description="Finish once.",
        input_type=LookupArgs,
        output_type=str,
        finish_turn=True,
    )
    responses.respond(function_call("finish", {"key": "A"}, "manual-finish"))
    agent = Agent(
        instructions="Use finish.", model="gpt-5.5", tools=[manual], provider=provider(responses)
    )
    async with timeout(30), agent.session() as source:
        await source.submit("finish")
        await next_request(source.interactions(), manual)
        payload = json.loads((await source.suspend()).to_json())
    if remove_outcomes:
        payload["native_continuation"].pop("tool_outcomes", None)
    state = SessionState.from_json(json.dumps(payload))
    async with timeout(30), agent.session(state=state) as restored:
        assert restored.restored_turn is not None
        request = await next_request(restored.interactions(), manual)
        await request.respond("restored result")
        result = await restored.restored_turn.get_result()
        assert result.final_output == "restored result"
        assert result.tool_call is not None and result.tool_call.call_id == "manual-finish"
    assert len(responses.requests) == 1

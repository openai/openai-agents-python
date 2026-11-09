"""Captured profile catalogs keep terminal authority through activation and restore."""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import AfterValidator

from agents import Agent, FunctionTool, ToolRef, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize("activation", ["handoff", "idle"])
@pytest.mark.parametrize("override", [False, True], ids=["profile-output", "explicit-output"])
async def test_captured_profile_terminal_flag_ends_without_an_extra_model_request(
    responses: ResponsesFixture, activation, override
):
    effects = []
    completions = []
    validations = []

    def validate_a(value):
        raise AssertionError("A cannot convert B completion")

    def validate_b(value):
        validations.append("B")
        return value

    output_a = Annotated[int, AfterValidator(validate_a)]
    output_b = Annotated[int, AfterValidator(validate_b)]

    def before_complete(ctx):
        completions.append(("B", ctx.final_output, ctx.tool_call.tool))

    def wrong_owner(ctx):
        raise AssertionError("A cannot accept B completion")

    @function_tool(finish_turn=True)
    def done() -> int:
        effects.append("once")
        return 7

    target = Agent(
        name="B",
        instructions="B owns completion.",
        tools=[done],
        before_complete=before_complete,
        output_type=output_b,
    )
    source = Agent(
        name="A",
        instructions="Route to B.",
        handoffs=[target],
        provider=provider(responses),
        before_complete=wrong_owner,
        output_type=output_a,
    )
    if activation == "handoff":
        responses.respond(function_call("transfer_to_b", {}, "handoff-b"))
    responses.respond(function_call("done", {}, "finish-b"))
    responses.respond(assistant_output("unwanted extra sample"))
    async with timeout(25), source.session() as session:
        if activation == "idle":
            await session.update(agent=target)
        result = await (
            await session.submit("Finish with B", output_type=str if override else None)
        ).get_result()
        assert result.final_output == ("7" if override else 7)
        assert type(result.final_output) is (str if override else int)
        assert result.tool_call is not None and result.tool_call.tool == ToolRef("done")
        assert await session.get_active_agent() is target
    assert effects == ["once"]
    assert completions == [("B", "7" if override else 7, ToolRef("done"))]
    assert type(completions[0][1]) is (str if override else int)
    assert validations == ([] if override else ["B"])
    assert len(responses.requests) == (2 if activation == "handoff" else 1)


async def test_restored_b_manual_completion_uses_fresh_profile_terminal_slots(
    responses: ResponsesFixture,
):
    def graph():
        manual = FunctionTool(
            name="done",
            description="Finish B",
            input_type=LookupArgs,
            output_type=str,
            finish_turn=True,
        )
        target = Agent(name="B", instructions="B owns completion.", tools=[manual])
        source = Agent(
            name="A", instructions="Route to B.", handoffs=[target], provider=provider(responses)
        )
        return source, target, manual

    source, target, original_tool = graph()
    responses.respond(function_call("transfer_to_b", {}, "handoff-b"))
    responses.respond(function_call("done", {"key": "owned"}, "manual-b"))
    async with timeout(30):
        async with source.session() as session:
            await session.submit("Route to B and finish")
            original = await next_request(session.interactions(), original_tool)
            assert await session.get_active_agent() is target
            saved = await session.suspend()
        fresh_source, fresh_target, fresh_tool = graph()
        responses.respond(assistant_output("unwanted extra sample"))
        async with fresh_source.session(state=SessionState.from_json(saved.to_json())) as restored:
            assert await restored.get_active_agent() is fresh_target
            current = await next_request(restored.interactions(), fresh_tool)
            assert current.call_id == original.call_id == "manual-b"
            assert original_tool.match(current) is None
            await current.respond("restored profile terminal result")
            assert restored.restored_turn is not None
            result = await restored.restored_turn.get_result()
            assert result.final_output == "restored profile terminal result"
            assert result.tool_call is not None and result.tool_call.tool == ToolRef("done")
    assert len(responses.requests) == 2

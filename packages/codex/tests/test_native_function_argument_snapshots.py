"""Validated mappings keep their Python semantics and detached policy views."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any

import pytest
from pydantic import AfterValidator, BaseModel

from agents import Agent, Allow, ReplaceArguments, ToolContext, ToolOutput, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider

ReadOnlyStrings = Annotated[Mapping[str, str], AfterValidator(MappingProxyType)]


class LabelGroups(BaseModel):
    # The validator retains the same mapping twice without changing its type.
    groups: Annotated[list[ReadOnlyStrings], AfterValidator(lambda values: values * 2)]


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("replacement", [False, True])
async def test_readonly_mapping_arguments_keep_policy_snapshots_isolated(
    responses: ResponsesFixture, nested: bool, replacement: bool
) -> None:
    received: list[dict[str, list[str]]] = []
    expected_name = "replacement" if replacement else "original"
    expected_tags = [expected_name]

    def input_values(name: str) -> dict[str, Any]:
        value = {"name": name}
        return {
            "labels": {"groups": [value]} if nested else value,
            "details": {"tags": [name]},
        }

    def mapping(value: Any) -> Mapping[str, str]:
        if nested:
            assert isinstance(value, LabelGroups)
            assert value.groups[0] is value.groups[1]
            value = value.groups[0]
        assert isinstance(value, MappingProxyType)
        return value

    def before(ctx: ToolContext[None]) -> Allow | ReplaceArguments:
        snapshot = ctx.effective_arguments
        assert mapping(snapshot["labels"])["name"] == "original"
        with pytest.raises(TypeError):
            snapshot["labels"] = {}
        snapshot["details"]["tags"].append("before snapshot edit")
        if replacement:
            return ReplaceArguments(input_values("replacement"))
        return Allow()

    def execute(ctx: ToolContext[None], labels: Any, details: dict[str, list[str]]) -> str:
        assert mapping(labels)["name"] == expected_name
        assert mapping(ctx.effective_arguments["labels"])["name"] == expected_name
        assert ctx.effective_arguments["labels"] is not labels
        assert ctx.original_arguments == input_values("original")
        assert details == {"tags": expected_tags}
        details["tags"].append("callback edit")
        received.append(details)
        return expected_name

    def after(ctx: ToolContext[None], output: ToolOutput) -> ToolOutput:
        tags = ctx.effective_arguments["details"]["tags"]
        assert tags == expected_tags + ([] if replacement else ["before snapshot edit"])
        tags.append("after snapshot edit")
        assert mapping(ctx.effective_arguments["labels"])["name"] == expected_name
        return output

    if nested:

        @function_tool(before=before, after=after)
        def labels(
            ctx: ToolContext[None], labels: LabelGroups, details: dict[str, list[str]]
        ) -> str:
            return execute(ctx, labels, details)

    else:

        @function_tool(before=before, after=after)
        def labels(
            ctx: ToolContext[None], labels: ReadOnlyStrings, details: dict[str, list[str]]
        ) -> str:
            return execute(ctx, labels, details)

    responses.respond(function_call("labels", input_values("original"), "labels-call"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Read labels.", tools=[labels], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        result = await (await session.submit("Read labels.")).get_result()
        assert result.final_output == "done"
    assert received == [{"tags": [expected_name, "callback edit"]}]
    assert tool_output(responses.requests[-1], "labels-call") == expected_name


async def test_readonly_mapping_callback_without_context_or_policy(
    responses: ResponsesFixture,
) -> None:
    received: list[Mapping[str, str]] = []

    @function_tool
    def labels(values: ReadOnlyStrings) -> str:
        assert isinstance(values, MappingProxyType)
        received.append(values)
        return values["name"]

    responses.respond(function_call("labels", {"values": {"name": "Ada"}}, "plain-call"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Read labels.", tools=[labels], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        result = await (await session.submit("Read labels.")).get_result()
        assert result.final_output == "done"
    assert received == [{"name": "Ada"}]
    assert tool_output(responses.requests[-1], "plain-call") == "Ada"

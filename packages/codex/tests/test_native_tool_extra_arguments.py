"""Surplus function arguments remain an explicit, top-level opt-in."""

from typing import Annotated

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents import Agent, Allow, ToolContext, function_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import function_declarations, provider


def test_extra_arguments_option_preserves_closed_declaration():
    async def callback(command: str) -> str:
        return command

    shell = function_tool(extra_arguments="ignore")(callback)
    default = function_tool(callback)
    assert shell.input_type.model_validate({"command": "pwd", "timeout": 30}).model_dump() == {
        "command": "pwd"
    }
    assert shell.input_type.model_json_schema() == default.input_type.model_json_schema()
    assert shell.input_type.model_json_schema()["additionalProperties"] is False


async def test_surplus_arguments_are_rejected_by_default(responses):
    effects = []
    hooks = []

    def before(context):
        hooks.append(context)
        return Allow()

    async def shell(command: str) -> str:
        effects.append(command)
        return command

    tool = function_tool(before=before)(shell)
    responses.respond(function_call("shell", {"command": "pwd", "timeout": 30}, "surplus"))
    async with Agent(
        instructions="Use shell.", tools=[tool], provider=provider(responses)
    ).session() as session:
        with pytest.raises(ValidationError) as error:
            await (await session.submit("begin")).get_result()
    assert error.value.errors()[0]["type"] == "extra_forbidden"
    assert effects == []
    assert hooks == []


async def test_ignoring_surplus_arguments_preserves_alias_defaults_and_context(responses):
    effects = []
    contexts = []
    hooks = []
    original = {"command": "pwd", "timeout": 30}
    application = {"owner": "synthetic"}

    def before(context):
        hooks.append(context)
        assert context.original_arguments == original
        assert context.effective_arguments == {"cmd": "pwd", "label": "default"}
        return Allow()

    @function_tool(extra_arguments="ignore", before=before)
    async def shell(
        context: ToolContext[dict[str, str]],
        cmd: Annotated[str, Field(alias="command")],
        label: str = "default",
    ) -> str:
        effects.append((cmd, label))
        contexts.append(context)
        return "synthetic-result"

    responses.respond(function_call("shell", original, "surplus"))
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Use shell.", tools=[shell], provider=provider(responses)
    ).session(context=application) as session:
        handle = await session.submit("begin")
        assert (await handle.get_result()).final_output == "done"

    assert effects == [("pwd", "default")]
    assert len(hooks) == len(contexts) == 1
    [context] = contexts
    assert context.context is application
    assert context.submission is handle
    assert context.call_id == "surplus"
    assert context.original_arguments == original
    assert context.effective_arguments == {"cmd": "pwd", "label": "default"}
    [declaration] = function_declarations(responses.requests[0])
    assert declaration["strict"] is False
    assert declaration["parameters"]["additionalProperties"] is False
    assert set(declaration["parameters"]["properties"]) == {"command", "label"}
    assert len(responses.requests) == 2
    assert "synthetic-result" in str(responses.requests[1]["input"])
    assert not responses.replies


@pytest.mark.parametrize("arguments", [{"timeout": 30}, {"command": 7, "timeout": 30}])
async def test_ignoring_surplus_arguments_still_rejects_invalid_declared_values(
    responses, arguments
):
    effects = []
    hooks = []

    def before(context):
        hooks.append(context)
        return Allow()

    @function_tool(extra_arguments="ignore", before=before)
    async def shell(command: str) -> str:
        effects.append(command)
        return command

    responses.respond(function_call("shell", arguments, "invalid"))
    async with Agent(
        instructions="Use shell.", tools=[shell], provider=provider(responses)
    ).session() as session:
        with pytest.raises(ValidationError):
            await (await session.submit("begin")).get_result()
    assert effects == []
    assert hooks == []


class ClosedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str


async def test_ignoring_surplus_arguments_preserves_nested_model_validation(responses):
    effects = []

    @function_tool(extra_arguments="ignore")
    async def shell(payload: ClosedPayload) -> str:
        effects.append(payload.command)
        return payload.command

    responses.respond(
        function_call("shell", {"payload": {"command": "pwd", "timeout": 30}}, "nested")
    )
    async with Agent(
        instructions="Use shell.", tools=[shell], provider=provider(responses)
    ).session() as session:
        with pytest.raises(ValidationError) as error:
            await (await session.submit("begin")).get_result()
    assert error.value.errors()[0]["loc"] == ("payload", "timeout")
    assert effects == []

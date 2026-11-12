"""Original input recovery before policy, approval, or function execution."""

from typing import Any

import pytest
from pydantic import ValidationError

from agents import Agent, Allow, ToolInputErrorContext, function_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


@pytest.mark.parametrize("needs_approval", [False, True])
async def test_original_input_recovery_before_policy_and_execution(responses, needs_approval):
    seen = []
    effects = []
    policies = []

    def recover(context: ToolInputErrorContext[dict[str, str]], error: Exception, original: Any):
        assert isinstance(error, ValidationError)
        assert context.context == {"label": "owning-run"}
        assert context.call_id == "invalid-input"
        assert not hasattr(context, "effective_arguments")
        assert not effects and not policies
        seen.append(original)
        return "Bad lookup input"

    def before(context):
        policies.append("before")
        return Allow()

    @function_tool(input_error_function=recover, needs_approval=needs_approval, before=before)
    async def lookup(key: str) -> str:
        effects.append(key)
        return "record"

    responses.respond(function_call("lookup", {"key": 7}, "invalid-input"))
    if not needs_approval:
        responses.respond(function_call("lookup", {"key": "valid"}, "valid-input"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with agent.session(context={"label": "owning-run"}) as session:
        result = await (await session.submit("begin")).get_result()
    assert result.final_output == "recovered"
    assert seen == [{"key": 7}]
    assert effects == ([] if needs_approval else ["valid"])
    assert policies == ([] if needs_approval else ["before"])
    assert "Bad lookup input" in str(responses.requests[1]["input"])
    assert not responses.replies


@pytest.mark.parametrize("original", [None, 7, [], "7"])
async def test_non_object_original_input_stays_unvalidated(responses, original):
    seen = []

    def recover(context, error, raw):
        assert not hasattr(context, "effective_arguments")
        seen.append(raw)
        return ""

    @function_tool(input_error_function=recover)
    async def lookup(key: str) -> str:
        raise AssertionError("Invalid input entered the body")

    responses.respond(function_call("lookup", original, "non-object"))
    responses.respond(assistant_output("recovered"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        assert (await (await session.submit("begin")).get_result()).final_output == "recovered"
    assert seen == [original]


@pytest.mark.parametrize("needs_approval", [False, True])
async def test_policy_replacement_validation_stays_outside_recovery(responses, needs_approval):
    from agents import ReplaceArguments

    seen = []

    def recover(*args):
        seen.append(True)
        return "retry"

    @function_tool(
        input_error_function=recover,
        needs_approval=needs_approval,
        run_before_approval=needs_approval,
        before=lambda context: ReplaceArguments({"key": 7}),
    )
    async def lookup(key: str) -> str:
        raise AssertionError("Invalid replacement entered the body")

    responses.respond(function_call("lookup", {"key": "valid"}, "replacement"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        with pytest.raises(ValidationError):
            await (await session.submit("begin")).get_result()
    assert not seen


@pytest.mark.parametrize("needs_approval", [False, True])
async def test_original_nested_input_precedes_validator_mutation(responses, needs_approval):
    from typing import Annotated

    from pydantic import BeforeValidator

    seen = []

    def mutate(value):
        value["key"] = 999
        raise ValueError("Invalid nested input")

    def recover(context, error, original):
        assert original == {"payload": {"key": 7}}
        seen.append(original)
        original["payload"]["key"] = 33
        return "retry"

    @function_tool(input_error_function=recover, needs_approval=needs_approval)
    async def lookup(payload: Annotated[dict[str, int], BeforeValidator(mutate)]) -> str:
        raise AssertionError("Invalid input entered the body")

    responses.respond(function_call("lookup", {"payload": {"key": 7}}, "mutated"))
    responses.respond(assistant_output("recovered"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        assert (await (await session.submit("begin")).get_result()).final_output == "recovered"
    assert seen == [{"payload": {"key": 33}}]


@pytest.mark.parametrize("managed", [False, True])
async def test_malformed_original_json_custom_recovery_precedes_policy_and_approval(
    responses, managed
):
    raw = ' {"key": '
    counts = {"format": 0, "before": 0, "approval": 0, "body": 0, "after": 0}

    def recover(context, error, original):
        assert isinstance(error, ValueError)
        assert "EOF" in str(error)
        assert original == raw
        assert context.call_id == "malformed-opt-in"
        assert not hasattr(context, "effective_arguments")
        counts["format"] += 1
        return "Custom malformed input"

    def before(context):
        counts["before"] += 1
        return Allow()

    def approve(context):
        counts["approval"] += 1
        return True

    def after(context, output):
        counts["after"] += 1
        return output

    @function_tool(
        input_error_function=recover,
        needs_approval=approve if managed else False,
        before=before,
        after=after,
    )
    async def lookup(key: str) -> str:
        counts["body"] += 1
        return "unexpected"

    malformed = function_call("lookup", {}, "malformed-opt-in")
    malformed["item"]["arguments"] = raw
    responses.respond(malformed)
    responses.respond(assistant_output("recovered"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        assert (await (await session.submit("begin")).get_result()).final_output == "recovered"
    assert counts == {"format": 1, "before": 0, "approval": 0, "body": 0, "after": 0}
    assert len(responses.requests) == 2
    assert "Custom malformed input" in str(responses.requests[1]["input"])

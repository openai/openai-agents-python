"""Run input formatters retain declared graph and callback authority."""

import pytest

from agents import Agent, function_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


@pytest.mark.parametrize("profiled", [False, True])
@pytest.mark.parametrize("managed", [False, True])
async def test_run_formatter_receives_malformed_json_before_approval(responses, profiled, managed):
    seen, effects, approvals = [], [], []

    def formatter(context):
        assert context.phase == "arguments"
        assert "EOF" in str(context.error)
        seen.append(context.call.call_id)
        return "Run formatter rejected malformed arguments"

    def approve(context):
        approvals.append(context)
        return True

    @function_tool(needs_approval=approve if managed else False)
    async def lookup(key: str) -> str:
        effects.append(key)
        return "unexpected"

    malformed = function_call("lookup", {}, "run-malformed")
    malformed["item"]["arguments"] = '{"key": '
    responses.respond(malformed)
    responses.respond(assistant_output("recovered"))
    target = Agent(name="Other", instructions="Other")
    async with Agent(
        instructions="Use lookup.",
        tools=[lookup],
        handoffs=[target] if profiled else [],
        provider=provider(responses),
    ).session(tool_error_formatter=formatter) as session:
        assert (await (await session.submit("begin")).get_result()).final_output == "recovered"
    assert seen == ["run-malformed"]
    assert effects == approvals == []
    assert "Run formatter rejected malformed arguments" in str(responses.requests[1]["input"])


async def test_restore_refreshes_run_formatter_without_changing_declared_graph(responses):
    seen = []

    def formatter(context):
        seen.append(context.call.call_id)
        return "Restored run formatter"

    @function_tool
    async def lookup(key: str) -> str:
        raise AssertionError("Malformed input entered the body")

    target = Agent(name="Other", instructions="Other")
    agent = Agent(
        instructions="Use lookup.", tools=[lookup], handoffs=[target], provider=provider(responses)
    )
    responses.respond(assistant_output("saved"))
    async with agent.session() as session:
        assert (
            await (await session.submit("save this conversation")).get_result()
        ).final_output == "saved"
        state = await session.save_state()
    malformed = function_call("lookup", {}, "restored-malformed")
    malformed["item"]["arguments"] = '{"key": '
    responses.respond(malformed)
    responses.respond(assistant_output("restored"))
    async with agent.session(state=state, tool_error_formatter=formatter) as session:
        assert (await (await session.submit("continue")).get_result()).final_output == "restored"
    assert seen == ["restored-malformed"]
    assert "Restored run formatter" in str(responses.requests[-1]["input"])


async def test_tool_update_preserves_run_formatter_eligibility(responses):
    seen = []

    def formatter(context):
        seen.append(context.phase)
        return "Updated tool formatter"

    @function_tool
    async def lookup(key: str) -> str:
        raise AssertionError("Malformed input entered the body")

    malformed = function_call("lookup", {}, "updated-malformed")
    malformed["item"]["arguments"] = '{"key": '
    responses.respond(malformed)
    responses.respond(assistant_output("updated"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session(tool_error_formatter=formatter) as session:
        await session.update(tools=[lookup])
        assert (await (await session.submit("begin")).get_result()).final_output == "updated"
    assert seen == ["arguments"]
    assert "Updated tool formatter" in str(responses.requests[1]["input"])


async def test_explicit_formatter_opt_out_preserves_native_rejection(responses):
    seen = []

    def formatter(context):
        seen.append(context)
        return "Must not apply"

    @function_tool(error_function=None)
    async def lookup(key: str) -> str:
        raise AssertionError("Malformed input entered the body")

    malformed = function_call("lookup", {}, "opt-out")
    malformed["item"]["arguments"] = '{"key": '
    responses.respond(malformed)
    responses.respond(assistant_output("rejected"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session(tool_error_formatter=formatter) as session:
        assert (await (await session.submit("begin")).get_result()).final_output == "rejected"
    assert seen == []
    assert "Must not apply" not in str(responses.requests[1]["input"])

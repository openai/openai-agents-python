"""Exercise strict unknown-function handling through the native dispatcher."""

import pytest

from agents import (
    Agent,
    FunctionTool,
    InteractionRequired,
    NativeError,
    SessionState,
    ToolNotFoundBehavior,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider


@pytest.mark.parametrize("recovery", [None, "return_error_to_model"])
async def test_unknown_tool_policy_is_scoped_to_one_turn(
    responses: ResponsesFixture, recovery: ToolNotFoundBehavior | None
) -> None:
    responses.respond(function_call("missing_sdk_function", {}, "strict-miss"))
    responses.respond(function_call("missing_recoverable_function", {}, "recoverable-miss"))
    responses.respond(assistant_output("Handled the tool error."))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))

    async with timeout(20), agent.session() as session:
        strict = await session.submit(
            "Use the missing tool.", tool_not_found_behavior="raise_error"
        )
        with pytest.raises(NativeError, match="missing_sdk_function"):
            await strict.get_result()
        assert len(responses.requests) == 1

        recovered = await session.submit("Try again.", tool_not_found_behavior=recovery)
        assert (await recovered.get_result()).final_output == "Handled the tool error."

    assert len(responses.requests) == 3
    assert "missing_recoverable_function" in tool_output(responses.requests[2], "recoverable-miss")


async def test_unknown_tool_policy_survives_manual_restore(responses: ResponsesFixture) -> None:
    lookup = FunctionTool(
        name="lookup",
        description="Look up a record.",
        input_type=LookupArgs,
        output_type=str,
    )
    responses.respond(function_call("lookup", {"key": "example"}, "manual"))
    responses.respond(function_call("missing_sdk_function", {}, "after-restore"))
    agent = Agent(instructions="Find the record.", tools=[lookup], provider=provider(responses))

    async with timeout(20):
        with pytest.raises(InteractionRequired) as paused:
            await agent.run("Find it.", tool_not_found_behavior="raise_error")
        assert paused.value.state is not None
        saved = SessionState.from_json(paused.value.state.to_json())
        assert any(
            (row["payload"].get("sdk_receipt") or {}).get("tool_not_found_behavior")
            == "raise_error"
            for row in saved.history
            if row["type"] == "turn_context"
        )

        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), lookup)
            turn = restored.active_turn
            assert turn is not None
            with pytest.raises(NativeError, match="missing_sdk_function"):
                await request.respond("found")
                await turn.get_result()

    assert len(responses.requests) == 2

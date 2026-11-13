import json

import pytest

from agents import (
    Agent,
    DecisionHandoff,
    Handoff,
    NativeProvider,
    OutputValidationError,
    ToolContext,
    function_tool,
)
from agents.decisions import DecisionResponse
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


async def test_handoff_invocation_failure_formats_without_transferring_and_can_retry(responses):
    effects = []
    failures = []
    application = {"application": "context"}

    def format_error(context, error):
        assert context.context is application
        failures.append(type(error).__name__)
        return "Routing unavailable; choose another ticket."

    @function_tool(error_function=format_error)
    def route(context: ToolContext[dict[str, str]], ticket: int) -> str:
        effects.append(ticket)
        if ticket == 1:
            raise LookupError("private callback diagnostic")
        return f"accepted-{ticket}"

    target = Agent(name="target", instructions="TARGET_ONLY")
    responses.respond(function_call("route", {"ticket": 1}, "failed"))
    responses.respond(function_call("route", {"ticket": 2}, "accepted"))
    responses.respond(assistant_output("target answered"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic", supports_websockets=False
    ) as provider:
        source = Agent(
            name="source",
            instructions="SOURCE_ONLY",
            provider=provider,
            model="gpt-5.5",
            handoffs=[Handoff(target, route)],
        )
        async with timeout(30), source.session(context=application) as session:
            assert (
                await (await session.submit("Route.")).get_result()
            ).final_output == "target answered"
            assert await session.get_active_agent() is target
    assert effects == [1, 2]
    assert failures == ["LookupError"]
    assert len(responses.requests) == 3
    assert (
        tool_output(responses.requests[1], "failed")
        == "Routing unavailable; choose another ticket."
    )
    assert "private callback diagnostic" not in json.dumps(responses.requests[1])
    assert "SOURCE_ONLY" in json.dumps(responses.requests[1])
    assert "TARGET_ONLY" not in json.dumps(responses.requests[1])
    assert tool_output(responses.requests[2], "accepted") == "accepted-2"
    assert "TARGET_ONLY" in json.dumps(responses.requests[2])


@pytest.mark.parametrize("choice", ["captured", "outside"])
async def test_invocation_time_selection_requires_a_captured_native_target(responses, choice):
    class Decisions:
        async def get_decision(self, request):
            assert request.output_schema["enum"] == ["captured", "reserve"]
            return DecisionResponse(value=choice)

    target = Agent(name="target", instructions="CAPTURED_TARGET", model="gpt-5.5")
    responses.respond(function_call("delegate", {"input": "Route this ticket."}, "choice"))
    if choice == "captured":
        responses.respond(assistant_output("target answered"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic", supports_websockets=False
    ) as provider:
        source = Agent(
            name="source",
            model="gpt-5.5",
            provider=provider,
            instructions="SOURCE_ONLY",
            decisions=Decisions(),
            handoffs=[
                DecisionHandoff(
                    name="delegate",
                    description="Delegate this ticket.",
                    question="Choose a target.",
                    targets={
                        "captured": target,
                        "reserve": Agent(name="reserve", instructions="RESERVE"),
                    },
                )
            ],
        )
        async with timeout(30), source.session() as session:
            turn = await session.submit("Route.")
            if choice == "outside":
                with pytest.raises(OutputValidationError):
                    await turn.get_result()
                assert await session.get_active_agent() is source
                assert len(responses.requests) == 1
            else:
                assert (await turn.get_result()).final_output == "target answered"
                assert await session.get_active_agent() is target
                assert len(responses.requests) == 2

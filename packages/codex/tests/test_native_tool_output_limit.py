"""Provider opening budgets have been controlling native tool result truncation."""

from __future__ import annotations

import pytest

from agents import Agent, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "12000", 2**63])
def test_tool_output_limit_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="tool_output_token_limit"):
        NativeProvider(tool_output_token_limit=value)


@pytest.mark.parametrize("limit", [None, 40_000])
async def test_tool_output_budget_applies_to_fresh_sessions_and_restore(responses, limit):
    payload = "BEGIN-" + "abcdefghijklmnopqrstuvwxyz0123456789" * 3000 + "-END"

    @function_tool
    def large_result() -> str:
        return payload

    async with NativeProvider(
        base_url=responses.url,
        api_key="synthetic-tool-budget",
        tool_output_token_limit=limit,
    ) as provider:
        agent = Agent(instructions="Read the result.", tools=[large_result])
        state = None
        for restore in (False, False, True):
            call_id = f"large-{len(responses.requests)}"
            responses.respond(function_call("large_result", {}, call_id))
            responses.respond(assistant_output("finished"))
            async with (
                timeout(20),
                agent.session(provider=provider, state=state if restore else None) as session,
            ):
                assert (
                    await (await session.submit("Read.")).get_result()
                ).final_output == "finished"
                state = await session.save_state()
            output = tool_output(responses.requests[-1], call_id)
            if limit is None:
                assert len(output) < len(payload)
                assert "tokens truncated" in output
                assert output.startswith("BEGIN-") and output.endswith("-END")
            else:
                assert output == payload
            assert "tool_output_token_limit" not in responses.requests[-1]


async def test_opening_provider_budget_applies_after_handoff(responses):
    payload = "large-result-" * 9000

    @function_tool
    def large_result() -> str:
        return payload

    responses.respond(function_call("transfer_to_target", {}, "route"))
    responses.respond(function_call("large_result", {}, "large"))
    responses.respond(assistant_output("finished"))
    async with (
        NativeProvider(
            base_url=responses.url,
            api_key="synthetic-opening-budget",
            tool_output_token_limit=40_000,
        ) as opening,
        NativeProvider(
            base_url=responses.url,
            api_key="synthetic-destination-budget",
            tool_output_token_limit=1,
        ) as destination,
    ):
        target = Agent(
            name="target", instructions="Read.", provider=destination, tools=[large_result]
        )
        source = Agent(instructions="Transfer.", provider=opening, handoffs=[target])
        async with timeout(20), source.session() as session:
            assert (await (await session.submit("Read.")).get_result()).final_output == "finished"
            assert await session.get_active_agent() is target
    assert tool_output(responses.requests[-1], "large") == payload

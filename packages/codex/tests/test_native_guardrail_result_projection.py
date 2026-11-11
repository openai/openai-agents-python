"""Restored root handles retain the live output-check records."""

from __future__ import annotations

import pytest

from agents import Agent, FunctionTool, GuardrailResult, GuardrailTripwire, OutputGuardrail
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_manual_profiles import save_at_manual_boundary
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize("tripwire", [False, True])
async def test_restored_handle_projects_admitted_output_guardrail_records(responses, tripwire):
    outputs = []

    def check(request, agent):
        outputs.append(request.final_output)
        return GuardrailResult(tripwire, {"output": request.final_output})

    lookup = FunctionTool(
        name="lookup", description="Read.", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=provider(responses),
        output_guardrails=[OutputGuardrail("restored-output", check)],
    )
    responses.respond(function_call("lookup", {"key": "record"}, "manual"))
    async with timeout(30):
        async with agent.session() as original:
            await original.submit("begin")
            await next_request(original.interactions(), lookup)
            saved = await save_at_manual_boundary(original)
        async with agent.session(state=saved) as restored:
            turn = restored.restored_turn
            request = await next_request(restored.interactions(), lookup)
            responses.respond(assistant_output("restored complete"))
            await request.respond("value")
            if tripwire:
                with pytest.raises(GuardrailTripwire) as caught:
                    await turn.get_result()
                expected = caught.value.results
            else:
                completed = await turn.get_result()
                expected = completed.guardrail_results
            assert [record.name for record in expected] == ["restored-output"]
            assert turn.guardrail_results == expected
            assert turn.guardrail_results[0].result.output_info == {"output": "restored complete"}
            assert outputs == ["restored complete"]
            if not tripwire:
                responses.respond(assistant_output("fresh complete"))
                fresh = await restored.submit("fresh")
                await fresh.get_result()
                assert len(fresh.guardrail_results) == 1
                assert turn.guardrail_results == expected

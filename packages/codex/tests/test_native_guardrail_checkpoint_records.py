import json

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    GuardrailResult,
    InputGuardrail,
    NativeError,
    OutputGuardrail,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_manual_tools import next_request


class Empty(BaseModel):
    pass


@pytest.mark.parametrize(
    ("profiled", "legacy"), [(False, False), (True, False), (False, True), (True, True)]
)
async def test_named_guard_diagnostics_survive_manual_restore_without_independent_run_leak(
    responses, profiled, legacy
):
    input_calls = 0

    def check_input(turn, context, source, value):
        nonlocal input_calls
        input_calls += 1
        return GuardrailResult(False, {"phase": "initial", "ordinal": input_calls})

    def check_output(request, source):
        return GuardrailResult(False, {"phase": "completed"})

    lookup = FunctionTool(name="lookup", description="Ask.", input_type=Empty, output_type=str)
    agent_provider = provider(responses)
    agent = Agent(
        instructions="Use lookup.",
        provider=agent_provider,
        tools=[lookup],
        handoffs=[Agent(name="Unused", instructions="Unused.", provider=agent_provider)]
        if profiled
        else [],
        input_guardrails=[InputGuardrail("before-approval", check_input, run_in_parallel=False)],
        output_guardrails=[OutputGuardrail("after-approval", check_output)],
    )
    responses.respond(function_call("lookup", {}, "checkpoint-approval"))
    responses.respond(assistant_output("resumed"))
    responses.respond(assistant_output("independent"))
    async with timeout(25):
        async with agent.session() as original:
            first = await original.submit("begin")
            await next_request(original.interactions(), lookup)
            prior = first.guardrail_results
            assert [r.name for r in prior] == ["before-approval"]
            saved = await original.suspend()
        unavailable_data = json.loads(saved.to_json())
        unavailable_data["guardrail_results"][0]["agent_profile"] = "missing-guard-source"
        unavailable = SessionState.from_json(json.dumps(unavailable_data))
        with pytest.raises(NativeError):
            async with agent.session(state=unavailable):
                pytest.fail("An unavailable diagnostic source must not open a resumed Session")
        assert len(responses.requests) == 1
        imported = SessionState.from_json(saved.to_json())
        if legacy:
            imported.guardrail_results.clear()
        async with agent.session(state=SessionState.from_json(imported.to_json())) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("accepted")
            assert restored.restored_turn is not None
            completed = await restored.restored_turn.get_result()
            assert completed.final_output == "resumed"
            records = completed.guardrail_results
            earlier = next((r for r in records if r.kind == "input"), None)
            resumed = {
                "names": [r.name for r in records],
                "input_calls": input_calls,
                "earlier_result": earlier.result if earlier else None,
                "earlier_session": earlier.session_id if earlier else None,
                "earlier_turn": earlier.turn_id if earlier else None,
                "agents": all(r.agent is agent for r in records),
            }
        async with agent.session() as independent:
            unrelated = await (await independent.submit("unrelated")).get_result()
            assert unrelated.final_output == "independent"
            fresh = next(r for r in unrelated.guardrail_results if r.kind == "input")
            assert fresh.result.output_info == {"phase": "initial", "ordinal": 2}
            assert input_calls == 2
            assert all(r.session_id == independent.id for r in unrelated.guardrail_results)
            assert [r.name for r in unrelated.guardrail_results] == [
                "before-approval",
                "after-approval",
            ]
        assert resumed == {
            "names": ["after-approval"] if legacy else ["before-approval", "after-approval"],
            "input_calls": 1,
            "earlier_result": None if legacy else prior[0].result,
            "earlier_session": None if legacy else prior[0].session_id,
            "earlier_turn": None if legacy else prior[0].turn_id,
            "agents": True,
        }
        assert completed.guardrail_results == records
        assert len(responses.requests) == 3

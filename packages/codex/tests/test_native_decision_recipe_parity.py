"""Decisions support the original guardrail and routing recipes in native turns."""

from dataclasses import replace

import pytest

from agents import Agent, GuardrailResult, GuardrailTripwire, InputGuardrail
from agents.usage import TokenUsage
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_decision_inherited_callbacks import Provider
from tests.test_native_decision_handoff import Decisions, graph, provider


@pytest.mark.parametrize("allowed", [True, False])
async def test_blocking_decision_guardrail_keeps_agent_and_turn_ownership(responses, allowed):
    class Policy(Provider):
        async def get_decision(self, request):
            return replace(await super().get_decision(request), value=allowed)

    policy = Policy()
    captured = []

    async def check(turn, context, source, value):
        assert source is agent
        assert context == "application context"
        assert value == "Scope request"
        assert responses.requests == []
        captured.append(turn)
        verdict = await turn.decide("Is this request in scope?", input=value)
        return GuardrailResult(not verdict, {"allowed": verdict})

    if allowed:
        responses.respond(assistant_output("Accepted."))
    async with timeout(20), provider(responses) as backend:
        agent = Agent(
            instructions="Answer in-scope requests.",
            provider=backend,
            decisions=policy,
            input_guardrails=[InputGuardrail("scope", check, run_in_parallel=False)],
        )
        async with agent.session(context="application context") as session:
            turn = await session.submit("Scope request")
            if allowed:
                result = await turn.get_result()
                assert result.final_output == "Accepted."
                (record,) = result.guardrail_results
            else:
                with pytest.raises(GuardrailTripwire) as blocked:
                    await turn.get_result()
                record = blocked.value.record
            assert record.agent is agent
            assert record.turn_id == turn.turn_id
            assert record.result.output_info == {"allowed": allowed}
            assert captured == [turn]
            with pytest.raises(RuntimeError, match="exact live callback"):
                await turn.decide("Expired?", input="Scope request")
            assert turn.run_usage.requests == (1 if allowed else 0)
            assert turn.run_usage.total_tokens == (14 if allowed else 0)
    assert [request.input for request in policy.requests] == ["Scope request"]
    assert len(responses.requests) == int(allowed)


async def test_decision_selected_transfer_uses_run_model_without_counting_decision_as_generation(
    responses,
):
    class BillableRoute(Decisions):
        async def get_decision(self, request):
            reply = await super().get_decision(request)
            usage = TokenUsage(7, 0, 0, 3, 0, 10)
            return replace(reply, usage=usage)

    decisions = BillableRoute()
    responses.respond(function_call("delegate", {"input": "Route this."}, "decision_route"))
    responses.respond(assistant_output("Routed."))
    async with timeout(20), provider(responses) as backend:
        source, billing = graph(backend, decisions)
        async with source.session() as session:
            turn = await session.submit("Route this.", model_override="gpt-6.1-sol")
            assert (await turn.get_result()).final_output == "Routed."
            assert await session.get_active_agent() is billing
            assert turn.run_usage.requests == 2
            assert turn.run_usage.total_tokens == 28
    assert [request["model"] for request in responses.requests] == [
        "gpt-6.1-sol",
        "gpt-6.1-sol",
    ]
    assert [request.input for request in decisions.requests] == ["Route this."]

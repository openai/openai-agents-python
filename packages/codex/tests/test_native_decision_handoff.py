"""Decisions has been selecting a captured target after the main model delegates."""

import asyncio

import pytest

from agents import Agent, DecisionHandoff, NativeProvider, OutputValidationError, TurnStoppedError
from agents.decisions import DecisionResponse
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


class Decisions:
    def __init__(self, value="billing", *, hold=False):
        self.value = value
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = hold
        self.cancelled = False

    async def get_decision(self, request):
        self.requests.append(request)
        self.entered.set()
        if self.hold:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return DecisionResponse(value=self.value)


def graph(provider, decisions):
    billing = Agent(name="billing", instructions="Billing specialist.", model="gpt-5.5")
    technical = Agent(name="technical", instructions="Technical specialist.", model="gpt-5.5")
    route = DecisionHandoff(
        name="delegate",
        description="Transfer a specialist request.",
        question="Choose the specialist.",
        targets={"billing": billing, "technical": technical},
    )
    source = Agent(
        name="main",
        instructions="Orchestrate.",
        model="gpt-5.5",
        provider=provider,
        decisions=decisions,
        handoffs=[route],
    )
    return source, billing


def provider(responses):
    return NativeProvider(base_url=responses.url, api_key="synthetic", supports_websockets=False)


async def test_main_model_invokes_decisions_and_native_transfers_without_a_routing_round_trip(
    responses,
):
    decisions = Decisions()
    responses.respond(function_call("delegate", {"input": "Billing question only."}, "route-call"))
    responses.respond(assistant_output("Billing answer."))
    async with timeout(30), provider(responses) as backend:
        source, billing = graph(backend, decisions)
        async with source.session() as session:
            turn = await session.submit("Private original transcript.")
            assert (await turn.get_result()).final_output == "Billing answer."
            assert await session.get_active_agent() is billing
            assert len(responses.requests) == 2
            assert [request["model"] for request in responses.requests] == [
                "gpt-5.5",
                "gpt-5.5",
            ]
            assert len(decisions.requests) == 1
            assert decisions.requests[0].input == "Billing question only."
            assert decisions.requests[0].question == "Choose the specialist."
            assert decisions.requests[0].output_schema["enum"] == ["billing", "technical"]
            assert "handoff_target_profile_id" not in str([item async for item in session.items()])


async def test_invalid_decision_stops_without_transferring(responses):
    decisions = Decisions("outside")
    responses.respond(function_call("delegate", {"input": "Route."}, "route-invalid"))
    async with timeout(30), provider(responses) as backend:
        source, _ = graph(backend, decisions)
        async with source.session() as session:
            turn = await session.submit("Route.")
            with pytest.raises(OutputValidationError):
                await turn.get_result()
            assert await session.get_active_agent() is source
            assert len(responses.requests) == 1


async def test_cancelled_selection_joins_decisions_and_fences_transfer(responses):
    decisions = Decisions(hold=True)
    responses.respond(function_call("delegate", {"input": "Route."}, "route-cancel"))
    async with timeout(30), provider(responses) as backend:
        source, _ = graph(backend, decisions)
        async with source.session() as session:
            turn = await session.submit("Route.")
            await decisions.entered.wait()
            await turn.cancel()
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            assert decisions.cancelled
            assert await session.get_active_agent() is source
            assert len(responses.requests) == 1


async def test_unoffered_decision_tool_never_invokes_decisions(responses):
    decisions = Decisions()
    responses.respond(function_call("delegate", {"input": "Route."}, "route-unoffered"))
    responses.respond(assistant_output("Main answer."))
    async with timeout(30), provider(responses) as backend:
        source, _ = graph(backend, decisions)
        async with source.session() as session:
            turn = await session.submit("Route.", before_step=lambda _, step_index: [])
            assert (await turn.get_result()).final_output == "Main answer."
            assert not decisions.requests
            assert await session.get_active_agent() is source


async def test_completed_handoff_checkpoint_uses_fresh_captured_targets(responses):
    from agents import SessionState

    responses.respond(function_call("delegate", {"input": "Billing."}, "route-checkpoint"))
    responses.respond(assistant_output("Billing answer."))
    responses.respond(assistant_output("Billing continues."))
    async with timeout(30), provider(responses) as backend:
        source, billing = graph(backend, Decisions())
        async with source.session() as session:
            await (await session.submit("Route.")).get_result()
            assert await session.get_active_agent() is billing
            state = SessionState.from_json((await session.save_state()).to_json())
        fresh_source, fresh_billing = graph(backend, Decisions())
        async with fresh_source.session(state=state) as restored:
            assert await restored.get_active_agent() is fresh_billing
            turn = await restored.submit("Continue.")
            assert (await turn.get_result()).final_output == "Billing continues."
            assert responses.requests[-1]["model"] == "gpt-5.5"

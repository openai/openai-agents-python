"""Existing preparation and collaboration callbacks inherit typed Decisions."""

from __future__ import annotations

import sys

import pytest

from agents import (
    Agent,
    DecisionResponse,
    Environment,
    NativeAgents,
    NativeProvider,
    RunBudget,
    StopTurn,
    TurnStoppedError,
)
from agents.usage import TokenUsage
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_deferred_jobs import Journal, Provider as JobProvider, declared, done
from tests.test_deferred_opening_outcome import pending
from tests.test_native_child_agents import collaboration, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import next_request


class Provider:
    def __init__(self):
        self.requests = []

    async def get_decision(self, request):
        self.requests.append(request)
        return DecisionResponse(
            value=True,
            usage=TokenUsage(7, 0, 0, 3, 0, 10),
            request_id="req-inherited-callback",
            model="custom-decision-provider",
        )


async def test_prepared_request_turn_decide_retains_usage_and_expires(responses: ResponsesFixture):
    decisions = Provider()
    captured = []

    async def prepare(turn, request, *, preparation_index, recovery_attempt):
        captured.append(turn)
        assert await turn.decide("Is this request allowed?", input="Synthetic evidence") is True
        return None

    responses.respond(assistant_output("prepared complete"))
    agent = Agent(
        model="gpt-5.5",
        instructions="Answer.",
        decisions=decisions,
        provider=NativeProvider(
            base_url=responses.url,
            api_key="synthetic-native-agent-key",
            supports_websockets=False,
        ),
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Answer.", prepare_request=prepare)
        result = await turn.get_result()
        assert result.final_output == "prepared complete"
        assert captured == [turn]
        assert result.thread_usage.total_token_usage.total_tokens == 14
        assert result.thread_usage.last_token_usage.total_tokens == 14
        receipts = [item async for item in session.turns()]
        assert sorted(item["usage"]["total_tokens"] for item in receipts[0]["responses"]) == [
            14,
        ]
        with pytest.raises(RuntimeError, match="exact live callback"):
            await captured[0].decide("Expired?", input="Synthetic evidence")
    assert len(decisions.requests) == 1
    assert len(responses.requests) == 1


async def test_native_agents_approval_decide_retains_exact_usage_and_expires(
    responses: ResponsesFixture,
    tmp_path,
    monkeypatch,
):
    decisions = Provider()
    captured = []

    async def approval(ctx, call):
        assert call.namespace == "collaboration" and call.name == "list_agents"
        captured.append(ctx)
        return await ctx.decide("May the coordinator list its children?", input="Synthetic policy")

    responses.respond(collaboration("list_agents", {}, "list-children"))
    responses.respond(assistant_output("approval complete"))
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="List children, then finish.",
            model="gpt-5.5",
            provider=NativeProvider(
                base_url=responses.url,
                api_key="synthetic-native-agent-key",
                supports_websockets=False,
            ),
            decisions=decisions,
            tools=[NativeAgents(approval=approval)],
        )
        async with agent.session(environment=declaration(executor)) as session:
            turn = await session.submit("List children.")
            result = await turn.get_result()
            assert result.final_output == "approval complete"
            assert len(captured) == 1 and captured[0].submission is turn
            assert result.thread_usage.total_token_usage.total_tokens == 28
            assert result.thread_usage.last_token_usage.total_tokens == 14
            receipts = [item async for item in session.turns()]
            assert sorted(item["usage"]["total_tokens"] for item in receipts[0]["responses"]) == [
                14,
                14,
            ]
            with pytest.raises(RuntimeError, match="exact live callback"):
                await captured[0].decide("Expired?", input="Synthetic policy")
    assert len(decisions.requests) == 1
    assert len(responses.requests) == 2


async def test_before_step_explicit_stop_preserves_policy_outcome_without_cost_estimate(
    responses: ResponsesFixture,
):
    estimates = []
    decisions = Provider()

    def estimate(usage):
        estimates.append(usage)
        return usage.total_tokens

    async def stop(turn, *, step_index):
        assert await turn.decide("Stop?", input="Synthetic evidence") is True
        return StopTurn("The decision requires stopping.")

    agent = Agent(
        model="gpt-5.5",
        instructions="Answer.",
        decisions=decisions,
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-native-agent-key", supports_websockets=False
        ),
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit(
            "Answer.",
            before_step=stop,
            budget=RunBudget(max_estimated_cost_micros=10, estimate_cost_micros=estimate),
        )
        with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
            await turn.get_result()
    assert estimates == []
    assert len(decisions.requests) == 1
    assert responses.requests == []


async def test_deferred_start_reuses_captured_agent_decisions(responses: ResponsesFixture):
    original, replacement = Provider(), Provider()
    jobs, tool = declared(Journal(), JobProvider([done(), done()]))

    async def complete(ctx):
        assert await ctx.decide("Is this complete?", input=ctx.text or "") is True

    agent = Agent(
        model="gpt-5.5",
        instructions="Render.",
        tools=[tool],
        decisions=original,
        before_complete=complete,
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ),
    )
    async with timeout(20), agent.session() as live:
        object.__setattr__(agent, "decisions", replacement)
        request = await pending(live, tool, responses)
        responses.respond(assistant_output("restored complete"))
        async with jobs.start(live, request) as (restored, turn):
            assert (await turn.get_result()).final_output == "restored complete"
            responses.respond(function_call(tool.name, {"pages": 3}, "second-job"))
            await restored.submit("Render another report.")
            second_request = await next_request(restored.interactions(), tool)
            responses.respond(assistant_output("second restored complete"))
            async with jobs.start(restored, second_request) as (_, second_turn):
                assert (await second_turn.get_result()).final_output == "second restored complete"
    assert len(original.requests) == 2 and not replacement.requests


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="requires native Unix environment"
)
async def test_read_only_environment_keeps_bound_decision_and_host_authority(
    responses: ResponsesFixture, tmp_path, monkeypatch
):
    decisions = Provider()
    host_marker = tmp_path / "host-authority"

    async def complete(ctx):
        assert await ctx.decide("Is this complete?", input=ctx.text or "") is True
        host_marker.write_text("approved")

    responses.respond(assistant_output("read-only complete"))
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        environment = Environment(
            url=executor.url,
            cwd=str(executor.root),
            headers={"Authorization": "Bearer " + executor.token},
            tools=["exec_command"],
            read_only=True,
        )
        agent = Agent(
            model="gpt-5.5",
            instructions="Answer.",
            decisions=decisions,
            before_complete=complete,
            provider=NativeProvider(
                base_url=responses.url,
                api_key="synthetic-native-agent-key",
                supports_websockets=False,
            ),
        )
        async with agent.session(environment=environment) as session:
            result = await (await session.submit("Answer.")).get_result()
            assert result.final_output == "read-only complete"
            assert result.thread_usage.total_token_usage.total_tokens == 14
            assert result.thread_usage.last_token_usage.total_tokens == 14
            assert (await session.save_state()).executor_read_only is True
    assert host_marker.read_text() == "approved"
    assert len(decisions.requests) == 1 and len(responses.requests) == 1

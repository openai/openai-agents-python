"""Existing preparation and collaboration callbacks inherit typed Decisions."""

from __future__ import annotations

import pytest

from agents import Agent, DecisionResponse, NativeAgents, NativeProvider
from agents.usage import TokenUsage
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_child_agents import collaboration, trust
from tests.test_native_executor_tools import declaration


class Provider:
    def __init__(self):
        self.requests = []

    async def get_decision(self, request):
        self.requests.append(request)
        return DecisionResponse(
            value={"response": True},
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
        assert result.thread_usage.total_token_usage.total_tokens == 24
        assert result.thread_usage.last_token_usage.total_tokens == 14
        receipts = [item async for item in session.turns()]
        assert sorted(item["usage"]["total_tokens"] for item in receipts[0]["responses"]) == [
            10,
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
            assert result.thread_usage.total_token_usage.total_tokens == 38
            assert result.thread_usage.last_token_usage.total_tokens == 14
            receipts = [item async for item in session.turns()]
            assert sorted(item["usage"]["total_tokens"] for item in receipts[0]["responses"]) == [
                10,
                14,
                14,
            ]
            with pytest.raises(RuntimeError, match="exact live callback"):
                await captured[0].decide("Expired?", input="Synthetic policy")
    assert len(decisions.requests) == 1
    assert len(responses.requests) == 2

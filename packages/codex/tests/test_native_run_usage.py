"""Measured run accounting through public native callback admission."""

from __future__ import annotations

import asyncio
import os

import pytest

from agents import Agent, ToolContext, function_tool
from agents.usage import TokenUsage

from .native_fixtures import assistant_output, function_call
from .test_native_agent import provider, responses as responses

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("OPENAI_AGENTS_NATIVE_TESTS") != "1", reason="native runtime opt-in"
    ),
]


async def test_run_usage_captures_reports_and_ignores_result_json(responses):
    retained = []
    snapshots = []

    @function_tool
    def metered(context: ToolContext[None]) -> dict:
        snapshots.append(context.submission.run_usage)
        context.report_usage(TokenUsage(3, 1, 0, 2, 0, 5))
        retained.append(context)
        # Model-visible usage-shaped JSON is unrelated to the report capability.
        return {"usage": {"input_tokens": 9000, "output_tokens": 9000}}

    responses.replies.extend([[function_call("metered", {}, "meter")], [assistant_output("done")]])
    async with Agent(
        instructions="Use metered.", provider=provider(responses), tools=[metered]
    ).session() as session:
        turn = await session.submit("Run")
        result = await turn.get_result()
        assert result.run_usage.requests == 3
        assert result.run_usage.input_tokens == 25
        assert result.run_usage.output_tokens == 8
        assert result.run_usage.total_tokens == 33
        assert result.run_usage.missing_usage_requests == 0
        assert turn.run_usage == result.run_usage
        assert snapshots[0].requests == 1
        assert snapshots[0].input_tokens == 11
        with pytest.raises(RuntimeError, match="no longer active"):
            retained[0].report_usage(None)
        assert turn.run_usage == result.run_usage
        assert (await turn.get_result()).run_usage == result.run_usage
    assert result.run_usage.requests == 3


async def test_run_usage_missing_and_invalid_reports_are_atomic(responses):
    snapshots = []

    @function_tool
    def metered(context: ToolContext[None]) -> str:
        before = context.submission.run_usage
        for invalid in (TokenUsage(-1, 0, 0, 0, 0, 0), TokenUsage(True, 0, 0, 0, 0, 0)):
            with pytest.raises(ValueError, match="nonnegative"):
                context.report_usage(invalid)
        with pytest.raises(ValueError, match="requests"):
            context.report_usage(None, requests=True)
        assert context.submission.run_usage == before
        context.report_usage(None, requests=3)
        snapshots.append(before)
        return "done"

    responses.replies.extend([[function_call("metered", {}, "meter")], [assistant_output("done")]])
    async with Agent(
        instructions="Use metered.", provider=provider(responses), tools=[metered]
    ).session() as session:
        result = await (await session.submit("Run")).get_result()
        assert result.run_usage.requests == 5
        assert result.run_usage.missing_usage_requests == 3
        assert result.run_usage.total_tokens == 28
        assert snapshots[0].requests == 1


async def test_cancelled_reporter_rejects_post_cancellation_usage(responses):
    entered, release = asyncio.Event(), asyncio.Event()
    retained = []

    @function_tool
    async def metered(context: ToolContext[None]) -> str:
        context.report_usage(TokenUsage(3, 0, 0, 2, 0, 5))
        retained.append(context)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                context.report_usage(None)
            raise
        return "done"

    responses.replies.append([function_call("metered", {}, "meter")])
    async with Agent(
        instructions="Use metered.", provider=provider(responses), tools=[metered]
    ).session() as session:
        turn = await session.submit("Run")
        await asyncio.wait_for(entered.wait(), 2)
        await turn.cancel()
        assert turn.run_usage.requests == 2
        assert turn.run_usage.total_tokens == 19
        with pytest.raises(RuntimeError, match="no longer active"):
            retained[0].report_usage(None)
        assert turn.run_usage.requests == 2


async def test_native_fast_run_usage_uses_only_archived_completions(responses):
    responses.replies.append([assistant_output("done")])
    result = await Agent(instructions="Answer.", provider=provider(responses)).run("Run")
    assert result.run_usage.requests == 1
    assert result.run_usage.total_tokens == 14
    assert result.run_usage.input_tokens == 11
    assert result.turn.run_usage == result.run_usage
    assert (await result.turn.get_result()).run_usage == result.run_usage


async def test_manual_checkpoint_usage_and_enablement_continue_once(responses):
    from agents import FunctionTool, SessionState

    from .test_native_agent import function_declarations
    from .test_native_manual_tools import LookupArgs, next_request

    enabled = True
    effects = []
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)

    @function_tool(is_enabled=lambda context, step: enabled)
    def metered(context: ToolContext[None]) -> str:
        context.report_usage(TokenUsage(3, 0, 0, 2, 0, 5))
        effects.append("reported")
        return "done"

    agent = Agent(instructions="Read both.", tools=[lookup, metered], provider=provider(responses))
    responses.respond(
        function_call("lookup", {"key": "A"}, "manual"), function_call("metered", {}, "meter")
    )
    async with agent.session() as source:
        turn = await source.submit("Read")
        await next_request(source.interactions(), lookup)
        saved = SessionState.from_json((await source.suspend()).to_json())
        assert saved.run_usage.requests == 2
        assert saved.run_usage.total_tokens == 19
        assert turn.run_usage == saved.run_usage
    enabled = False
    responses.respond(assistant_output("continued"))
    async with agent.session(state=saved) as restored:
        request = await next_request(restored.interactions(), lookup)
        await request.respond("known")
        result = await restored.restored_turn.get_result()
        assert result.run_usage.requests == 3
        assert result.run_usage.total_tokens == 33
        assert effects == ["reported"]
        assert "metered" not in {
            tool["name"] for tool in function_declarations(responses.requests[-1])
        }
        idle = await restored.save_state()
        assert idle.run_usage is None
    responses.respond(assistant_output("fresh"))
    async with agent.session(state=idle) as fresh:
        result = await (await fresh.submit("Next")).get_result()
        assert result.run_usage.requests == 1
        assert result.run_usage.total_tokens == 14


async def test_run_usage_survives_validated_argument_replacement(responses):
    from agents import ReplaceArguments

    @function_tool(before=lambda context: ReplaceArguments({"amount": 2}))
    def metered(context: ToolContext[None], amount: int) -> str:
        assert context.original_arguments["amount"] == 1
        assert context.effective_arguments["amount"] == 2
        context.report_usage(TokenUsage(amount, 0, 0, 0, 0, amount))
        return "done"

    responses.respond(function_call("metered", {"amount": 1}, "replaced"))
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Use metered.", provider=provider(responses), tools=[metered]
    ).session() as session:
        result = await (await session.submit("Run")).get_result()
        assert result.run_usage.requests == 3
        assert result.run_usage.total_tokens == 30

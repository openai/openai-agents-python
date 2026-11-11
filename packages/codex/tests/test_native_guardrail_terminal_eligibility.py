"""Restored held terminal results remain guarded after sibling failure."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agents import (
    Agent,
    FunctionTool,
    GuardrailResult,
    GuardrailTripwire,
    OutputGuardrail,
    ToolOutput,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_manual_profiles import save_at_manual_boundary
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize("failed_sibling", [False, True])
@pytest.mark.parametrize("reject", [False, True])
async def test_restored_failed_batch_still_checks_held_terminal(responses, failed_sibling, reject):
    effects, checked = [], []

    @function_tool(finish_turn=True)
    def finish() -> str:
        effects.append("finish")
        return "held-secret-sentinel"

    @function_tool
    def sibling() -> ToolOutput:
        effects.append("sibling")
        return (
            ToolOutput.error("settled sibling failed")
            if failed_sibling
            else ToolOutput.text("settled sibling accepted")
        )

    manual = FunctionTool(
        name="lookup", description="Read.", input_type=LookupArgs, output_type=str
    )
    responses.respond(
        function_call("finish", {}, "held-call"),
        function_call("sibling", {}, "sibling-call"),
        function_call("lookup", {"key": "pending"}, "manual-call"),
    )
    responses.respond(assistant_output("ordinary after restore"))
    agent = Agent(
        instructions="Use declared tools.",
        tools=[finish, sibling, manual],
        provider=provider(responses),
    )
    async with timeout(20):
        async with agent.session() as original:
            await original.submit("begin")
            await next_request(original.interactions(), manual)
            saved = await save_at_manual_boundary(original)
            assert sorted(effects) == ["finish", "sibling"]
            assert "held-secret-sentinel" not in json.dumps(saved.history)

        def check(context, source):
            if context.tool_call is not None:
                checked.append(context.tool_call.call_id)
                assert context.final_output == "held-secret-sentinel"
                return GuardrailResult(reject)
            return GuardrailResult(False)

        restored_agent = replace(
            agent, output_guardrails=(OutputGuardrail("restored-output", check),)
        )
        async with restored_agent.session(
            state=SessionState.from_json(saved.to_json())
        ) as restored:
            pending = await next_request(restored.interactions(), manual)
            await pending.respond("manual accepted")
            assert restored.restored_turn is not None
            if reject:
                with pytest.raises(GuardrailTripwire):
                    await restored.restored_turn.get_result()
            else:
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "ordinary after restore"
            assert checked == ["held-call"]
            assert sorted(effects) == ["finish", "sibling"]
            rows = [item async for item in restored.items()]
            assert ("held-secret-sentinel" in json.dumps(rows)) is not reject


async def test_failed_terminal_without_held_body_continues_normally(responses):
    effects, checked = [], []

    @function_tool(finish_turn=True)
    def finish() -> ToolOutput:
        effects.append("failed finish")
        return ToolOutput.error("ordinary terminal tool failure")

    def check(context, source):
        checked.append(context.tool_call)
        assert context.tool_call is None
        return GuardrailResult(False)

    responses.respond(function_call("finish", {}, "failed-terminal"))
    responses.respond(assistant_output("ordinary after failure"))
    async with Agent(
        instructions="Use finish.",
        tools=[finish],
        provider=provider(responses),
        output_guardrails=[OutputGuardrail("ordinary-only", check)],
    ).session() as session:
        result = await (await session.submit("begin")).get_result()
        assert result.final_output == "ordinary after failure"
        assert effects == ["failed finish"]
        assert checked == [None]
        assert len(responses.requests) == 2


@pytest.mark.parametrize("reject", [False, True])
async def test_live_failed_batch_validates_only_held_candidate(responses, reject):
    effects, checked, completed = [], [], []

    @function_tool(finish_turn=True)
    def finish() -> str:
        effects.append("finish")
        return "live-held-secret"

    @function_tool
    def sibling() -> ToolOutput:
        effects.append("sibling")
        return ToolOutput.error("ordinary sibling failure")

    def check(context, source):
        if context.tool_call is not None:
            checked.append(context.tool_call.call_id)
            assert context.final_output == "live-held-secret"
            return GuardrailResult(reject)
        return GuardrailResult(False)

    responses.respond(
        function_call("finish", {}, "live-held"), function_call("sibling", {}, "live-failed")
    )
    responses.respond(assistant_output("ordinary live continuation"))
    async with Agent(
        instructions="Use tools.",
        tools=[finish, sibling],
        provider=provider(responses),
        before_complete=lambda context: completed.append(context.text),
        output_guardrails=[OutputGuardrail("live-check", check)],
    ).session() as session:
        turn = await session.submit("begin")
        if reject:
            with pytest.raises(GuardrailTripwire):
                await turn.get_result()
        else:
            assert (await turn.get_result()).final_output == "ordinary live continuation"
        assert checked == ["live-held"]
        assert completed == ([] if reject else ["ordinary live continuation"])
        assert sorted(effects) == ["finish", "sibling"]
        assert (
            "live-held-secret" in json.dumps([item async for item in session.items()])
        ) is not reject

"""Settled correction history across supported native capture boundaries."""

import json

import pytest

from agents import (
    Agent,
    CapabilityError,
    FunctionTool,
    LocalStorage,
    NativeProvider,
    PromptedOutput,
    SessionState,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_prompted_repair import Report


@pytest.mark.parametrize("boundary", ["save", "fork", "compact"])
async def test_settled_repair_keeps_receipts_without_replaying_at_capture(responses, boundary):
    effects = []

    @function_tool
    def record() -> str:
        effects.append("once")
        return "synthetic-receipt"

    agent = Agent(
        instructions="Read",
        tools=[record],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    responses.respond(function_call("record", {}, "effect"))
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(assistant_output('{"label":"fixed"}'))
    responses.respond(assistant_output("fresh-root"))
    async with timeout(36):
        async with agent.session() as source:
            root = await source.submit("record", output_type=PromptedOutput(Report, 1))
            result = await root.get_result()
            assert result.final_output == Report(label="fixed")
            assert len(result.repair_attempts) == 1
            attempt = result.repair_attempts[0]
            assert attempt["usage"]["total_tokens"] == 14
            items = [item async for item in source.items() if item["turn_id"] == attempt["id"]]
            assert items and "fixed" in json.dumps(items)
            assert effects == ["once"] and len(responses.requests) == 3
            assert (await source.checkpoint_status(kind="idle")).status == "ready"
            if boundary == "compact":
                await source.compact(summary="One action recorded.")
                state = await source.save_state()
            elif boundary == "fork":
                state = await source.fork()
                assert state.source_session_id != source.id
            else:
                state = await source.save_state()
            # Round trip through application storage. No credentials or live grant
            # can come from passive SDK receipts on restoration.
            serialized = state.to_json()
            assert "synthetic-receipt" in serialized
            assert "Bearer synthetic" not in serialized
        async with agent.session(state=SessionState.from_json(serialized)) as restored:
            assert restored.restored_turn is None
            turns = [row async for row in restored.turns()]
            repairs = [row for row in turns if "parent_turn_id" in row]
            assert repairs == [{**attempt, "parent_session_id": restored.id}]
            restored_items = [
                item async for item in restored.items() if item["turn_id"] == attempt["id"]
            ]
            assert restored_items == items
            assert effects == ["once"] and len(responses.requests) == 3
            # Capture again must not duplicate receipts, and caller mutation is detached.
            repairs[0].clear()
            second = await restored.save_state()
            assert len([row async for row in restored.turns() if "parent_turn_id" in row]) == 1
            later = await restored.submit("continue")
            assert (await later.get_result()).final_output == "fresh-root"
            assert effects == ["once"] and len(responses.requests) == 4
            assert "fixed" not in json.dumps(responses.requests[-1]["input"])
        async with agent.session(state=SessionState.from_json(second.to_json())) as again:
            assert len([row async for row in again.turns() if "parent_turn_id" in row]) == 1
            assert effects == ["once"] and len(responses.requests) == 4


async def test_later_manual_suspend_restore_after_settled_repair(responses):
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(
        instructions="Read",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(assistant_output('{"label":"fixed"}'))
    responses.respond(function_call("lookup", {"key": "B"}, "manual-B"))
    async with timeout(36):
        async with agent.session() as source:
            first = await source.submit("first", output_type=PromptedOutput(Report, 1))
            result = await first.get_result()
            later = await source.submit("read B")
            stale = await next_request(source.interactions(), lookup)
            state = SessionState.from_json((await source.suspend()).to_json())
            assert len(responses.requests) == 3
        responses.respond(assistant_output("fresh reply"))
        async with agent.session(state=state) as restored:
            turn = restored.restored_turn
            assert turn is not None and turn.turn_id == later.turn_id
            repairs = [row async for row in restored.turns() if "parent_turn_id" in row]
            assert repairs == list(result.repair_attempts)
            fresh = await next_request(restored.interactions(), lookup)
            with pytest.raises((RuntimeError, ValueError)):
                await stale.respond("old")
            assert len(responses.requests) == 3
            await fresh.respond("current")
            assert (await turn.get_result()).final_output == "fresh reply"
            assert len(responses.requests) == 4


async def test_local_prompted_capability_truth_and_zero_retry_path(responses, tmp_path):
    agent = Agent(
        instructions="Read",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
        output_type=PromptedOutput(Report, 1),
    )
    async with timeout(25), agent.session(storage=LocalStorage(tmp_path / "profile")) as local:
        support = local.capabilities.get("prompted_output")
        assert support.status == "limited"
        with pytest.raises(CapabilityError):
            local.capabilities.require("prompted_output")
        local.capabilities.require("prompted_output", allow_limited=True)
        with pytest.raises(CapabilityError, match="LocalStorage"):
            await local.submit("invalid budget")
        assert not responses.requests
        responses.respond(assistant_output('{"label":"valid"}'))
        result = await (await local.submit("zero", output_type=PromptedOutput(Report))).get_result()
        assert result.final_output == Report(label="valid")
        assert len(responses.requests) == 1

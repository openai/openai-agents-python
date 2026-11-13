"""Restored tool work has been retaining fresh application authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest

from agents import (
    Agent,
    ContinueTurn,
    FinishToolBatch,
    FunctionTool,
    NativeProvider,
    SessionState,
    ToolBatchCompletionContext,
    ToolOutput,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import LookupArgs, next_request

MODEL = next(
    item["slug"]
    for item in json.loads(
        (Path(__file__).parent / "fixtures/request-control-models.json").read_text()
    )["models"]
    if item.get("supported_in_api") and item.get("tool_mode") != "code_mode_only"
)


@pytest.mark.parametrize("mixed,later_live", [(False, False), (True, False), (True, True)])
async def test_recovered_batch_requires_fresh_acceptance_for_each_result(
    responses: ResponsesFixture, mixed: bool, later_live: bool
) -> None:
    effects: list[str] = []
    proposals: list[list[tuple[str | None, bool]]] = []

    @function_tool
    def record() -> ToolOutput:
        effects.append("record")
        return ToolOutput.error("Synthetic failed automatic result")

    @function_tool
    def live() -> str:
        effects.append("live")
        return "Fresh live result"

    manual = FunctionTool(
        name="approve", description="Current approval", input_type=LookupArgs, output_type=str
    )

    def complete(context: ToolBatchCompletionContext[Any]) -> FinishToolBatch[str]:
        proposals.append(
            [(result.call.call_id, result.output.success) for result in context.results]
        )
        return FinishToolBatch("REDUCED")

    agent = Agent(
        instructions="Synthetic authority control",
        model=MODEL,
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-batch-authority"),
        tools=[record, manual, live] if mixed else [manual],
        tool_batch_completion=complete,
    )
    responses.respond(
        *([function_call("record", {}, "automatic")] if mixed else []),
        function_call("approve", {"key": "synthetic"}, "manual"),
    )
    responses.respond(
        function_call("live", {}, "later-live")
        if later_live
        else assistant_output("MODEL CONTINUED")
    )
    async with timeout(30), agent.session() as source:
        await source.submit("Synthetic input")
        await next_request(source.interactions(), manual)
        state = json.loads((await source.suspend()).to_json())
    if mixed:
        outcomes = state["native_continuation"]["tool_outcomes"]["outcomes"]
        outcomes[:] = [
            [call_id, True if call_id == "automatic" else succeeded]
            for call_id, succeeded in outcomes
        ]
    async with (
        timeout(30),
        agent.session(state=SessionState.from_json(json.dumps(state))) as resumed,
    ):
        request = await next_request(resumed.interactions(), manual)
        await request.respond("Fresh approved result")
        assert resumed.restored_turn is not None
        completed = await resumed.restored_turn.get_result()
    if mixed:
        assert proposals == ([[("later-live", True)]] if later_live else [])
        assert completed.final_output == ("REDUCED" if later_live else "MODEL CONTINUED")
        assert effects == (["record", "live"] if later_live else ["record"])
        assert len(responses.requests) == 2
    else:
        assert proposals == [[("manual", True)]]
        assert completed.final_output == "REDUCED"
        assert effects == []
        assert len(responses.requests) == 1


async def test_consumed_recovery_receipts_do_not_block_a_later_profile_batch(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []
    root_proposals: list[list[str | None]] = []
    destination_proposals: list[list[str | None]] = []

    @function_tool
    def live() -> str:
        effects.append("live")
        return "Fresh destination result"

    def root_complete(context: ToolBatchCompletionContext[Any]) -> ContinueTurn:
        root_proposals.append([result.call.call_id for result in context.results])
        return ContinueTurn("Continue with the declared destination.")

    def destination_complete(context: ToolBatchCompletionContext[Any]) -> FinishToolBatch[str]:
        destination_proposals.append([result.call.call_id for result in context.results])
        return FinishToolBatch("DESTINATION REDUCED")

    manual = FunctionTool(
        name="approve", description="Current approval", input_type=LookupArgs, output_type=str
    )
    destination = Agent(
        name="Destination",
        instructions="Use live.",
        model=MODEL,
        tools=[live],
        tool_batch_completion=destination_complete,
    )
    agent = Agent(
        name="Root",
        instructions="Approve then hand off.",
        model=MODEL,
        tools=[manual],
        handoffs=[destination],
        tool_batch_completion=root_complete,
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-handoff-authority"),
    )
    responses.respond(function_call("approve", {"key": "synthetic"}, "manual"))
    responses.respond(function_call("transfer_to_destination", {}, "handoff"))
    responses.respond(function_call("live", {}, "destination-live"))
    async with timeout(30), agent.session() as source:
        await source.submit("Synthetic input")
        await next_request(source.interactions(), manual)
        saved = (await source.suspend()).to_json()
    async with timeout(30), agent.session(state=SessionState.from_json(saved)) as resumed:
        request = await next_request(resumed.interactions(), manual)
        await request.respond("Fresh approved result")
        assert resumed.restored_turn is not None
        assert (await resumed.restored_turn.get_result()).final_output == "DESTINATION REDUCED"
    assert effects == ["live"]
    assert root_proposals == [["manual"]]
    assert destination_proposals == [["destination-live"]]
    assert len(responses.requests) == 3


@pytest.mark.parametrize("variant", ["default", "direct", "code", "historical_sibling"])
async def test_only_pending_direct_requests_require_a_current_direct_declaration(
    responses: ResponsesFixture, variant: str
) -> None:
    effects: list[str] = []

    def sibling(callers: tuple[Literal["direct", "code"], ...] | None) -> FunctionTool:
        @function_tool(name="record", allowed_callers=callers)
        def record() -> str:
            effects.append("record")
            return "Historical accepted output"

        return record

    def manual(callers: tuple[Literal["direct", "code"], ...] | None) -> FunctionTool:
        return FunctionTool(
            name="approve",
            description="Current declared operation",
            input_type=LookupArgs,
            output_type=str,
            allowed_callers=callers,
        )

    original = manual(("direct",))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-current-callers")
    agent = Agent(
        instructions="Synthetic route authority",
        model=MODEL,
        provider=provider,
        tools=[sibling(None), original] if variant == "historical_sibling" else [original],
    )
    responses.respond(
        *([function_call("record", {}, "completed")] if variant == "historical_sibling" else []),
        function_call("approve", {"key": "synthetic"}, "pending"),
    )
    responses.respond(assistant_output("MODEL CONTINUED"))
    async with timeout(30), agent.session() as source:
        await source.submit("Synthetic input")
        await next_request(source.interactions(), original)
        saved = (await source.suspend()).to_json()
    current = manual(
        ("code",) if variant == "code" else ("direct",) if variant == "direct" else None
    )
    current_agent = Agent(
        instructions="Synthetic route authority",
        model=MODEL,
        provider=provider,
        tools=[sibling(("code",)), current] if variant == "historical_sibling" else [current],
    )
    if variant == "code":
        with pytest.raises(
            RuntimeError, match="Pending manual tool no longer allows direct model calls"
        ):
            async with timeout(30), current_agent.session(state=SessionState.from_json(saved)):
                pytest.fail("A code-only declaration must not receive a restored direct grant")
        assert len(responses.requests) == 1
        return
    async with timeout(30), current_agent.session(state=SessionState.from_json(saved)) as resumed:
        request = await next_request(resumed.interactions(), current)
        await request.respond("Fresh current result")
        assert resumed.restored_turn is not None
        assert (await resumed.restored_turn.get_result()).final_output == "MODEL CONTINUED"
    assert len(responses.requests) == 2
    assert effects == (["record"] if variant == "historical_sibling" else [])


async def test_restore_keeps_a_buffered_completed_sibling_without_reissuing_it(
    responses: ResponsesFixture,
) -> None:
    first = FunctionTool(
        name="first",
        description="First request",
        input_type=LookupArgs,
        output_type=str,
        allowed_callers=("direct",),
    )
    second = FunctionTool(
        name="second",
        description="Second request",
        input_type=LookupArgs,
        output_type=str,
        allowed_callers=("direct",),
    )
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-buffered-authority")
    agent = Agent(
        instructions="Keep request order.", model=MODEL, provider=provider, tools=[first, second]
    )
    responses.respond(
        function_call("first", {"key": "first"}, "first-call"),
        function_call("second", {"key": "second"}, "second-call"),
    )
    responses.respond(assistant_output("MODEL CONTINUED"))
    async with timeout(30), agent.session() as source:
        await source.submit("Synthetic input")
        await next_request(source.interactions(), first)
        request = await next_request(source.interactions(), second)
        await request.respond("Already accepted second result")
        state = json.loads((await source.suspend()).to_json())
    assert "second-call" in state["native_continuation"]["accepted_replies"]
    current_second = FunctionTool(
        name="second",
        description="Second request",
        input_type=LookupArgs,
        output_type=str,
        allowed_callers=("code",),
    )
    current = Agent(
        instructions="Keep request order.",
        model=MODEL,
        provider=provider,
        tools=[first, current_second],
    )
    async with (
        timeout(30),
        current.session(state=SessionState.from_json(json.dumps(state))) as resumed,
    ):
        request = await next_request(resumed.interactions(), first)
        await request.respond("Fresh first result")
        assert resumed.restored_turn is not None
        assert (await resumed.restored_turn.get_result()).final_output == "MODEL CONTINUED"
    assert len(responses.requests) == 2

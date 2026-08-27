from __future__ import annotations

import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_output_item import Program
from pydantic import BaseModel

from agents import (
    Agent,
    ModelSettings,
    ProgrammaticToolCallingTool,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    ToolCallItem,
    ToolCallOutputItem,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    handoff,
)
from agents.decorators import tool, tool_input_guardrail, tool_output_guardrail
from agents.extensions.handoff_filters import remove_all_tools
from agents.handoffs import HandoffInputData

pytestmark = pytest.mark.hosted


class InventoryResult(BaseModel):
    units: int


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_programmatic_tool_calling_retains_program_owned_calls_and_output(
    integration_model: str, streaming: bool
) -> None:
    calls: list[str] = []

    @tool(allowed_callers=["programmatic"])
    def read_inventory(sku: str) -> InventoryResult:
        """Return the deterministic available units for an item."""
        calls.append(sku)
        return InventoryResult(units={"alpha": 7, "beta": 11}[sku])

    agent = Agent(
        name="Packaged programmatic tool agent",
        model=integration_model,
        instructions=(
            "Use Programmatic Tool Calling. Generate a JavaScript program that calls "
            "read_inventory('alpha') and read_inventory('beta') with Promise.all, adds the "
            "units fields from their returned objects, and returns the result. Then answer "
            "exactly TOTAL:18."
        ),
        model_settings=ModelSettings(tool_choice="programmatic_tool_calling", max_tokens=1024),
        tools=[read_inventory, ProgrammaticToolCallingTool()],
    )
    result: RunResult | RunResultStreaming
    if streaming:
        streamed = Runner.run_streamed(
            agent,
            "Calculate the total inventory.",
            run_config=RunConfig(tracing_disabled=True),
            max_turns=5,
        )
        async for _event in streamed.stream_events():
            pass
        result = streamed
    else:
        result = await Runner.run(
            agent,
            "Calculate the total inventory.",
            run_config=RunConfig(tracing_disabled=True),
            max_turns=5,
        )
    program_calls = [
        item.raw_item
        for item in result.new_items
        if isinstance(item, ToolCallItem)
        and isinstance(item.raw_item, ResponseFunctionToolCall)
        and item.raw_item.caller is not None
        and item.raw_item.caller.type == "program"
    ]

    assert sorted(calls) == ["alpha", "beta"]
    assert len(program_calls) == 2
    assert any(
        isinstance(item, ToolCallItem) and isinstance(item.raw_item, Program)
        for item in result.new_items
    )
    assert any(
        isinstance(item, ToolCallOutputItem)
        and getattr(item.raw_item, "type", None) == "program_output"
        for item in result.new_items
    )
    assert result.final_output == "TOTAL:18"


async def test_programmatic_tool_history_survives_a_filtered_handoff(
    integration_model: str,
) -> None:
    calls: list[str] = []
    handoff_filter_inputs: list[tuple[HandoffInputData, HandoffInputData]] = []

    def capture_filtered_handoff(input_data: HandoffInputData) -> HandoffInputData:
        filtered = remove_all_tools(input_data)
        handoff_filter_inputs.append((input_data, filtered))
        return filtered

    @tool(allowed_callers=["programmatic"])
    def inspect_inventory(sku: str) -> InventoryResult:
        """Return deterministic inventory details to the hosted program."""
        calls.append(sku)
        return InventoryResult(units=18)

    specialist = Agent(
        name="Packaged program summary specialist",
        model=integration_model,
        instructions="Reply with exactly FILTERED_PROGRAM_HANDOFF_OK.",
        model_settings={"max_tokens": 256},
    )
    coordinator = Agent(
        name="Packaged program handoff coordinator",
        model=integration_model,
        instructions=(
            "First use Programmatic Tool Calling to run inspect_inventory('alpha'). "
            "After the program returns, immediately transfer to the summary specialist."
        ),
        tools=[inspect_inventory, ProgrammaticToolCallingTool()],
        handoffs=[handoff(specialist, input_filter=capture_filtered_handoff)],
        model_settings={"max_tokens": 1024},
    )
    result = await Runner.run(
        coordinator,
        "Inspect alpha with a program, then transfer the answer.",
        run_config=RunConfig(tracing_disabled=True, nest_handoff_history=True),
        max_turns=7,
    )

    assert calls == ["alpha"]
    assert result.final_output == "FILTERED_PROGRAM_HANDOFF_OK"
    assert result.last_agent is specialist
    assert len(handoff_filter_inputs) == 1
    original_input, filtered_input = handoff_filter_inputs[0]
    assert any(
        isinstance(item, ToolCallItem | ToolCallOutputItem)
        for item in (*original_input.pre_handoff_items, *original_input.new_items)
    )
    assert not any(
        isinstance(item, ToolCallItem | ToolCallOutputItem)
        for item in (*filtered_input.pre_handoff_items, *filtered_input.new_items)
    )
    assert any(
        isinstance(output, Program)
        for response in result.raw_responses
        for output in response.output
    )


@pytest.mark.nightly
@pytest.mark.parametrize("approved", [False, True], ids=["rejected", "approved"])
async def test_programmatic_tool_approval_preserves_caller_across_serialized_resume(
    integration_model: str, approved: bool
) -> None:
    calls: list[str] = []

    @tool(allowed_callers=["programmatic"], needs_approval=True)
    def approve_inventory(sku: str) -> InventoryResult:
        """Read inventory only after the program's tool call is approved."""
        calls.append(sku)
        return InventoryResult(units=18)

    agent = Agent(
        name="Packaged programmatic approval agent",
        model=integration_model,
        instructions=(
            "Use Programmatic Tool Calling to invoke approve_inventory('alpha'). "
            "If it succeeds reply exactly PROGRAM_APPROVED; if it is rejected reply "
            "exactly PROGRAM_REJECTED."
        ),
        model_settings=ModelSettings(tool_choice="programmatic_tool_calling", max_tokens=1024),
        tools=[approve_inventory, ProgrammaticToolCallingTool()],
    )
    config = RunConfig(tracing_disabled=True)

    first = await Runner.run(agent, "Read the protected inventory.", run_config=config, max_turns=6)
    assert len(first.interruptions) == 1
    state = await RunState.from_json(agent, first.to_state().to_json())
    interruption = state.get_interruptions()[0]
    if approved:
        state.approve(interruption)
    else:
        state.reject(interruption, rejection_message="Inventory access was rejected.")

    resumed = await Runner.run(agent, state, run_config=config, max_turns=6)
    outputs = [item for item in resumed.new_items if isinstance(item, ToolCallOutputItem)]

    assert calls == (["alpha"] if approved else [])
    assert outputs
    if approved:
        assert resumed.final_output == "PROGRAM_APPROVED"
    else:
        rejected_item = next(
            item
            for item in outputs
            if isinstance(item.raw_item, dict)
            and item.raw_item.get("type") == "function_call_output"
        )
        assert rejected_item.output == "Inventory access was rejected."
        assert json.loads(cast(dict[str, Any], rejected_item.raw_item)["output"]) == {
            "error": "Inventory access was rejected."
        }
    callers = [
        cast(dict[str, Any], item.raw_item).get("caller")
        if isinstance(item.raw_item, dict)
        else getattr(item.raw_item, "caller", None)
        for item in outputs
    ]
    assert any(
        (caller.get("type") if isinstance(caller, dict) else getattr(caller, "type", None))
        == "program"
        for caller in callers
    )


@pytest.mark.nightly
@pytest.mark.parametrize("rejection_stage", ["input", "output"])
async def test_programmatic_structured_tool_guardrail_errors_are_valid_json(
    integration_model: str, rejection_stage: str
) -> None:
    calls: list[str] = []
    rejection_message = f"Inventory {rejection_stage} was rejected."

    @tool_input_guardrail
    def inspect_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        if rejection_stage == "input":
            return ToolGuardrailFunctionOutput.reject_content(rejection_message)
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def inspect_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        if rejection_stage == "output":
            return ToolGuardrailFunctionOutput.reject_content(rejection_message)
        return ToolGuardrailFunctionOutput.allow()

    @tool(
        allowed_callers=["programmatic"],
        tool_input_guardrails=[inspect_input],
        tool_output_guardrails=[inspect_output],
    )
    def inspect_inventory(sku: str) -> InventoryResult:
        """Read inventory after both programmatic tool guardrails allow the request."""
        calls.append(sku)
        return InventoryResult(units=18)

    agent = Agent(
        name="Packaged programmatic guardrail rejection agent",
        model=integration_model,
        instructions=(
            "Use Programmatic Tool Calling. Write a JavaScript program that calls "
            "inspect_inventory('alpha') and returns the resulting error field when present. "
            "After the program finishes, reply exactly PROGRAM_GUARDRAIL_REJECTED."
        ),
        model_settings=ModelSettings(tool_choice="programmatic_tool_calling", max_tokens=1024),
        tools=[inspect_inventory, ProgrammaticToolCallingTool()],
    )

    result = await Runner.run(
        agent,
        "Inspect the guarded inventory and report its rejection.",
        run_config=RunConfig(tracing_disabled=True),
        max_turns=6,
    )
    rejected_item = next(
        item
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "function_call_output"
    )

    assert calls == ([] if rejection_stage == "input" else ["alpha"])
    assert rejected_item.output == rejection_message
    assert json.loads(cast(dict[str, Any], rejected_item.raw_item)["output"]) == {
        "error": rejection_message
    }
    assert result.final_output == "PROGRAM_GUARDRAIL_REJECTED"

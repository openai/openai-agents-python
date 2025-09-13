from __future__ import annotations

import json
from typing import Any

import pytest

from agents import (
    Agent,
    FunctionTool,
    RunContextWrapper,
    Runner,
    ToolGuardrailFunctionOutput,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.tool import function_tool
from agents.items import ToolCallOutputItem

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_tool_input_guardrail_blocks_and_uses_message():
    executed = {"called": False}

    @function_tool(name_override="guarded_tool")
    def guarded_tool() -> str:
        executed["called"] = True
        return "real_result"

    @tool_input_guardrail
    def input_gr(data) -> ToolGuardrailFunctionOutput:
        # Always block with model message
        return ToolGuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
            model_message="blocked_by_input_guardrail",
        )

    # Attach to FunctionTool
    assert isinstance(guarded_tool, FunctionTool)
    guarded_tool.tool_input_guardrails = [input_gr]

    model = FakeModel()
    agent = Agent(name="test", model=model, tools=[guarded_tool])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("guarded_tool", json.dumps({}))],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="start")

    # Tool should not run
    assert executed["called"] is False
    # Tool output item should contain the guardrail model message
    tool_outputs = [it for it in result.new_items if isinstance(it, ToolCallOutputItem)]
    assert len(tool_outputs) == 1
    # raw_item.output is the string sent to the model
    assert tool_outputs[0].raw_item["output"] == "blocked_by_input_guardrail"
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_tool_output_guardrail_replaces_result():
    executed = {"called": False}

    @function_tool(name_override="guarded_tool_out")
    def guarded_tool_out() -> str:
        executed["called"] = True
        return "real_output"

    @tool_output_guardrail
    def output_gr(data) -> ToolGuardrailFunctionOutput:
        # Replace result
        return ToolGuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
            model_message="overridden_by_output_guardrail",
        )

    assert isinstance(guarded_tool_out, FunctionTool)
    guarded_tool_out.tool_output_guardrails = [output_gr]

    model = FakeModel()
    agent = Agent(name="test", model=model, tools=[guarded_tool_out])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("guarded_tool_out", json.dumps({}))],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="go")

    assert executed["called"] is True
    tool_outputs = [it for it in result.new_items if isinstance(it, ToolCallOutputItem)]
    assert len(tool_outputs) == 1
    assert tool_outputs[0].raw_item["output"] == "overridden_by_output_guardrail"
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_input_guardrail_takes_precedence_over_output_guardrail():
    executed = {"called": False}

    @function_tool(name_override="both_guarded")
    def both_guarded() -> str:
        executed["called"] = True
        return "should_not_matter"

    @tool_input_guardrail
    def input_gr(data) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
            model_message="input_wins",
        )

    @tool_output_guardrail
    def output_gr(data) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
            model_message="output_would_win_if_reached",
        )

    assert isinstance(both_guarded, FunctionTool)
    both_guarded.tool_input_guardrails = [input_gr]
    both_guarded.tool_output_guardrails = [output_gr]

    model = FakeModel()
    agent = Agent(name="test", model=model, tools=[both_guarded])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("both_guarded", json.dumps({}))],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="go")

    # Input guardrail should prevent tool from running
    assert executed["called"] is False
    tool_outputs = [it for it in result.new_items if isinstance(it, ToolCallOutputItem)]
    assert len(tool_outputs) == 1
    assert tool_outputs[0].raw_item["output"] == "input_wins"
    assert result.final_output == "done"


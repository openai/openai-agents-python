from __future__ import annotations

from typing import Any

from agents import (
    AgentHookContext,
    FunctionTool,
    HandoffInputData,
    ItemHelpers,
    MultiProvider,
    RunConfig,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    Usage,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.tool_context import ToolContext


def test_run_config_positional_arguments_remain_backward_compatible() -> None:
    async def keep_handoff_input(data: HandoffInputData) -> HandoffInputData:
        return data

    config = RunConfig(None, MultiProvider(), None, keep_handoff_input)

    assert config.handoff_input_filter is keep_handoff_input
    assert config.session_settings is None


def test_function_tool_positional_arguments_keep_guardrail_positions() -> None:
    async def invoke(_ctx: ToolContext[Any], _args: str) -> str:
        return "ok"

    @tool_input_guardrail
    def allow_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def allow_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow()

    input_guardrails = [allow_input]
    output_guardrails = [allow_output]

    tool = FunctionTool(
        "tool_name",
        "tool_description",
        {"type": "object", "properties": {}},
        invoke,
        True,
        True,
        input_guardrails,
        output_guardrails,
    )

    assert tool.needs_approval is False
    assert tool.tool_input_guardrails is not None
    assert tool.tool_output_guardrails is not None
    assert tool.tool_input_guardrails[0] is allow_input
    assert tool.tool_output_guardrails[0] is allow_output


def test_agent_hook_context_third_positional_argument_is_turn_input() -> None:
    turn_input = ItemHelpers.input_to_new_input_list("hello")
    context = AgentHookContext(None, Usage(), turn_input)

    assert context.turn_input == turn_input
    assert isinstance(context._approvals, dict)

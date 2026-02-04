from __future__ import annotations

from typing import Any

from agents import (
    Agent,
    AgentHookContext,
    FunctionTool,
    HandoffInputData,
    ItemHelpers,
    MultiProvider,
    RunConfig,
    RunContextWrapper,
    RunResult,
    RunResultStreaming,
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


def test_run_result_v070_positional_constructor_still_works() -> None:
    result = RunResult(
        "x",
        [],
        [],
        "ok",
        [],
        [],
        [],
        [],
        RunContextWrapper(context=None),
        Agent(name="agent"),
    )
    assert result.final_output == "ok"
    assert result.interruptions == []


def test_run_result_streaming_v070_positional_constructor_still_works() -> None:
    result = RunResultStreaming(
        "x",
        [],
        [],
        "ok",
        [],
        [],
        [],
        [],
        RunContextWrapper(context=None),
        Agent(name="agent"),
        0,
        1,
        None,
        None,
    )
    assert result.final_output == "ok"
    assert result.interruptions == []


def test_run_result_streaming_accepts_legacy_run_impl_task_keyword() -> None:
    result = RunResultStreaming(
        input="x",
        new_items=[],
        raw_responses=[],
        final_output="ok",
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=RunContextWrapper(context=None),
        current_agent=Agent(name="agent"),
        current_turn=0,
        max_turns=1,
        _current_agent_output_schema=None,
        trace=None,
        _run_impl_task=None,
    )
    assert result.run_loop_task is None

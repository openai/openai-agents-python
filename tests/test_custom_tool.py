from typing import Any, cast

import pytest
from openai.types.responses import ResponseCustomToolCall

from agents import Agent, CustomTool, RunConfig, RunContextWrapper
from agents.items import ToolApprovalItem, ToolCallOutputItem
from agents.lifecycle import RunHooks
from agents.run_internal.run_steps import ToolRunCustom
from agents.run_internal.tool_actions import CustomToolAction
from agents.tool import CustomToolOnApprovalFunctionResult
from agents.tool_context import ToolContext


@pytest.mark.asyncio
async def test_custom_tool_action_returns_custom_tool_call_output() -> None:
    async def invoke(ctx: ToolContext[Any], raw_input: str) -> str:
        assert ctx.tool_name == "raw_editor"
        assert ctx.tool_arguments == "hello"
        return raw_input.upper()

    tool = CustomTool(
        name="raw_editor",
        description="Edit raw text.",
        on_invoke_tool=invoke,
        format={"type": "text"},
    )
    agent = Agent(name="custom-agent", tools=[tool])
    tool_call = ResponseCustomToolCall(
        type="custom_tool_call",
        name="raw_editor",
        call_id="call_custom",
        input="hello",
    )

    result = await CustomToolAction.execute(
        agent=agent,
        call=ToolRunCustom(tool_call=tool_call, custom_tool=tool),
        hooks=RunHooks[Any](),
        context_wrapper=RunContextWrapper(context=None),
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item == {
        "type": "custom_tool_call_output",
        "call_id": "call_custom",
        "output": "HELLO",
    }


@pytest.mark.asyncio
async def test_custom_tool_on_approval_callback_auto_rejects_with_reason() -> None:
    async def invoke(_ctx: ToolContext[Any], _raw_input: str) -> str:
        raise AssertionError("rejected custom tool should not execute")

    async def on_approval(
        _context: RunContextWrapper[Any], _approval_item: ToolApprovalItem
    ) -> CustomToolOnApprovalFunctionResult:
        return {"approve": False, "reason": "Not allowed"}

    tool = CustomTool(
        name="raw_editor",
        description="Edit raw text.",
        on_invoke_tool=invoke,
        format={"type": "text"},
        needs_approval=True,
        on_approval=on_approval,
    )
    agent = Agent(name="custom-agent", tools=[tool])
    tool_call = ResponseCustomToolCall(
        type="custom_tool_call",
        name="raw_editor",
        call_id="call_custom",
        input="hello",
    )

    result = await CustomToolAction.execute(
        agent=agent,
        call=ToolRunCustom(tool_call=tool_call, custom_tool=tool),
        hooks=RunHooks[Any](),
        context_wrapper=RunContextWrapper(context=None),
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert result.output == "Not allowed"
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item == {
        "type": "custom_tool_call_output",
        "call_id": "call_custom",
        "output": "Not allowed",
    }

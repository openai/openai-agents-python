"""Tests for tool_origin preservation on rejected function tool calls."""

from __future__ import annotations

import sys

import pytest

from agents import Agent, function_tool
from agents.items import ToolCallOutputItem
from agents.tool import ToolOriginType

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message
from .utils.hitl import reject_tool_call

if sys.version_info >= (3, 10):
    from .mcp.helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_rejected_function_tool_preserves_tool_origin():
    """Test that rejected function tools preserve tool_origin."""
    model = FakeModel()

    @function_tool
    def test_tool(x: int) -> str:
        """Test tool."""
        return f"result: {x}"

    agent = Agent(name="test", model=model, tools=[test_tool])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("test_tool", '{"x": 42}')],
            [get_text_message("done")],
        ]
    )

    # Pre-reject the tool call
    tool_call = get_function_tool_call("test_tool", '{"x": 42}')
    from openai.types.responses import ResponseFunctionToolCall

    from agents.lifecycle import RunHooks
    from agents.run_config import RunConfig
    from agents.run_context import RunContextWrapper
    from agents.run_internal.run_steps import ToolRunFunction
    from agents.run_internal.tool_execution import execute_function_tool_calls

    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    assert isinstance(tool_call, ResponseFunctionToolCall)
    reject_tool_call(context, agent, tool_call, "test_tool")

    # Execute the tool call which should be rejected
    tool_run = ToolRunFunction(tool_call=tool_call, function_tool=test_tool)
    results, _, _ = await execute_function_tool_calls(
        agent=agent,
        tool_runs=[tool_run],
        hooks=RunHooks(),
        context_wrapper=context,
        config=RunConfig(),
    )

    # Should have a rejection result
    assert len(results) == 1
    result = results[0]
    assert result.run_item is not None
    assert isinstance(result.run_item, ToolCallOutputItem)

    # Verify tool_origin is preserved on rejection
    assert result.run_item.tool_origin is not None
    assert result.run_item.tool_origin.type == ToolOriginType.FUNCTION


@pytest.mark.asyncio
async def test_rejected_agent_as_tool_preserves_tool_origin():
    """Test that rejected agent-as-tool preserves tool_origin."""
    model = FakeModel()
    nested_model = FakeModel()

    nested_agent = Agent(
        name="nested_agent",
        model=nested_model,
        instructions="You are a nested agent.",
    )

    nested_model.add_multiple_turn_outputs([[get_text_message("nested response")]])

    tool = nested_agent.as_tool(
        tool_name="nested_tool",
        tool_description="A nested agent tool",
    )

    orchestrator = Agent(name="orchestrator", model=model, tools=[tool])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("nested_tool", '{"input": "test"}')],
            [get_text_message("done")],
        ]
    )

    # Pre-reject the tool call
    tool_call = get_function_tool_call("nested_tool", '{"input": "test"}')
    from openai.types.responses import ResponseFunctionToolCall

    from agents.lifecycle import RunHooks
    from agents.run_config import RunConfig
    from agents.run_context import RunContextWrapper
    from agents.run_internal.run_steps import ToolRunFunction
    from agents.run_internal.tool_execution import execute_function_tool_calls
    from agents.tool import FunctionTool

    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    assert isinstance(tool_call, ResponseFunctionToolCall)
    assert isinstance(tool, FunctionTool)
    reject_tool_call(context, orchestrator, tool_call, "nested_tool")

    # Execute the tool call which should be rejected
    tool_run = ToolRunFunction(tool_call=tool_call, function_tool=tool)
    results, _, _ = await execute_function_tool_calls(
        agent=orchestrator,
        tool_runs=[tool_run],
        hooks=RunHooks(),
        context_wrapper=context,
        config=RunConfig(),
    )

    # Should have a rejection result
    assert len(results) == 1
    result = results[0]
    assert result.run_item is not None
    assert isinstance(result.run_item, ToolCallOutputItem)

    # Verify tool_origin is preserved on rejection
    assert result.run_item.tool_origin is not None
    assert result.run_item.tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert result.run_item.tool_origin.agent_as_tool is not None
    assert result.run_item.tool_origin.agent_as_tool.name == "nested_agent"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.version_info < (3, 10), reason="MCP tests require Python 3.10+")
async def test_rejected_mcp_tool_preserves_tool_origin():
    """Test that rejected MCP tools preserve tool_origin."""
    model = FakeModel()
    server = FakeMCPServer(server_name="test_mcp_server")
    server.add_tool("mcp_tool", {})

    agent = Agent(name="test", model=model, mcp_servers=[server])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("mcp_tool", "")],
            [get_text_message("done")],
        ]
    )

    # Pre-reject the tool call
    tool_call = get_function_tool_call("mcp_tool", "")
    from openai.types.responses import ResponseFunctionToolCall

    from agents.lifecycle import RunHooks
    from agents.mcp import MCPUtil
    from agents.run_config import RunConfig
    from agents.run_context import RunContextWrapper
    from agents.run_internal.run_steps import ToolRunFunction
    from agents.run_internal.tool_execution import execute_function_tool_calls
    from agents.tool import FunctionTool

    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    assert isinstance(tool_call, ResponseFunctionToolCall)
    reject_tool_call(context, agent, tool_call, "mcp_tool")

    # Get the MCP tool as FunctionTool
    mcp_tools = await MCPUtil.get_all_function_tools(
        agent.mcp_servers,
        convert_schemas_to_strict=False,
        run_context=context,
        agent=agent,
    )
    mcp_tool = next(tool for tool in mcp_tools if tool.name == "mcp_tool")
    assert isinstance(mcp_tool, FunctionTool)

    # Execute the tool call which should be rejected
    tool_run = ToolRunFunction(tool_call=tool_call, function_tool=mcp_tool)
    results, _, _ = await execute_function_tool_calls(
        agent=agent,
        tool_runs=[tool_run],
        hooks=RunHooks(),
        context_wrapper=context,
        config=RunConfig(),
    )

    # Should have a rejection result
    assert len(results) == 1
    result = results[0]
    assert result.run_item is not None
    assert isinstance(result.run_item, ToolCallOutputItem)

    # Verify tool_origin is preserved on rejection
    assert result.run_item.tool_origin is not None
    assert result.run_item.tool_origin.type == ToolOriginType.MCP
    assert result.run_item.tool_origin.mcp_server is not None
    assert result.run_item.tool_origin.mcp_server.name == "test_mcp_server"

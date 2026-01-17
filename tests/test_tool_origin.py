"""Tests for tool origin tracking feature."""

from __future__ import annotations

import sys
from typing import cast

import pytest

from agents import Agent, FunctionTool, RunContextWrapper, Runner, function_tool
from agents.items import ToolCallItem, ToolCallItemTypes, ToolCallOutputItem
from agents.tool import ToolOrigin, ToolOriginType

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message

if sys.version_info >= (3, 10):
    from .mcp.helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_function_tool_origin():
    """Test that regular function tools have FUNCTION origin."""
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

    result = await Runner.run(agent, input="test")
    tool_call_items = [item for item in result.new_items if isinstance(item, ToolCallItem)]
    tool_output_items = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]

    assert len(tool_call_items) == 1
    assert tool_call_items[0].tool_origin is not None
    assert tool_call_items[0].tool_origin.type == ToolOriginType.FUNCTION
    assert tool_call_items[0].tool_origin.mcp_server is None
    assert tool_call_items[0].tool_origin.agent_as_tool is None

    assert len(tool_output_items) == 1
    assert tool_output_items[0].tool_origin is not None
    assert tool_output_items[0].tool_origin.type == ToolOriginType.FUNCTION
    assert tool_output_items[0].tool_origin.mcp_server is None
    assert tool_output_items[0].tool_origin.agent_as_tool is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.version_info < (3, 10), reason="MCP tests require Python 3.10+")
async def test_mcp_tool_origin():
    """Test that MCP tools have MCP origin with server name."""
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

    result = await Runner.run(agent, input="test")
    tool_call_items = [item for item in result.new_items if isinstance(item, ToolCallItem)]
    tool_output_items = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]

    assert len(tool_call_items) == 1
    assert tool_call_items[0].tool_origin is not None
    assert tool_call_items[0].tool_origin.type == ToolOriginType.MCP
    assert tool_call_items[0].tool_origin.mcp_server is not None
    assert tool_call_items[0].tool_origin.mcp_server.name == "test_mcp_server"
    assert tool_call_items[0].tool_origin.agent_as_tool is None

    assert len(tool_output_items) == 1
    assert tool_output_items[0].tool_origin is not None
    assert tool_output_items[0].tool_origin.type == ToolOriginType.MCP
    assert tool_output_items[0].tool_origin.mcp_server is not None
    assert tool_output_items[0].tool_origin.mcp_server.name == "test_mcp_server"
    assert tool_output_items[0].tool_origin.agent_as_tool is None


@pytest.mark.asyncio
async def test_agent_as_tool_origin():
    """Test that agent-as-tool has AGENT_AS_TOOL origin with agent name."""
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

    result = await Runner.run(orchestrator, input="test")
    tool_call_items = [item for item in result.new_items if isinstance(item, ToolCallItem)]
    tool_output_items = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]

    assert len(tool_call_items) == 1
    assert tool_call_items[0].tool_origin is not None
    assert tool_call_items[0].tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert tool_call_items[0].tool_origin.mcp_server is None
    assert tool_call_items[0].tool_origin.agent_as_tool is not None
    assert tool_call_items[0].tool_origin.agent_as_tool.name == "nested_agent"

    assert len(tool_output_items) == 1
    assert tool_output_items[0].tool_origin is not None
    assert tool_output_items[0].tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert tool_output_items[0].tool_origin.mcp_server is None
    assert tool_output_items[0].tool_origin.agent_as_tool is not None
    assert tool_output_items[0].tool_origin.agent_as_tool.name == "nested_agent"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.version_info < (3, 10), reason="MCP tests require Python 3.10+")
async def test_multiple_tool_origins():
    """Test that multiple tools from different origins work together."""
    model = FakeModel()
    nested_model = FakeModel()

    @function_tool
    def func_tool(x: int) -> str:
        """Function tool."""
        return f"function: {x}"

    mcp_server = FakeMCPServer(server_name="mcp_server")
    mcp_server.add_tool("mcp_tool", {})

    nested_agent = Agent(name="nested", model=nested_model, instructions="Nested agent")
    nested_model.add_multiple_turn_outputs([[get_text_message("nested response")]])
    agent_tool = nested_agent.as_tool(tool_name="agent_tool", tool_description="Agent tool")

    agent = Agent(
        name="test",
        model=model,
        tools=[func_tool, agent_tool],
        mcp_servers=[mcp_server],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("func_tool", '{"x": 1}'),
                get_function_tool_call("mcp_tool", ""),
                get_function_tool_call("agent_tool", '{"input": "test"}'),
            ],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="test")
    tool_call_items = [item for item in result.new_items if isinstance(item, ToolCallItem)]
    tool_output_items = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]

    assert len(tool_call_items) == 3
    assert len(tool_output_items) == 3

    # Find items by tool name
    function_item = next(
        item for item in tool_call_items if getattr(item.raw_item, "name", None) == "func_tool"
    )
    mcp_item = next(
        item for item in tool_call_items if getattr(item.raw_item, "name", None) == "mcp_tool"
    )
    agent_item = next(
        item for item in tool_call_items if getattr(item.raw_item, "name", None) == "agent_tool"
    )

    assert function_item.tool_origin is not None
    assert function_item.tool_origin.type == ToolOriginType.FUNCTION
    assert mcp_item.tool_origin is not None
    assert mcp_item.tool_origin.type == ToolOriginType.MCP
    assert mcp_item.tool_origin.mcp_server is not None
    assert mcp_item.tool_origin.mcp_server.name == "mcp_server"
    assert agent_item.tool_origin is not None
    assert agent_item.tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert agent_item.tool_origin.agent_as_tool is not None
    assert agent_item.tool_origin.agent_as_tool.name == "nested"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.version_info < (3, 10), reason="MCP tests require Python 3.10+")
async def test_tool_origin_streaming():
    """Test that tool origin is populated correctly in streaming scenarios."""
    model = FakeModel()
    server = FakeMCPServer(server_name="streaming_server")
    server.add_tool("streaming_tool", {})

    agent = Agent(name="test", model=model, mcp_servers=[server])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("streaming_tool", "")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="test")
    tool_call_items = []
    tool_output_items = []

    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            if isinstance(event.item, ToolCallItem):
                tool_call_items.append(event.item)
            elif isinstance(event.item, ToolCallOutputItem):
                tool_output_items.append(event.item)

    assert len(tool_call_items) == 1
    assert tool_call_items[0].tool_origin is not None
    assert tool_call_items[0].tool_origin.type == ToolOriginType.MCP
    assert tool_call_items[0].tool_origin.mcp_server is not None
    assert tool_call_items[0].tool_origin.mcp_server.name == "streaming_server"

    assert len(tool_output_items) == 1
    assert tool_output_items[0].tool_origin is not None
    assert tool_output_items[0].tool_origin.type == ToolOriginType.MCP
    assert tool_output_items[0].tool_origin.mcp_server is not None
    assert tool_output_items[0].tool_origin.mcp_server.name == "streaming_server"


@pytest.mark.asyncio
async def test_tool_origin_repr():
    """Test that ToolOrigin repr only shows relevant fields."""
    # FUNCTION origin
    function_origin = ToolOrigin(type=ToolOriginType.FUNCTION)
    assert "mcp_server_name" not in repr(function_origin)
    assert "agent_as_tool_name" not in repr(function_origin)

    # MCP origin
    if sys.version_info >= (3, 10):
        from .mcp.helpers import FakeMCPServer

        test_server = FakeMCPServer(server_name="test_server")
        mcp_origin = ToolOrigin(type=ToolOriginType.MCP, mcp_server=test_server)
        assert "mcp_server_name='test_server'" in repr(mcp_origin)
        assert "agent_as_tool_name" not in repr(mcp_origin)

    # AGENT_AS_TOOL origin
    model = FakeModel()
    test_agent = Agent(name="test_agent", model=model, instructions="Test agent")
    agent_origin = ToolOrigin(type=ToolOriginType.AGENT_AS_TOOL, agent_as_tool=test_agent)
    assert "agent_as_tool_name='test_agent'" in repr(agent_origin)
    assert "mcp_server_name" not in repr(agent_origin)


@pytest.mark.asyncio
async def test_tool_origin_defaults_to_function():
    """Test that tools without explicit origin default to FUNCTION."""
    model = FakeModel()

    # Create a FunctionTool directly without using @function_tool decorator
    async def test_func(ctx: RunContextWrapper, args: str) -> str:
        return "result"

    tool = FunctionTool(
        name="direct_tool",
        description="Direct tool",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=test_func,
    )

    agent = Agent(name="test", model=model, tools=[tool])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("direct_tool", "")],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="test")
    tool_call_items = [item for item in result.new_items if isinstance(item, ToolCallItem)]

    assert len(tool_call_items) == 1
    # Even though _tool_origin is None, _get_tool_origin_info defaults to FUNCTION
    assert tool_call_items[0].tool_origin is not None
    assert tool_call_items[0].tool_origin.type == ToolOriginType.FUNCTION


@pytest.mark.asyncio
async def test_non_function_tool_items_have_no_origin():
    """Test that non-FunctionTool items (computer, shell, etc.) don't have tool_origin."""
    model = FakeModel()

    @function_tool
    def func_tool() -> str:
        """Function tool."""
        return "result"

    agent = Agent(name="test", model=model, tools=[func_tool])

    # Create a ToolCallItem for a non-function tool (simulating computer/shell tool)
    computer_call = {
        "type": "computer_use_preview",
        "call_id": "call_123",
        "actions": [],
    }

    # This simulates what happens for non-FunctionTool items
    # They should not have tool_origin set
    item = ToolCallItem(
        raw_item=cast(ToolCallItemTypes, computer_call),
        agent=agent,
    )

    assert item.tool_origin is None

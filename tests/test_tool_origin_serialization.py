"""Tests for tool_origin serialization in RunState."""

from __future__ import annotations

import sys

import pytest

from agents import Agent, Runner, function_tool
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.run_state import RunState
from agents.tool import ToolOriginType

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message

if sys.version_info >= (3, 10):
    from .mcp.helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_serialize_tool_origin_function():
    """Test that FUNCTION tool_origin is serialized and deserialized."""
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
    assert len(tool_output_items) == 1

    tool_call_item = tool_call_items[0]
    tool_output_item = tool_output_items[0]

    # Verify tool_origin is set
    assert tool_call_item.tool_origin is not None
    assert tool_call_item.tool_origin.type == ToolOriginType.FUNCTION
    assert tool_output_item.tool_origin is not None
    assert tool_output_item.tool_origin.type == ToolOriginType.FUNCTION

    # Serialize and deserialize
    context = result.context_wrapper
    state = RunState(
        context=context,
        original_input="test",
        starting_agent=agent,
        max_turns=5,
    )
    state._generated_items = [tool_call_item, tool_output_item]

    json_data = state.to_json()
    deserialized_state = await RunState.from_json(agent, json_data)

    # Verify tool_origin was preserved
    deserialized_tool_call = next(
        item for item in deserialized_state._generated_items if isinstance(item, ToolCallItem)
    )
    deserialized_tool_output = next(
        item for item in deserialized_state._generated_items if isinstance(item, ToolCallOutputItem)
    )

    assert deserialized_tool_call.tool_origin is not None
    assert deserialized_tool_call.tool_origin.type == ToolOriginType.FUNCTION
    assert deserialized_tool_output.tool_origin is not None
    assert deserialized_tool_output.tool_origin.type == ToolOriginType.FUNCTION


@pytest.mark.asyncio
async def test_serialize_tool_origin_agent_as_tool():
    """Test that AGENT_AS_TOOL tool_origin is serialized and deserialized."""
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
    assert len(tool_output_items) == 1

    tool_call_item = tool_call_items[0]
    tool_output_item = tool_output_items[0]

    # Verify tool_origin is set
    assert tool_call_item.tool_origin is not None
    assert tool_call_item.tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert tool_call_item.tool_origin.agent_as_tool is not None
    assert tool_call_item.tool_origin.agent_as_tool.name == "nested_agent"
    assert tool_output_item.tool_origin is not None
    assert tool_output_item.tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert tool_output_item.tool_origin.agent_as_tool is not None
    assert tool_output_item.tool_origin.agent_as_tool.name == "nested_agent"

    # Serialize and deserialize
    context = result.context_wrapper
    state = RunState(
        context=context,
        original_input="test",
        starting_agent=orchestrator,
        max_turns=5,
    )
    state._generated_items = [tool_call_item, tool_output_item]

    json_data = state.to_json()
    deserialized_state = await RunState.from_json(orchestrator, json_data)

    # Verify tool_origin was preserved
    deserialized_tool_call = next(
        item for item in deserialized_state._generated_items if isinstance(item, ToolCallItem)
    )
    deserialized_tool_output = next(
        item for item in deserialized_state._generated_items if isinstance(item, ToolCallOutputItem)
    )

    assert deserialized_tool_call.tool_origin is not None
    assert deserialized_tool_call.tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert deserialized_tool_call.tool_origin.agent_as_tool is not None
    assert deserialized_tool_call.tool_origin.agent_as_tool.name == "nested_agent"
    assert deserialized_tool_output.tool_origin is not None
    assert deserialized_tool_output.tool_origin.type == ToolOriginType.AGENT_AS_TOOL
    assert deserialized_tool_output.tool_origin.agent_as_tool is not None
    assert deserialized_tool_output.tool_origin.agent_as_tool.name == "nested_agent"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.version_info < (3, 10), reason="MCP tests require Python 3.10+")
async def test_serialize_tool_origin_mcp():
    """Test that MCP tool_origin is serialized and deserialized."""
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
    assert len(tool_output_items) == 1

    tool_call_item = tool_call_items[0]
    tool_output_item = tool_output_items[0]

    # Verify tool_origin is set
    assert tool_call_item.tool_origin is not None
    assert tool_call_item.tool_origin.type == ToolOriginType.MCP
    assert tool_call_item.tool_origin.mcp_server is not None
    assert tool_call_item.tool_origin.mcp_server.name == "test_mcp_server"
    assert tool_output_item.tool_origin is not None
    assert tool_output_item.tool_origin.type == ToolOriginType.MCP
    assert tool_output_item.tool_origin.mcp_server is not None
    assert tool_output_item.tool_origin.mcp_server.name == "test_mcp_server"

    # Serialize and deserialize
    context = result.context_wrapper
    state = RunState(
        context=context,
        original_input="test",
        starting_agent=agent,
        max_turns=5,
    )
    state._generated_items = [tool_call_item, tool_output_item]

    json_data = state.to_json()
    deserialized_state = await RunState.from_json(agent, json_data)

    # Verify tool_origin was preserved
    deserialized_tool_call = next(
        item for item in deserialized_state._generated_items if isinstance(item, ToolCallItem)
    )
    deserialized_tool_output = next(
        item for item in deserialized_state._generated_items if isinstance(item, ToolCallOutputItem)
    )

    assert deserialized_tool_call.tool_origin is not None
    assert deserialized_tool_call.tool_origin.type == ToolOriginType.MCP
    # MCP server should be reconstructed from agent's mcp_servers
    assert deserialized_tool_call.tool_origin.mcp_server is not None
    assert deserialized_tool_call.tool_origin.mcp_server.name == "test_mcp_server"
    assert deserialized_tool_output.tool_origin is not None
    assert deserialized_tool_output.tool_origin.type == ToolOriginType.MCP
    assert deserialized_tool_output.tool_origin.mcp_server is not None
    assert deserialized_tool_output.tool_origin.mcp_server.name == "test_mcp_server"

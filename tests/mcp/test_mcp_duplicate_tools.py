"""Tests for MCP duplicate tool name handling."""

import pytest

from agents import Agent, FunctionTool, RunContextWrapper
from agents.mcp import MCPServer, MCPUtil

from .helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_get_all_function_tools_with_duplicate_names():
    """Test that duplicate tool names across MCP servers are automatically renamed."""
    server1 = FakeMCPServer(server_name="server1")
    server1.add_tool("search", {})
    server1.add_tool("fetch", {})

    server2 = FakeMCPServer(server_name="server2")
    server2.add_tool("search", {})  # duplicate name
    server2.add_tool("update", {})

    servers: list[MCPServer] = [server1, server2]
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    tools = await MCPUtil.get_all_function_tools(servers, False, run_context, agent)

    # Should have 4 tools total
    assert len(tools) == 4

    tool_names = [tool.name for tool in tools]
    # Original names from first server should be preserved
    assert "search" in tool_names
    assert "fetch" in tool_names
    # Duplicate from second server should be renamed
    assert "server2__search" in tool_names
    assert "update" in tool_names


@pytest.mark.asyncio
async def test_get_all_function_tools_with_duplicate_names_three_servers():
    """Test duplicate tool name handling with three servers having the same tool name."""
    server1 = FakeMCPServer(server_name="server1")
    server1.add_tool("search", {})

    server2 = FakeMCPServer(server_name="server2")
    server2.add_tool("search", {})  # duplicate

    server3 = FakeMCPServer(server_name="server3")
    server3.add_tool("search", {})  # another duplicate

    servers: list[MCPServer] = [server1, server2, server3]
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    tools = await MCPUtil.get_all_function_tools(servers, False, run_context, agent)

    assert len(tools) == 3
    tool_names = [tool.name for tool in tools]
    assert "search" in tool_names
    assert "server2__search" in tool_names
    assert "server3__search" in tool_names


@pytest.mark.asyncio
async def test_get_all_function_tools_normalizes_server_name_in_renamed_tool():
    """Test renamed tool names use a function-calling-safe server prefix."""
    server1 = FakeMCPServer(server_name="Primary Server")
    server1.add_tool("search", {})

    server2 = FakeMCPServer(server_name="Secondary-Server")
    server2.add_tool("search", {})  # duplicate

    servers: list[MCPServer] = [server1, server2]
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    tools = await MCPUtil.get_all_function_tools(servers, False, run_context, agent)

    tool_names = [tool.name for tool in tools]
    assert "search" in tool_names
    assert "secondary_server__search" in tool_names


@pytest.mark.asyncio
async def test_get_all_function_tools_no_duplicates():
    """Test that non-duplicate tool names are not affected."""
    server1 = FakeMCPServer(server_name="server1")
    server1.add_tool("search", {})

    server2 = FakeMCPServer(server_name="server2")
    server2.add_tool("fetch", {})  # no duplicate

    servers: list[MCPServer] = [server1, server2]
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    tools = await MCPUtil.get_all_function_tools(servers, False, run_context, agent)

    assert len(tools) == 2
    tool_names = [tool.name for tool in tools]
    assert "search" in tool_names
    assert "fetch" in tool_names
    # Should not have any prefixed names
    assert "server1__search" not in tool_names
    assert "server2__fetch" not in tool_names


@pytest.mark.asyncio
async def test_get_all_function_tools_preserves_mcp_origin():
    """Test that renamed tools preserve their MCP origin metadata."""
    server1 = FakeMCPServer(server_name="server1")
    server1.add_tool("search", {})

    server2 = FakeMCPServer(server_name="server2")
    server2.add_tool("search", {})  # duplicate

    servers: list[MCPServer] = [server1, server2]
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    tools = await MCPUtil.get_all_function_tools(servers, False, run_context, agent)

    # Find the renamed tool
    renamed_tool = next((t for t in tools if t.name == "server2__search"), None)
    assert renamed_tool is not None
    assert isinstance(renamed_tool, FunctionTool)
    # Check that MCP origin is preserved
    assert renamed_tool._tool_origin is not None
    assert renamed_tool._tool_origin.mcp_server_name == "server2"


@pytest.mark.asyncio
async def test_renamed_tool_can_be_invoked():
    """Test that renamed tools can still be invoked successfully."""
    server1 = FakeMCPServer(server_name="server1")
    server1.add_tool("search", {})

    server2 = FakeMCPServer(server_name="server2")
    server2.add_tool("search", {})  # duplicate

    servers: list[MCPServer] = [server1, server2]
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    tools = await MCPUtil.get_all_function_tools(servers, False, run_context, agent)

    # Find the renamed tool and invoke it
    renamed_tool = next((t for t in tools if t.name == "server2__search"), None)
    assert renamed_tool is not None
    assert isinstance(renamed_tool, FunctionTool)

    # The tool should be invocable
    assert renamed_tool.on_invoke_tool is not None

"""
Tests for MCP tool name conflict resolution.

This test file specifically tests the functionality that resolves tool name conflicts
when multiple MCP servers have tools with the same name by adding server name prefixes.
"""

import asyncio
import json
from typing import Any, Union

import pytest
from mcp import Tool as MCPTool
from mcp.types import CallToolResult, Content, TextContent

from agents import Agent
from agents.agent import AgentBase
from agents.exceptions import UserError
from agents.mcp import MCPUtil
from agents.mcp.server import MCPServer
from agents.run_context import RunContextWrapper


def create_test_agent(name: str = "test_agent") -> Agent:
    """Create a test agent for tool name conflict tests."""
    return Agent(name=name, instructions="Test agent")


def create_test_context() -> RunContextWrapper:
    """Create a test run context for tool name conflict tests."""
    return RunContextWrapper(context=None)


class MockMCPServer(MCPServer):
    """Mock MCP server for testing tool name prefixing functionality."""

    def __init__(self, name: str, tools: list[tuple[str, dict]]):
        super().__init__()
        self._name = name
        self._tools = [
            MCPTool(name=tool_name, description=f"Tool {tool_name}", inputSchema=schema)
            for tool_name, schema in tools
        ]

    @property
    def name(self) -> str:
        return self._name

    async def connect(self):
        pass

    async def cleanup(self):
        pass

    async def list_tools(
        self,
        run_context: Union[RunContextWrapper[Any], None] = None,
        agent: Union["AgentBase", None] = None,
    ) -> list[MCPTool]:
        """Return tools with server name prefix to simulate the actual server behavior."""
        tools = []
        for tool in self._tools:
            # Simulate the server adding prefix behavior
            tool_copy = MCPTool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.inputSchema
            )
            # Store original name
            tool_copy.original_name = tool.name
            # Add server name prefix
            tool_copy.name = f"{self.name}_{tool.name}"
            tools.append(tool_copy)
        return tools

    async def call_tool(self, tool_name: str, arguments: Union[dict[str, Any], None]) -> CallToolResult:
        """Mock tool invocation."""
        return CallToolResult(
            content=[TextContent(type="text", text=f"Result from {self.name}.{tool_name}")]
        )

    async def list_prompts(self):
        return {"prompts": []}

    async def get_prompt(self, name: str, arguments: Union[dict[str, Any], None] = None):
        return {"messages": []}


@pytest.mark.asyncio
async def test_tool_name_prefixing_single_server():
    """Test tool name prefixing functionality for a single server."""
    server = MockMCPServer("server1", [("run", {}), ("echo", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    tools = await server.list_tools(run_context, agent)
    
    # Verify tool names have correct prefixes
    assert len(tools) == 2
    tool_names = [tool.name for tool in tools]
    assert "server1_run" in tool_names
    assert "server1_echo" in tool_names
    
    # Verify original names are preserved
    for tool in tools:
        assert hasattr(tool, 'original_name')
        if tool.name == "server1_run":
            assert tool.original_name == "run"
        elif tool.name == "server1_echo":
            assert tool.original_name == "echo"


@pytest.mark.asyncio
async def test_tool_name_prefixing_multiple_servers():
    """Test tool name prefixing functionality with multiple servers having conflicting tool names."""
    server1 = MockMCPServer("server1", [("run", {}), ("echo", {})])
    server2 = MockMCPServer("server2", [("run", {}), ("list", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    # Get all tools
    tools1 = await server1.list_tools(run_context, agent)
    tools2 = await server2.list_tools(run_context, agent)
    
    all_tools = tools1 + tools2
    
    # Verify no duplicate tool names
    tool_names = [tool.name for tool in all_tools]
    assert len(tool_names) == len(set(tool_names)), "Tool names should be unique"
    
    # Verify specific tool names
    expected_names = ["server1_run", "server1_echo", "server2_run", "server2_list"]
    assert set(tool_names) == set(expected_names)
    
    # Verify original names are correctly preserved
    for tool in all_tools:
        assert hasattr(tool, 'original_name')
        if tool.name == "server1_run":
            assert tool.original_name == "run"
        elif tool.name == "server2_run":
            assert tool.original_name == "run"


@pytest.mark.asyncio
async def test_mcp_util_get_all_function_tools_no_conflicts():
    """Test MCPUtil.get_all_function_tools with no conflicting tool names."""
    server1 = MockMCPServer("server1", [("tool1", {}), ("tool2", {})])
    server2 = MockMCPServer("server2", [("tool3", {}), ("tool4", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    # Since tool names are now prefixed, there should be no conflicts
    function_tools = await MCPUtil.get_all_function_tools(
        [server1, server2], 
        convert_schemas_to_strict=False,
        run_context=run_context,
        agent=agent
    )
    
    assert len(function_tools) == 4
    tool_names = [tool.name for tool in function_tools]
    assert "server1_tool1" in tool_names
    assert "server1_tool2" in tool_names
    assert "server2_tool3" in tool_names
    assert "server2_tool4" in tool_names


@pytest.mark.asyncio
async def test_mcp_util_get_all_function_tools_with_resolved_conflicts():
    """Test MCPUtil.get_all_function_tools with originally conflicting tool names that are now resolved."""
    # Create two servers with same tool names
    server1 = MockMCPServer("server1", [("run", {}), ("echo", {})])
    server2 = MockMCPServer("server2", [("run", {}), ("list", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    # Since tool names are now prefixed, this should not raise an exception
    function_tools = await MCPUtil.get_all_function_tools(
        [server1, server2], 
        convert_schemas_to_strict=False,
        run_context=run_context,
        agent=agent
    )
    
    assert len(function_tools) == 4
    tool_names = [tool.name for tool in function_tools]
    assert "server1_run" in tool_names
    assert "server1_echo" in tool_names
    assert "server2_run" in tool_names
    assert "server2_list" in tool_names


class LegacyMockMCPServer(MCPServer):
    """Mock MCP server that simulates legacy behavior without name prefixing (for regression testing)."""

    def __init__(self, name: str, tools: list[tuple[str, dict]]):
        super().__init__()
        self._name = name
        self._tools = [
            MCPTool(name=tool_name, description=f"Tool {tool_name}", inputSchema=schema)
            for tool_name, schema in tools
        ]

    @property
    def name(self) -> str:
        return self._name

    async def connect(self):
        pass

    async def cleanup(self):
        pass

    async def list_tools(
        self,
        run_context: Union[RunContextWrapper[Any], None] = None,
        agent: Union["AgentBase", None] = None,
    ) -> list[MCPTool]:
        """Return tools without prefixes (simulating legacy behavior)."""
        return self._tools.copy()

    async def call_tool(self, tool_name: str, arguments: Union[dict[str, Any], None]) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Result from {self.name}.{tool_name}")]
        )

    async def list_prompts(self):
        return {"prompts": []}

    async def get_prompt(self, name: str, arguments: Union[dict[str, Any], None] = None):
        return {"messages": []}


@pytest.mark.asyncio
async def test_legacy_behavior_with_conflicts():
    """Test legacy behavior where conflicting tool names should raise UserError."""
    # Use servers without prefixing functionality
    server1 = LegacyMockMCPServer("server1", [("run", {}), ("echo", {})])
    server2 = LegacyMockMCPServer("server2", [("run", {}), ("list", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    # Should raise UserError due to tool name conflicts
    with pytest.raises(UserError, match="Duplicate tool names found"):
        await MCPUtil.get_all_function_tools(
            [server1, server2], 
            convert_schemas_to_strict=False,
            run_context=run_context,
            agent=agent
        )


@pytest.mark.asyncio
async def test_tool_invocation_uses_original_name():
    """Test that tool invocation uses the original name rather than the prefixed name."""
    server = MockMCPServer("server1", [("run", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    # Get tools
    tools = await server.list_tools(run_context, agent)
    tool = tools[0]
    
    # Verify tool has both prefixed name and original name
    assert tool.name == "server1_run"
    assert tool.original_name == "run"
    
    # Create function tool via MCPUtil
    function_tool = MCPUtil.to_function_tool(tool, server, convert_schemas_to_strict=False)
    
    # Verify function tool uses prefixed name
    assert function_tool.name == "server1_run"
    
    # Simulate tool invocation
    result = await MCPUtil.invoke_mcp_tool(server, tool, run_context, "{}")
    
    # Verify invocation succeeds
    assert "Result from server1.run" in result


@pytest.mark.asyncio 
async def test_empty_server_name():
    """Test handling of empty server names."""
    server = MockMCPServer("", [("run", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    tools = await server.list_tools(run_context, agent)
    
    # Verify even with empty server name, prefix is added to avoid empty names
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "_run"  # empty prefix + "_" + tool name
    assert tool.original_name == "run"


@pytest.mark.asyncio
async def test_special_characters_in_server_name():
    """Test handling of server names with special characters."""
    server = MockMCPServer("server-1.test", [("run", {})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    tools = await server.list_tools(run_context, agent)
    
    # Verify special characters in server names are handled correctly
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "server-1.test_run"
    assert tool.original_name == "run"


@pytest.mark.asyncio
async def test_tool_description_preserved():
    """Test that tool descriptions are preserved after adding name prefixes."""
    original_description = "This is a test tool"
    server = MockMCPServer("server1", [("run", {"description": original_description})])
    
    run_context = create_test_context()
    agent = create_test_agent()
    
    tools = await server.list_tools(run_context, agent)
    tool = tools[0]
    
    # Verify description is preserved
    assert tool.description == "Tool run"  # Based on MockMCPServer implementation
    assert tool.name == "server1_run"
    assert tool.original_name == "run" 
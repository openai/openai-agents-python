import logging
from typing import Any

import pytest
from mcp.types import Tool as MCPTool
from pydantic import BaseModel

from agents import FunctionTool, RunContextWrapper
from agents.exceptions import AgentsException, ModelBehaviorError
from agents.mcp import MCPServer, MCPUtil

from .helpers import FakeMCPServer


class Foo(BaseModel):
    bar: str
    baz: int


class Bar(BaseModel):
    qux: str


@pytest.mark.asyncio
async def test_get_all_function_tools():
    """Test that the get_all_function_tools function returns all function tools from a list of MCP
    servers.
    """
    names = ["test_tool_1", "test_tool_2", "test_tool_3", "test_tool_4", "test_tool_5"]
    schemas = [
        {},
        {},
        {},
        Foo.model_json_schema(),
        Bar.model_json_schema(),
    ]

    server1 = FakeMCPServer()
    server1.add_tool(names[0], schemas[0])
    server1.add_tool(names[1], schemas[1])

    server2 = FakeMCPServer()
    server2.add_tool(names[2], schemas[2])
    server2.add_tool(names[3], schemas[3])

    server3 = FakeMCPServer()
    server3.add_tool(names[4], schemas[4])

    servers: list[MCPServer] = [server1, server2, server3]
    tools = await MCPUtil.get_all_function_tools(servers)
    assert len(tools) == 5
    assert all(tool.name in names for tool in tools)

    for idx, tool in enumerate(tools):
        assert isinstance(tool, FunctionTool)
        assert tool.params_json_schema == schemas[idx]
        assert tool.name == names[idx]


@pytest.mark.asyncio
async def test_invoke_mcp_tool():
    """Test that the invoke_mcp_tool function invokes an MCP tool and returns the result."""
    server = FakeMCPServer()
    server.add_tool("test_tool_1", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool_1", inputSchema={})

    await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    # Just making sure it doesn't crash


@pytest.mark.asyncio
async def test_mcp_invoke_bad_json_errors(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)

    """Test that bad JSON input errors are logged and re-raised."""
    server = FakeMCPServer()
    server.add_tool("test_tool_1", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool_1", inputSchema={})

    with pytest.raises(ModelBehaviorError):
        await MCPUtil.invoke_mcp_tool(server, tool, ctx, "not_json")

    assert "Invalid JSON input for tool test_tool_1" in caplog.text


class CrashingFakeMCPServer(FakeMCPServer):
    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None):
        raise Exception("Crash!")


@pytest.mark.asyncio
async def test_mcp_invocation_crash_causes_error(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)

    """Test that bad JSON input errors are logged and re-raised."""
    server = CrashingFakeMCPServer()
    server.add_tool("test_tool_1", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool_1", inputSchema={})

    with pytest.raises(AgentsException):
        await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")

    assert "Error invoking MCP tool test_tool_1" in caplog.text

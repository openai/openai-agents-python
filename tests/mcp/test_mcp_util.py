import logging
from typing import Any

import pytest
from inline_snapshot import snapshot
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, TypeAdapter

from agents import Agent, FunctionTool, RunContextWrapper
from agents.exceptions import AgentsException, ModelBehaviorError
from agents.mcp import MCPServer, MCPUtil

from .helpers import FakeMCPServer


class Foo(BaseModel):
    bar: str
    baz: int


class Bar(BaseModel):
    qux: dict[str, str]


Baz = TypeAdapter(dict[str, str])


def _convertible_schema() -> dict[str, Any]:
    schema = Foo.model_json_schema()
    schema["additionalProperties"] = False
    return schema


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
    tools = await MCPUtil.get_all_function_tools(servers, convert_schemas_to_strict=False)
    assert len(tools) == 5
    assert all(tool.name in names for tool in tools)

    for idx, tool in enumerate(tools):
        assert isinstance(tool, FunctionTool)
        if schemas[idx] == {}:
            assert tool.params_json_schema == snapshot({"properties": {}})
        else:
            assert tool.params_json_schema == schemas[idx]
        assert tool.name == names[idx]

    # Also make sure it works with strict schemas
    tools = await MCPUtil.get_all_function_tools(servers, convert_schemas_to_strict=True)
    assert len(tools) == 5
    assert all(tool.name in names for tool in tools)


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


@pytest.mark.asyncio
async def test_agent_convert_schemas_true():
    """Test that setting convert_schemas_to_strict to True converts non-strict schemas to strict.
    - 'foo' tool is already strict and remains strict.
    - 'bar' tool is non-strict and becomes strict (additionalProperties set to False, etc).
    """
    strict_schema = Foo.model_json_schema()
    non_strict_schema = Baz.json_schema()
    possible_to_convert_schema = _convertible_schema()

    server = FakeMCPServer()
    server.add_tool("foo", strict_schema)
    server.add_tool("bar", non_strict_schema)
    server.add_tool("baz", possible_to_convert_schema)
    agent = Agent(
        name="test_agent", mcp_servers=[server], mcp_config={"convert_schemas_to_strict": True}
    )
    tools = await agent.get_mcp_tools()

    foo_tool = next(tool for tool in tools if tool.name == "foo")
    assert isinstance(foo_tool, FunctionTool)
    bar_tool = next(tool for tool in tools if tool.name == "bar")
    assert isinstance(bar_tool, FunctionTool)
    baz_tool = next(tool for tool in tools if tool.name == "baz")
    assert isinstance(baz_tool, FunctionTool)

    # Checks that additionalProperties is set to False
    assert foo_tool.params_json_schema == snapshot(
        {
            "properties": {
                "bar": {"title": "Bar", "type": "string"},
                "baz": {"title": "Baz", "type": "integer"},
            },
            "required": ["bar", "baz"],
            "title": "Foo",
            "type": "object",
            "additionalProperties": False,
        }
    )
    assert foo_tool.strict_json_schema is True, "foo_tool should be strict"

    # Checks that additionalProperties is set to False
    assert bar_tool.params_json_schema == snapshot(
        {"type": "object", "additionalProperties": {"type": "string"}, "properties": {}}
    )
    assert bar_tool.strict_json_schema is False, "bar_tool should not be strict"

    # Checks that additionalProperties is set to False
    assert baz_tool.params_json_schema == snapshot(
        {
            "properties": {
                "bar": {"title": "Bar", "type": "string"},
                "baz": {"title": "Baz", "type": "integer"},
            },
            "required": ["bar", "baz"],
            "title": "Foo",
            "type": "object",
            "additionalProperties": False,
        }
    )
    assert baz_tool.strict_json_schema is True, "baz_tool should be strict"


@pytest.mark.asyncio
async def test_agent_convert_schemas_false():
    """Test that setting convert_schemas_to_strict to False leaves tool schemas as non-strict.
    - 'foo' tool remains strict.
    - 'bar' tool remains non-strict (additionalProperties remains True).
    """
    strict_schema = Foo.model_json_schema()
    non_strict_schema = Baz.json_schema()
    possible_to_convert_schema = _convertible_schema()

    server = FakeMCPServer()
    server.add_tool("foo", strict_schema)
    server.add_tool("bar", non_strict_schema)
    server.add_tool("baz", possible_to_convert_schema)

    agent = Agent(
        name="test_agent", mcp_servers=[server], mcp_config={"convert_schemas_to_strict": False}
    )
    tools = await agent.get_mcp_tools()

    foo_tool = next(tool for tool in tools if tool.name == "foo")
    assert isinstance(foo_tool, FunctionTool)
    bar_tool = next(tool for tool in tools if tool.name == "bar")
    assert isinstance(bar_tool, FunctionTool)
    baz_tool = next(tool for tool in tools if tool.name == "baz")
    assert isinstance(baz_tool, FunctionTool)

    assert foo_tool.params_json_schema == strict_schema
    assert foo_tool.strict_json_schema is False, "Shouldn't be converted unless specified"

    assert bar_tool.params_json_schema == snapshot(
        {"type": "object", "additionalProperties": {"type": "string"}, "properties": {}}
    )
    assert bar_tool.strict_json_schema is False

    assert baz_tool.params_json_schema == possible_to_convert_schema
    assert baz_tool.strict_json_schema is False, "Shouldn't be converted unless specified"


@pytest.mark.asyncio
async def test_agent_convert_schemas_unset():
    """Test that leaving convert_schemas_to_strict unset (defaulting to False) leaves tool schemas
    as non-strict.
    - 'foo' tool remains strict.
    - 'bar' tool remains non-strict.
    """
    strict_schema = Foo.model_json_schema()
    non_strict_schema = Baz.json_schema()
    possible_to_convert_schema = _convertible_schema()

    server = FakeMCPServer()
    server.add_tool("foo", strict_schema)
    server.add_tool("bar", non_strict_schema)
    server.add_tool("baz", possible_to_convert_schema)
    agent = Agent(name="test_agent", mcp_servers=[server])
    tools = await agent.get_mcp_tools()

    foo_tool = next(tool for tool in tools if tool.name == "foo")
    assert isinstance(foo_tool, FunctionTool)
    bar_tool = next(tool for tool in tools if tool.name == "bar")
    assert isinstance(bar_tool, FunctionTool)
    baz_tool = next(tool for tool in tools if tool.name == "baz")
    assert isinstance(baz_tool, FunctionTool)

    assert foo_tool.params_json_schema == strict_schema
    assert foo_tool.strict_json_schema is False, "Shouldn't be converted unless specified"

    assert bar_tool.params_json_schema == snapshot(
        {"type": "object", "additionalProperties": {"type": "string"}, "properties": {}}
    )
    assert bar_tool.strict_json_schema is False

    assert baz_tool.params_json_schema == possible_to_convert_schema
    assert baz_tool.strict_json_schema is False, "Shouldn't be converted unless specified"


@pytest.mark.asyncio
async def test_util_adds_properties():
    """The MCP spec doesn't require the inputSchema to have `properties`, so we need to add it
    if it's missing.
    """
    schema = {
        "type": "object",
        "description": "Test tool",
    }

    server = FakeMCPServer()
    server.add_tool("test_tool", schema)

    tools = await MCPUtil.get_all_function_tools([server], convert_schemas_to_strict=False)
    tool = next(tool for tool in tools if tool.name == "test_tool")

    assert isinstance(tool, FunctionTool)
    assert "properties" in tool.params_json_schema
    assert tool.params_json_schema["properties"] == {}

    assert tool.params_json_schema == snapshot(
        {"type": "object", "description": "Test tool", "properties": {}}
    )

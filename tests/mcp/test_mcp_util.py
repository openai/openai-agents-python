import json
import logging
from typing import Any

import pytest
from inline_snapshot import snapshot
from mcp.types import CallToolResult, ImageContent, TextContent, Tool as MCPTool
from pydantic import BaseModel, TypeAdapter

from agents import Agent, FunctionTool, RunContextWrapper, default_tool_error_function
from agents.exceptions import AgentsException, ModelBehaviorError
from agents.mcp import MCPServer, MCPUtil
from agents.tool_context import ToolContext

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
    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")

    tools = await MCPUtil.get_all_function_tools(servers, False, run_context, agent)
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
    tools = await MCPUtil.get_all_function_tools(servers, True, run_context, agent)
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
async def test_mcp_meta_resolver_merges_and_passes():
    captured: dict[str, Any] = {}

    def resolve_meta(context):
        captured["run_context"] = context.run_context
        captured["server_name"] = context.server_name
        captured["tool_name"] = context.tool_name
        captured["arguments"] = context.arguments
        return {"request_id": "req-123", "locale": "ja"}

    server = FakeMCPServer(tool_meta_resolver=resolve_meta)
    server.add_tool("test_tool_1", {})

    ctx = RunContextWrapper(context={"request_id": "req-123"})
    tool = MCPTool(name="test_tool_1", inputSchema={})

    await MCPUtil.invoke_mcp_tool(
        server,
        tool,
        ctx,
        "{}",
        meta={"locale": "en", "extra": "value"},
    )

    assert server.tool_metas[-1] == {"request_id": "req-123", "locale": "en", "extra": "value"}
    assert captured["run_context"] is ctx
    assert captured["server_name"] == server.name
    assert captured["tool_name"] == "test_tool_1"
    assert captured["arguments"] == {}


@pytest.mark.asyncio
async def test_mcp_meta_resolver_does_not_mutate_arguments():
    def resolve_meta(context):
        if context.arguments is not None:
            context.arguments["mutated"] = "yes"
        return {"meta": "ok"}

    server = FakeMCPServer(tool_meta_resolver=resolve_meta)
    server.add_tool("test_tool_1", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool_1", inputSchema={})

    await MCPUtil.invoke_mcp_tool(server, tool, ctx, '{"foo": "bar"}')

    result = server.tool_results[-1]
    prefix = f"result_{tool.name}_"
    assert result.startswith(prefix)
    args = json.loads(result[len(prefix) :])
    assert args == {"foo": "bar"}


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
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ):
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
async def test_mcp_tool_graceful_error_handling(caplog: pytest.LogCaptureFixture):
    """Test that MCP tool errors are handled gracefully when invoked via FunctionTool.

    When an MCP tool is created via to_function_tool and then invoked, errors should be
    caught and converted to error messages instead of raising exceptions. This allows
    the agent to continue running after tool failures.
    """
    caplog.set_level(logging.DEBUG)

    # Create a server that will crash when calling a tool
    server = CrashingFakeMCPServer()
    server.add_tool("crashing_tool", {})

    # Convert MCP tool to FunctionTool (this wraps invoke_mcp_tool with error handling)
    mcp_tool = MCPTool(name="crashing_tool", inputSchema={})
    agent = Agent(name="test-agent")
    function_tool = MCPUtil.to_function_tool(
        mcp_tool, server, convert_schemas_to_strict=False, agent=agent
    )

    # Create tool context
    tool_context = ToolContext(
        context=None,
        tool_name="crashing_tool",
        tool_call_id="test_call_1",
        tool_arguments="{}",
    )

    # Invoke the tool - should NOT raise an exception, but return an error message
    result = await function_tool.on_invoke_tool(tool_context, "{}")

    # Verify that the result is an error message (not an exception)
    assert isinstance(result, str)
    assert "error" in result.lower() or "occurred" in result.lower()

    # Verify that the error message matches what default_tool_error_function would return
    # The error gets wrapped in AgentsException by invoke_mcp_tool, so we check for that format
    # The error message now includes the server name
    wrapped_error = AgentsException(
        "Error invoking MCP tool crashing_tool on server 'fake_mcp_server': Crash!"
    )
    expected_error_msg = default_tool_error_function(tool_context, wrapped_error)
    assert result == expected_error_msg

    # Verify that the error was logged
    assert (
        "MCP tool crashing_tool failed" in caplog.text or "Error invoking MCP tool" in caplog.text
    )


@pytest.mark.asyncio
async def test_mcp_tool_timeout_handling():
    """Test that MCP tool timeouts are handled gracefully.

    This simulates a timeout scenario where the MCP server call_tool raises a timeout error.
    The error should be caught and converted to an error message instead of halting the agent.
    """

    class TimeoutFakeMCPServer(FakeMCPServer):
        async def call_tool(
            self,
            tool_name: str,
            arguments: dict[str, Any] | None,
            meta: dict[str, Any] | None = None,
        ):
            # Simulate a timeout error - this would normally be wrapped in AgentsException
            # by invoke_mcp_tool
            raise Exception(
                "Timed out while waiting for response to ClientRequest. Waited 1.0 seconds."
            )

    server = TimeoutFakeMCPServer()
    server.add_tool("timeout_tool", {})

    # Convert MCP tool to FunctionTool
    mcp_tool = MCPTool(name="timeout_tool", inputSchema={})
    agent = Agent(name="test-agent")
    function_tool = MCPUtil.to_function_tool(
        mcp_tool, server, convert_schemas_to_strict=False, agent=agent
    )

    # Create tool context
    tool_context = ToolContext(
        context=None,
        tool_name="timeout_tool",
        tool_call_id="test_call_2",
        tool_arguments="{}",
    )

    # Invoke the tool - should NOT raise an exception
    result = await function_tool.on_invoke_tool(tool_context, "{}")

    # Verify that the result is an error message
    assert isinstance(result, str)
    assert "error" in result.lower() or "occurred" in result.lower()
    assert "Timed out" in result


@pytest.mark.asyncio
async def test_to_function_tool_legacy_call_without_agent_uses_server_policy():
    """Legacy three-argument to_function_tool calls should honor server policy."""

    server = FakeMCPServer(require_approval="always")
    server.add_tool("legacy_tool", {})

    # Backward compatibility: old call style omitted the `agent` argument.
    function_tool = MCPUtil.to_function_tool(
        MCPTool(name="legacy_tool", inputSchema={}),
        server,
        convert_schemas_to_strict=False,
    )

    # Legacy calls should still respect server-level approval settings.
    assert function_tool.needs_approval is True

    tool_context = ToolContext(
        context=None,
        tool_name="legacy_tool",
        tool_call_id="legacy_call_1",
        tool_arguments="{}",
    )
    result = await function_tool.on_invoke_tool(tool_context, "{}")
    if isinstance(result, str):
        assert "result_legacy_tool_" in result
    elif isinstance(result, dict):
        assert "result_legacy_tool_" in str(result.get("text", ""))
    else:
        pytest.fail(f"Unexpected tool result type: {type(result).__name__}")


@pytest.mark.asyncio
async def test_mcp_tool_failure_error_function_agent_default():
    """Agent-level failure_error_function should handle MCP tool failures."""

    def custom_failure(_ctx: RunContextWrapper[Any], _exc: Exception) -> str:
        return "custom_mcp_failure"

    server = CrashingFakeMCPServer()
    server.add_tool("crashing_tool", {})

    agent = Agent(
        name="test-agent",
        mcp_servers=[server],
        mcp_config={"failure_error_function": custom_failure},
    )
    run_context = RunContextWrapper(context=None)
    tools = await agent.get_mcp_tools(run_context)
    function_tool = next(tool for tool in tools if tool.name == "crashing_tool")
    assert isinstance(function_tool, FunctionTool)

    tool_context = ToolContext(
        context=None,
        tool_name="crashing_tool",
        tool_call_id="test_call_custom_1",
        tool_arguments="{}",
    )

    result = await function_tool.on_invoke_tool(tool_context, "{}")
    assert result == "custom_mcp_failure"


@pytest.mark.asyncio
async def test_mcp_tool_failure_error_function_server_override():
    """Server-level failure_error_function should override agent defaults."""

    def agent_failure(_ctx: RunContextWrapper[Any], _exc: Exception) -> str:
        return "agent_failure"

    def server_failure(_ctx: RunContextWrapper[Any], _exc: Exception) -> str:
        return "server_failure"

    server = CrashingFakeMCPServer(failure_error_function=server_failure)
    server.add_tool("crashing_tool", {})

    agent = Agent(
        name="test-agent",
        mcp_servers=[server],
        mcp_config={"failure_error_function": agent_failure},
    )
    run_context = RunContextWrapper(context=None)
    tools = await agent.get_mcp_tools(run_context)
    function_tool = next(tool for tool in tools if tool.name == "crashing_tool")
    assert isinstance(function_tool, FunctionTool)

    tool_context = ToolContext(
        context=None,
        tool_name="crashing_tool",
        tool_call_id="test_call_custom_2",
        tool_arguments="{}",
    )

    result = await function_tool.on_invoke_tool(tool_context, "{}")
    assert result == "server_failure"


@pytest.mark.asyncio
async def test_mcp_tool_failure_error_function_server_none_raises():
    """Server-level None should re-raise MCP tool failures."""

    server = CrashingFakeMCPServer(failure_error_function=None)
    server.add_tool("crashing_tool", {})

    agent = Agent(
        name="test-agent",
        mcp_servers=[server],
        mcp_config={"failure_error_function": default_tool_error_function},
    )
    run_context = RunContextWrapper(context=None)
    tools = await agent.get_mcp_tools(run_context)
    function_tool = next(tool for tool in tools if tool.name == "crashing_tool")
    assert isinstance(function_tool, FunctionTool)

    tool_context = ToolContext(
        context=None,
        tool_name="crashing_tool",
        tool_call_id="test_call_custom_3",
        tool_arguments="{}",
    )

    with pytest.raises(AgentsException):
        await function_tool.on_invoke_tool(tool_context, "{}")


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
    run_context = RunContextWrapper(context=None)
    tools = await agent.get_mcp_tools(run_context)

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
    run_context = RunContextWrapper(context=None)
    tools = await agent.get_mcp_tools(run_context)

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
async def test_mcp_fastmcp_behavior_verification():
    """Test that verifies the exact FastMCP _convert_to_content behavior we observed.

    Based on our testing, FastMCP's _convert_to_content function behaves as follows:
    - None → content=[] → MCPUtil returns "[]"
    - [] → content=[] → MCPUtil returns "[]"
    - {} → content=[TextContent(text="{}")] → MCPUtil returns full JSON
    - [{}] → content=[TextContent(text="{}")] → MCPUtil returns full JSON (flattened)
    - [[]] → content=[] → MCPUtil returns "[]" (recursive empty)
    """

    from mcp.types import TextContent

    server = FakeMCPServer()
    server.add_tool("test_tool", {})

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    # Case 1: None -> [].
    server._custom_content = []
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    assert result == [], f"None should return [], got {result}"

    # Case 2: [] -> [].
    server._custom_content = []
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    assert result == [], f"[] should return [], got {result}"

    # Case 3: {} -> {"type": "text", "text": "{}"}.
    server._custom_content = [TextContent(text="{}", type="text")]
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    expected = {"type": "text", "text": "{}"}
    assert result == expected, f"{{}} should return {expected}, got {result}"

    # Case 4: [{}] -> {"type": "text", "text": "{}"}.
    server._custom_content = [TextContent(text="{}", type="text")]
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    expected = {"type": "text", "text": "{}"}
    assert result == expected, f"[{{}}] should return {expected}, got {result}"

    # Case 5: [[]] -> [].
    server._custom_content = []
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    assert result == [], f"[[]] should return [], got {result}"

    # Case 6: String values work normally.
    server._custom_content = [TextContent(text="hello", type="text")]
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    expected = {"type": "text", "text": "hello"}
    assert result == expected, f"String should return {expected}, got {result}"

    # Case 7: Image content works normally.
    server._custom_content = [ImageContent(data="AAAA", mimeType="image/png", type="image")]
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    expected = {"type": "image", "image_url": "data:image/png;base64,AAAA"}
    assert result == expected, f"Image should return {expected}, got {result}"


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
    run_context = RunContextWrapper(context=None)
    tools = await agent.get_mcp_tools(run_context)

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

    run_context = RunContextWrapper(context=None)
    agent = Agent(name="test_agent", instructions="Test agent")
    tools = await MCPUtil.get_all_function_tools([server], False, run_context, agent)
    tool = next(tool for tool in tools if tool.name == "test_tool")

    assert isinstance(tool, FunctionTool)
    assert "properties" in tool.params_json_schema
    assert tool.params_json_schema["properties"] == {}

    assert tool.params_json_schema == snapshot(
        {"type": "object", "description": "Test tool", "properties": {}}
    )


class StructuredContentTestServer(FakeMCPServer):
    """Test server that allows setting both content and structured content for testing."""

    def __init__(self, use_structured_content: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.use_structured_content = use_structured_content
        self._test_content: list[Any] = []
        self._test_structured_content: dict[str, Any] | None = None

    def set_test_result(self, content: list[Any], structured_content: dict[str, Any] | None = None):
        """Set the content and structured content that will be returned by call_tool."""
        self._test_content = content
        self._test_structured_content = structured_content

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Return test result with specified content and structured content."""
        self.tool_calls.append(tool_name)

        return CallToolResult(
            content=self._test_content, structuredContent=self._test_structured_content
        )


@pytest.mark.parametrize(
    "use_structured_content,content,structured_content,expected_output",
    [
        # Scenario 1: use_structured_content=True with structured content available
        # Should return only structured content
        (
            True,
            [TextContent(text="text content", type="text")],
            {"data": "structured_value", "type": "structured"},
            '{"data": "structured_value", "type": "structured"}',
        ),
        # Scenario 2: use_structured_content=False with structured content available
        # Should return text content only (structured content ignored)
        (
            False,
            [TextContent(text="text content", type="text")],
            {"data": "structured_value", "type": "structured"},
            {"type": "text", "text": "text content"},
        ),
        # Scenario 3: use_structured_content=True but no structured content
        # Should fall back to text content
        (
            True,
            [TextContent(text="fallback text", type="text")],
            None,
            {"type": "text", "text": "fallback text"},
        ),
        # Scenario 4: use_structured_content=True with empty structured content (falsy)
        # Should fall back to text content
        (
            True,
            [TextContent(text="fallback text", type="text")],
            {},
            {"type": "text", "text": "fallback text"},
        ),
        # Scenario 5: use_structured_content=True, structured content available, empty text content
        # Should return structured content
        (True, [], {"message": "only structured"}, '{"message": "only structured"}'),
        # Scenario 6: use_structured_content=False, multiple text content items
        # Should return JSON array of text content
        (
            False,
            [TextContent(text="first", type="text"), TextContent(text="second", type="text")],
            {"ignored": "structured"},
            [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
        ),
        # Scenario 7: use_structured_content=True, multiple text content, with structured content
        # Should return only structured content (text content ignored)
        (
            True,
            [
                TextContent(text="ignored first", type="text"),
                TextContent(text="ignored second", type="text"),
            ],
            {"priority": "structured"},
            '{"priority": "structured"}',
        ),
        # Scenario 8: use_structured_content=False, empty content
        # Should return empty array
        (False, [], None, []),
        # Scenario 9: use_structured_content=True, empty content, no structured content
        # Should return empty array
        (True, [], None, []),
    ],
)
@pytest.mark.asyncio
async def test_structured_content_handling(
    use_structured_content: bool,
    content: list[Any],
    structured_content: dict[str, Any] | None,
    expected_output: str,
):
    """Test that structured content handling works correctly with various scenarios.

    This test verifies the fix for the MCP tool output logic where:
    - When use_structured_content=True and structured content exists, it's used exclusively
    - When use_structured_content=False or no structured content, falls back to text content
    - The old unreachable code path has been fixed
    """

    server = StructuredContentTestServer(use_structured_content=use_structured_content)
    server.add_tool("test_tool", {})
    server.set_test_result(content, structured_content)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="test_tool", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")
    assert result == expected_output


@pytest.mark.asyncio
async def test_structured_content_priority_over_text():
    """Test that when use_structured_content=True, structured content takes priority.

    This verifies the core fix: structured content should be used exclusively when available
    and requested, not concatenated with text content.
    """

    server = StructuredContentTestServer(use_structured_content=True)
    server.add_tool("priority_test", {})

    # Set both text and structured content
    text_content = [TextContent(text="This should be ignored", type="text")]
    structured_content = {"important": "This should be returned", "value": 42}
    server.set_test_result(text_content, structured_content)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="priority_test", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should return only structured content
    import json

    assert isinstance(result, str)
    parsed_result = json.loads(result)
    assert parsed_result == structured_content
    assert "This should be ignored" not in result


@pytest.mark.asyncio
async def test_structured_content_fallback_behavior():
    """Test fallback behavior when structured content is requested but not available.

    This verifies that the logic properly falls back to text content processing
    when use_structured_content=True but no structured content is provided.
    """

    server = StructuredContentTestServer(use_structured_content=True)
    server.add_tool("fallback_test", {})

    # Set only text content, no structured content
    text_content = [TextContent(text="Fallback content", type="text")]
    server.set_test_result(text_content, None)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="fallback_test", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should fall back to text content
    assert isinstance(result, dict)
    assert result["type"] == "text"
    assert result["text"] == "Fallback content"


@pytest.mark.asyncio
async def test_backwards_compatibility_unchanged():
    """Test that default behavior (use_structured_content=False) remains unchanged.

    This ensures the fix doesn't break existing behavior for servers that don't use
    structured content or have it disabled.
    """

    server = StructuredContentTestServer(use_structured_content=False)
    server.add_tool("compat_test", {})

    # Set both text and structured content
    text_content = [TextContent(text="Traditional text output", type="text")]
    structured_content = {"modern": "structured output"}
    server.set_test_result(text_content, structured_content)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="compat_test", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should return only text content (structured content ignored)
    assert isinstance(result, dict)
    assert result["type"] == "text"
    assert result["text"] == "Traditional text output"
    assert "modern" not in result


@pytest.mark.asyncio
async def test_empty_structured_content_fallback():
    """Test that empty structured content (falsy values) falls back to text content.

    This tests the condition: if server.use_structured_content and result.structuredContent
    where empty dict {} should be falsy and trigger fallback.
    """

    server = StructuredContentTestServer(use_structured_content=True)
    server.add_tool("empty_structured_test", {})

    # Set text content and empty structured content
    text_content = [TextContent(text="Should use this text", type="text")]
    empty_structured: dict[str, Any] = {}  # This should be falsy
    server.set_test_result(text_content, empty_structured)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="empty_structured_test", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should fall back to text content because empty dict is falsy
    assert isinstance(result, dict)
    assert result["type"] == "text"
    assert result["text"] == "Should use this text"


@pytest.mark.asyncio
async def test_complex_structured_content():
    """Test handling of complex structured content with nested objects and arrays."""

    server = StructuredContentTestServer(use_structured_content=True)
    server.add_tool("complex_test", {})

    # Set complex structured content
    complex_structured = {
        "results": [
            {"id": 1, "name": "Item 1", "metadata": {"tags": ["a", "b"]}},
            {"id": 2, "name": "Item 2", "metadata": {"tags": ["c", "d"]}},
        ],
        "pagination": {"page": 1, "total": 2},
        "status": "success",
    }

    server.set_test_result([], complex_structured)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="complex_test", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should return the complex structured content as-is
    import json

    assert isinstance(result, str)
    parsed_result = json.loads(result)
    assert parsed_result == complex_structured
    assert len(parsed_result["results"]) == 2
    assert parsed_result["pagination"]["total"] == 2


@pytest.mark.asyncio
async def test_multiple_content_items_with_structured():
    """Test that multiple text content items are ignored when structured content is available.

    This verifies that the new logic prioritizes structured content over multiple text items,
    which was one of the scenarios that had unclear behavior in the old implementation.
    """

    server = StructuredContentTestServer(use_structured_content=True)
    server.add_tool("multi_content_test", {})

    # Set multiple text content items and structured content
    text_content = [
        TextContent(text="First text item", type="text"),
        TextContent(text="Second text item", type="text"),
        TextContent(text="Third text item", type="text"),
    ]
    structured_content = {"chosen": "structured over multiple text items"}
    server.set_test_result(text_content, structured_content)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="multi_content_test", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should return only structured content, ignoring all text items
    import json

    assert isinstance(result, str)
    parsed_result = json.loads(result)
    assert parsed_result == structured_content
    assert "First text item" not in result
    assert "Second text item" not in result
    assert "Third text item" not in result


@pytest.mark.asyncio
async def test_multiple_content_items_without_structured():
    """Test that multiple text content items are properly handled when no structured content."""

    server = StructuredContentTestServer(use_structured_content=True)
    server.add_tool("multi_text_test", {})

    # Set multiple text content items without structured content
    text_content = [TextContent(text="First", type="text"), TextContent(text="Second", type="text")]
    server.set_test_result(text_content, None)

    ctx = RunContextWrapper(context=None)
    tool = MCPTool(name="multi_text_test", inputSchema={})

    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "{}")

    # Should return JSON array of text content items
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "First"
    assert result[1]["type"] == "text"
    assert result[1]["text"] == "Second"

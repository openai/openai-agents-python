import logging
from typing import Any

import pytest
from inline_snapshot import snapshot
from mcp.types import CallToolResult, TextContent, Tool as MCPTool
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

    # Case 1: None -> "[]".
    server._custom_content = []
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    assert result == "[]", f"None should return '[]', got {result}"

    # Case 2: [] -> "[]".
    server._custom_content = []
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    assert result == "[]", f"[] should return '[]', got {result}"

    # Case 3: {} -> {"type":"text","text":"{}","annotations":null,"meta":null}.
    server._custom_content = [TextContent(text="{}", type="text")]
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    expected = '{"type":"text","text":"{}","annotations":null,"meta":null}'
    assert result == expected, f"{{}} should return {expected}, got {result}"

    # Case 4: [{}] -> {"type":"text","text":"{}","annotations":null,"meta":null}.
    server._custom_content = [TextContent(text="{}", type="text")]
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    expected = '{"type":"text","text":"{}","annotations":null,"meta":null}'
    assert result == expected, f"[{{}}] should return {expected}, got {result}"

    # Case 5: [[]] -> "[]".
    server._custom_content = []
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    assert result == "[]", f"[[]] should return '[]', got {result}"

    # Case 6: String values work normally.
    server._custom_content = [TextContent(text="hello", type="text")]
    result = await MCPUtil.invoke_mcp_tool(server, tool, ctx, "")
    expected = '{"type":"text","text":"hello","annotations":null,"meta":null}'
    assert result == expected, f"String should return {expected}, got {result}"


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

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> CallToolResult:
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
            '{"type":"text","text":"text content","annotations":null,"meta":null}',
        ),
        # Scenario 3: use_structured_content=True but no structured content
        # Should fall back to text content
        (
            True,
            [TextContent(text="fallback text", type="text")],
            None,
            '{"type":"text","text":"fallback text","annotations":null,"meta":null}',
        ),
        # Scenario 4: use_structured_content=True with empty structured content (falsy)
        # Should fall back to text content
        (
            True,
            [TextContent(text="fallback text", type="text")],
            {},
            '{"type":"text","text":"fallback text","annotations":null,"meta":null}',
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
            '[{"type": "text", "text": "first", "annotations": null, "meta": null}, '
            '{"type": "text", "text": "second", "annotations": null, "meta": null}]',
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
        (False, [], None, "[]"),
        # Scenario 9: use_structured_content=True, empty content, no structured content
        # Should return empty array
        (True, [], None, "[]"),
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
    import json

    parsed_result = json.loads(result)
    assert parsed_result["text"] == "Fallback content"
    assert parsed_result["type"] == "text"


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
    import json

    parsed_result = json.loads(result)
    assert parsed_result["text"] == "Traditional text output"
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
    import json

    parsed_result = json.loads(result)
    assert parsed_result["text"] == "Should use this text"
    assert parsed_result["type"] == "text"


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
    import json

    parsed_result = json.loads(result)
    assert isinstance(parsed_result, list)
    assert len(parsed_result) == 2
    assert parsed_result[0]["text"] == "First"
    assert parsed_result[1]["text"] == "Second"

import functools
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

from typing_extensions import NotRequired, TypedDict

from .. import _debug
from ..exceptions import AgentsException, ModelBehaviorError, UserError
from ..logger import logger
from ..run_context import RunContextWrapper
from ..strict_schema import ensure_strict_json_schema
from ..tool import FunctionTool, Tool
from ..tracing import FunctionSpanData, get_current_span, mcp_tools_span
from ..util._types import MaybeAwaitable

if TYPE_CHECKING:
    from mcp.types import Tool as MCPTool

    from ..agent import AgentBase
    from .server import MCPServer


@dataclass
class ToolFilterContext:
    """Context information available to tool filter functions."""

    run_context: RunContextWrapper[Any]
    """The current run context."""

    agent: "AgentBase"
    """The agent that is requesting the tool list."""

    server_name: str
    """The name of the MCP server."""


ToolFilterCallable = Callable[["ToolFilterContext", "MCPTool"], MaybeAwaitable[bool]]
"""A function that determines whether a tool should be available.

Args:
    context: The context information including run context, agent, and server name.
    tool: The MCP tool to filter.

Returns:
    Whether the tool should be available (True) or filtered out (False).
"""


class ToolFilterStatic(TypedDict):
    """Static tool filter configuration using allowlists and blocklists."""

    allowed_tool_names: NotRequired[list[str]]
    """Optional list of tool names to allow (whitelist).
    If set, only these tools will be available."""

    blocked_tool_names: NotRequired[list[str]]
    """Optional list of tool names to exclude (blacklist).
    If set, these tools will be filtered out."""


ToolFilter = Union[ToolFilterCallable, ToolFilterStatic, None]
"""A tool filter that can be either a function, static configuration, or None (no filtering)."""


def create_static_tool_filter(
    allowed_tool_names: Optional[list[str]] = None,
    blocked_tool_names: Optional[list[str]] = None,
) -> Optional[ToolFilterStatic]:
    """Create a static tool filter from allowlist and blocklist parameters.

    This is a convenience function for creating a ToolFilterStatic.

    Args:
        allowed_tool_names: Optional list of tool names to allow (whitelist).
        blocked_tool_names: Optional list of tool names to exclude (blacklist).

    Returns:
        A ToolFilterStatic if any filtering is specified, None otherwise.
    """
    if allowed_tool_names is None and blocked_tool_names is None:
        return None

    filter_dict: ToolFilterStatic = {}
    if allowed_tool_names is not None:
        filter_dict["allowed_tool_names"] = allowed_tool_names
    if blocked_tool_names is not None:
        filter_dict["blocked_tool_names"] = blocked_tool_names

    return filter_dict


class MCPUtil:
    """Set of utilities for interop between MCP and Agents SDK tools."""

    @classmethod
    async def get_all_function_tools(
        cls,
        servers: list["MCPServer"],
        convert_schemas_to_strict: bool,
        run_context: RunContextWrapper[Any],
        agent: "AgentBase",
    ) -> list[Tool]:
        """Get all function tools from a list of MCP servers."""
        tools = []
        tool_names: set[str] = set()
        for server in servers:
            server_tools = await cls.get_function_tools(
                server, convert_schemas_to_strict, run_context, agent
            )
            server_tool_names = {tool.name for tool in server_tools}
            if len(server_tool_names & tool_names) > 0:
                raise UserError(
                    f"Duplicate tool names found across MCP servers: "
                    f"{server_tool_names & tool_names}"
                )
            tool_names.update(server_tool_names)
            tools.extend(server_tools)

        return tools

    @classmethod
    async def get_function_tools(
        cls,
        server: "MCPServer",
        convert_schemas_to_strict: bool,
        run_context: RunContextWrapper[Any],
        agent: "AgentBase",
    ) -> list[Tool]:
        """Get all function tools from a single MCP server."""

        with mcp_tools_span(server=server.name) as span:
            tools = await server.list_tools(run_context, agent)
            span.span_data.result = [tool.name for tool in tools]

        return [cls.to_function_tool(tool, server, convert_schemas_to_strict) for tool in tools]

    @classmethod
    def to_function_tool(
        cls, tool: "MCPTool", server: "MCPServer", convert_schemas_to_strict: bool
    ) -> FunctionTool:
        """Convert an MCP tool to an Agents SDK function tool."""
        invoke_func = functools.partial(cls.invoke_mcp_tool, server, tool)
        schema, is_strict = tool.inputSchema, False

        # MCP spec doesn't require the inputSchema to have `properties`, but OpenAI spec does.
        if "properties" not in schema:
            schema["properties"] = {}

        if convert_schemas_to_strict:
            try:
                schema = ensure_strict_json_schema(schema)
                is_strict = True
            except Exception as e:
                logger.info(f"Error converting MCP schema to strict mode: {e}")

        return FunctionTool(
            name=tool.name,
            description=tool.description or "",
            params_json_schema=schema,
            on_invoke_tool=invoke_func,
            strict_json_schema=is_strict,
        )

    @classmethod
    async def invoke_mcp_tool(
        cls, server: "MCPServer", tool: "MCPTool", context: RunContextWrapper[Any], input_json: str
    ) -> str:
        """Invoke an MCP tool and return the result as a string."""
        try:
            json_data: dict[str, Any] = json.loads(input_json) if input_json else {}
        except Exception as e:
            if _debug.DONT_LOG_TOOL_DATA:
                logger.debug(f"Invalid JSON input for tool {tool.name}")
            else:
                logger.debug(f"Invalid JSON input for tool {tool.name}: {input_json}")
            raise ModelBehaviorError(
                f"Invalid JSON input for tool {tool.name}: {input_json}"
            ) from e

        if _debug.DONT_LOG_TOOL_DATA:
            logger.debug(f"Invoking MCP tool {tool.name}")
        else:
            logger.debug(f"Invoking MCP tool {tool.name} with input {input_json}")

        try:
            result = await server.call_tool(tool.name, json_data)
        except Exception as e:
            logger.error(f"Error invoking MCP tool {tool.name}: {e}")
            raise AgentsException(f"Error invoking MCP tool {tool.name}: {e}") from e

        if _debug.DONT_LOG_TOOL_DATA:
            logger.debug(f"MCP tool {tool.name} completed.")
        else:
            logger.debug(f"MCP tool {tool.name} returned {result}")

        # The MCP tool result is a list of content items, whereas OpenAI tool outputs are a single
        # string. We'll try to convert.
        if len(result.content) == 1:
            tool_output = result.content[0].model_dump_json()
            # Append structured content if it exists and we're using it.
            if server.use_structured_content and result.structuredContent:
                tool_output = f"{tool_output}\n{json.dumps(result.structuredContent)}"
        elif len(result.content) > 1:
            tool_results = [item.model_dump(mode="json") for item in result.content]
            if server.use_structured_content and result.structuredContent:
                tool_results.append(result.structuredContent)
            tool_output = json.dumps(tool_results)
        elif server.use_structured_content and result.structuredContent:
            tool_output = json.dumps(result.structuredContent)
        else:
            # Empty content is a valid result (e.g., "no results found")
            tool_output = "[]"

        current_span = get_current_span()
        if current_span:
            if isinstance(current_span.span_data, FunctionSpanData):
                current_span.span_data.output = tool_output
                current_span.span_data.mcp_data = {
                    "server": server.name,
                }
            else:
                logger.warning(
                    f"Current span is not a FunctionSpanData, skipping tool output: {current_span}"
                )

        return tool_output

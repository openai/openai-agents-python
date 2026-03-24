from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, Union

import httpx
from typing_extensions import NotRequired, TypedDict

from .. import _debug
from .._mcp_tool_metadata import resolve_mcp_tool_description_for_model, resolve_mcp_tool_title
from ..exceptions import AgentsException, MCPToolCancellationError, ModelBehaviorError, UserError

try:
    from mcp.shared.exceptions import McpError as _McpError
except ImportError:  # pragma: no cover – mcp is optional on Python < 3.10
    _McpError = None  # type: ignore[assignment, misc]
from ..logger import logger
from ..run_context import RunContextWrapper
from ..strict_schema import ensure_strict_json_schema
from ..tool import (
    FunctionTool,
    Tool,
    ToolErrorFunction,
    ToolOutputImageDict,
    ToolOutputTextDict,
    _build_handled_function_tool_error_handler,
    _build_wrapped_function_tool,
    default_tool_error_function,
)
from ..tracing import FunctionSpanData, get_current_span, mcp_tools_span
from ..util._types import MaybeAwaitable

if TYPE_CHECKING:
    ToolOutputItem = ToolOutputTextDict | ToolOutputImageDict
    ToolOutput = str | ToolOutputItem | list[ToolOutputItem]
else:
    ToolOutputItem = Union[ToolOutputTextDict, ToolOutputImageDict]  # noqa: UP007
    ToolOutput = Union[str, ToolOutputItem, list[ToolOutputItem]]  # noqa: UP007

if TYPE_CHECKING:
    from mcp.types import Tool as MCPTool

    from ..agent import AgentBase
    from .server import MCPServer


class HttpClientFactory(Protocol):
    """Protocol for HTTP client factory functions.

    This interface matches the MCP SDK's McpHttpClientFactory but is defined locally
    to avoid accessing internal MCP SDK modules.
    """

    def __call__(
        self,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient: ...


@dataclass
class ToolFilterContext:
    """Context information available to tool filter functions."""

    run_context: RunContextWrapper[Any]
    """The current run context."""

    agent: AgentBase
    """The agent that is requesting the tool list."""

    server_name: str
    """The name of the MCP server."""


if TYPE_CHECKING:
    ToolFilterCallable = Callable[[ToolFilterContext, MCPTool], MaybeAwaitable[bool]]
else:
    ToolFilterCallable = Callable[[ToolFilterContext, Any], MaybeAwaitable[bool]]
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


if TYPE_CHECKING:
    ToolFilter = ToolFilterCallable | ToolFilterStatic | None
else:
    ToolFilter = Union[ToolFilterCallable, ToolFilterStatic, None]  # noqa: UP007
"""A tool filter that can be either a function, static configuration, or None (no filtering)."""


@dataclass
class MCPToolMetaContext:
    """Context information available to MCP tool meta resolver functions."""

    run_context: RunContextWrapper[Any]
    """The current run context."""

    server_name: str
    """The name of the MCP server."""

    tool_name: str
    """The name of the tool being invoked."""

    arguments: dict[str, Any] | None
    """The parsed tool arguments."""


if TYPE_CHECKING:
    MCPToolMetaResolver = Callable[
        [MCPToolMetaContext],
        MaybeAwaitable[dict[str, Any] | None],
    ]
else:
    MCPToolMetaResolver = Callable[..., Any]
"""A function that produces MCP request metadata for tool calls.

Args:
    context: Context information about the tool invocation.

Returns:
    A dict to send as MCP `_meta`, or None to omit metadata.
"""


def create_static_tool_filter(
    allowed_tool_names: list[str] | None = None,
    blocked_tool_names: list[str] | None = None,
) -> ToolFilterStatic | None:
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

    @staticmethod
    def _extract_static_meta(tool: Any) -> dict[str, Any] | None:
        meta = getattr(tool, "meta", None)
        if isinstance(meta, dict):
            return copy.deepcopy(meta)

        model_extra = getattr(tool, "model_extra", None)
        if isinstance(model_extra, dict):
            extra_meta = model_extra.get("meta")
            if isinstance(extra_meta, dict):
                return copy.deepcopy(extra_meta)

        model_dump = getattr(tool, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                dumped_meta = dumped.get("meta")
                if isinstance(dumped_meta, dict):
                    return copy.deepcopy(dumped_meta)

        return None

    @classmethod
    async def get_all_function_tools(
        cls,
        servers: list[MCPServer],
        convert_schemas_to_strict: bool,
        run_context: RunContextWrapper[Any],
        agent: AgentBase,
        failure_error_function: ToolErrorFunction | None = default_tool_error_function,
    ) -> list[Tool]:
        """Get all function tools from a list of MCP servers."""
        tools = []
        tool_names: set[str] = set()
        for server in servers:
            server_tools = await cls.get_function_tools(
                server,
                convert_schemas_to_strict,
                run_context,
                agent,
                failure_error_function=failure_error_function,
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
        server: MCPServer,
        convert_schemas_to_strict: bool,
        run_context: RunContextWrapper[Any],
        agent: AgentBase,
        failure_error_function: ToolErrorFunction | None = default_tool_error_function,
    ) -> list[Tool]:
        """Get all function tools from a single MCP server."""

        with mcp_tools_span(server=server.name) as span:
            tools = await server.list_tools(run_context, agent)
            span.span_data.result = [tool.name for tool in tools]

        return [
            cls.to_function_tool(
                tool,
                server,
                convert_schemas_to_strict,
                agent,
                failure_error_function=failure_error_function,
            )
            for tool in tools
        ]

    @classmethod
    def to_function_tool(
        cls,
        tool: MCPTool,
        server: MCPServer,
        convert_schemas_to_strict: bool,
        agent: AgentBase | None = None,
        failure_error_function: ToolErrorFunction | None = default_tool_error_function,
    ) -> FunctionTool:
        """Convert an MCP tool to an Agents SDK function tool.

        The ``agent`` parameter is optional for backward compatibility with older
        call sites that used ``MCPUtil.to_function_tool(tool, server, strict)``.
        When omitted, this helper preserves the historical behavior for static
        policies. If the server uses a callable approval policy, approvals default
        to required to avoid bypassing dynamic checks.
        """
        static_meta = cls._extract_static_meta(tool)
        invoke_func_impl = functools.partial(
            cls.invoke_mcp_tool,
            server,
            tool,
            meta=static_meta,
        )
        effective_failure_error_function = server._get_failure_error_function(
            failure_error_function
        )
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

        needs_approval: (
            bool | Callable[[RunContextWrapper[Any], dict[str, Any], str], Awaitable[bool]]
        ) = server._get_needs_approval_for_tool(tool, agent)

        function_tool = _build_wrapped_function_tool(
            name=tool.name,
            description=resolve_mcp_tool_description_for_model(tool),
            params_json_schema=schema,
            invoke_tool_impl=invoke_func_impl,
            on_handled_error=_build_handled_function_tool_error_handler(
                span_message="Error running tool (non-fatal)",
                log_label="MCP tool",
            ),
            failure_error_function=effective_failure_error_function,
            strict_json_schema=is_strict,
            needs_approval=needs_approval,
            mcp_title=resolve_mcp_tool_title(tool),
        )
        return function_tool

    @staticmethod
    def _merge_mcp_meta(
        resolved_meta: dict[str, Any] | None,
        explicit_meta: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if resolved_meta is None and explicit_meta is None:
            return None
        merged: dict[str, Any] = {}
        if resolved_meta is not None:
            merged.update(resolved_meta)
        if explicit_meta is not None:
            merged.update(explicit_meta)
        return merged

    @classmethod
    async def _resolve_meta(
        cls,
        server: MCPServer,
        context: RunContextWrapper[Any],
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        meta_resolver = getattr(server, "tool_meta_resolver", None)
        if meta_resolver is None:
            return None

        arguments_copy = copy.deepcopy(arguments) if arguments is not None else None
        resolver_context = MCPToolMetaContext(
            run_context=context,
            server_name=server.name,
            tool_name=tool_name,
            arguments=arguments_copy,
        )
        result = meta_resolver(resolver_context)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return None
        if not isinstance(result, dict):
            raise TypeError("MCP meta resolver must return a dict or None.")
        return result

    @classmethod
    async def invoke_mcp_tool(
        cls,
        server: MCPServer,
        tool: MCPTool,
        context: RunContextWrapper[Any],
        input_json: str,
        *,
        meta: dict[str, Any] | None = None,
    ) -> ToolOutput:
        """Invoke an MCP tool and return the result as ToolOutput."""
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
            resolved_meta = await cls._resolve_meta(server, context, tool.name, json_data)
            merged_meta = cls._merge_mcp_meta(resolved_meta, meta)
            call_task = asyncio.create_task(
                server.call_tool(tool.name, json_data)
                if merged_meta is None
                else server.call_tool(tool.name, json_data, meta=merged_meta)
            )
            try:
                done, _ = await asyncio.wait({call_task}, return_when=asyncio.FIRST_COMPLETED)
                finished_task = done.pop()
                if finished_task.cancelled():
                    raise MCPToolCancellationError(
                        f"Failed to call tool '{tool.name}' on MCP server '{server.name}': "
                        "tool execution was cancelled."
                    )
                result = finished_task.result()
            except asyncio.CancelledError:
                if not call_task.done():
                    call_task.cancel()
                try:
                    await call_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise
        except (UserError, MCPToolCancellationError):
            # Re-raise handled tool-call errors as-is; the FunctionTool failure pipeline
            # will format them into model-visible tool errors when appropriate.
            raise
        except Exception as e:
            if _McpError is not None and isinstance(e, _McpError):
                # An MCP-level error (e.g. upstream HTTP 4xx/5xx, tool not found, etc.)
                # is not a programming error – re-raise so the FunctionTool failure
                # pipeline (failure_error_function) can handle it.  The default handler
                # will surface the message as a structured error result; callers who set
                # failure_error_function=None will have the error raised as documented.
                error_text = e.error.message if hasattr(e, "error") and e.error else str(e)
                logger.warning(
                    f"MCP tool {tool.name} on server '{server.name}' returned an error: "
                    f"{error_text}"
                )
                raise

            logger.error(f"Error invoking MCP tool {tool.name} on server '{server.name}': {e}")
            raise AgentsException(
                f"Error invoking MCP tool {tool.name} on server '{server.name}': {e}"
            ) from e

        if _debug.DONT_LOG_TOOL_DATA:
            logger.debug(f"MCP tool {tool.name} completed.")
        else:
            logger.debug(f"MCP tool {tool.name} returned {result}")

        # If structured content is requested and available, use it exclusively
        tool_output: ToolOutput
        if server.use_structured_content and result.structuredContent:
            tool_output = json.dumps(result.structuredContent)
        else:
            tool_output_list: list[ToolOutputItem] = []
            for item in result.content:
                if item.type == "text":
                    tool_output_list.append(ToolOutputTextDict(type="text", text=item.text))
                elif item.type == "image":
                    tool_output_list.append(
                        ToolOutputImageDict(
                            type="image", image_url=f"data:{item.mimeType};base64,{item.data}"
                        )
                    )
                else:
                    # Fall back to regular text content
                    tool_output_list.append(
                        ToolOutputTextDict(type="text", text=str(item.model_dump(mode="json")))
                    )
            if len(tool_output_list) == 1:
                tool_output = tool_output_list[0]
            else:
                tool_output = tool_output_list

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

"""Select tools from an application-owned Classic MCP client for a native Session."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from mcp.types import CallToolResult, ImageContent, TextContent, Tool as MCPTool
from pydantic import RootModel, TypeAdapter

from _agents_codex._function_tools import FunctionTool, ToolContext
from _agents_codex._mcp_names import _project_tool_names
from _agents_codex.mcp import _launch_text
from _agents_codex.native_interception import Allow, Deny
from _agents_codex.tools import _CapturedFunction

from ..classic.agent import AgentBase
from ..classic.mcp._compat import (
    image_mime_type,
    result_is_error,
    result_structured_content,
    tool_input_schema,
)
from ..classic.mcp.server import _UNSET, MCPServer
from ..classic.run_context import RunContextWrapper
from ..tools import ToolOutput, ToolOutputImage, ToolOutputText, ToolRef

__all__ = ("MCPNamespace", "select_mcp_tools")

ContextT = TypeVar("ContextT")
_Arguments = RootModel[dict[str, Any]]
_Binder = Callable[[ContextT, ContextT, FunctionTool[Any, Any, Any]], _CapturedFunction]
_Approval = Callable[[ToolContext[ContextT]], Allow | Deny | Awaitable[Allow | Deny]]
_MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")
_MAX_DISCOVERED_TOOLS = 16_384
_MAX_DISCOVERED_NAME_BYTES = 4_096
_MAX_DISCOVERED_NAME_BUDGET = 1_048_576


@dataclass(frozen=True)
class MCPNamespace(Generic[ContextT]):
    """An inert namespace descriptor for one application-owned Classic MCP client.

    Use a separate client for each credential scope. The application owns the
    client, including opening and closing it. The optional authentication callback
    can prepare the connection when this namespace is selected.
    """

    name: str
    server: MCPServer = field(repr=False)
    description: str = ""
    authenticate: Callable[[ContextT], None | Awaitable[None]] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _MCP_NAME.fullmatch(self.name) is None:
            raise ValueError("MCP namespace name must use letters, digits, underscores or hyphens")
        if self.name in {"functions", "tool_search", "_agents_sdk"}:
            raise ValueError("MCP namespace name is reserved")
        _launch_text(self.description, "namespace description", allow_empty=True)
        if not isinstance(self.server, MCPServer):
            raise TypeError("MCP namespace requires a Classic MCPServer")
        if self.authenticate is not None and not callable(self.authenticate):
            raise TypeError("MCP authenticate must be callable")


@dataclass(frozen=True, kw_only=True)
class _CapturedMCPTool(_CapturedFunction):
    @property
    def _requires_live_manual_session(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True, init=False)
class _SelectedMCPTool(FunctionTool[_Arguments, ToolOutput, _Binder[ContextT]]):
    _schema_json: str = field(repr=False)
    _selected_context: ContextT = field(repr=False)

    def __init__(self, *, _schema_json: str, _selected_context: ContextT, **options: Any) -> None:
        object.__setattr__(self, "_schema_json", _schema_json)
        object.__setattr__(self, "_selected_context", _selected_context)
        super().__init__(**options)

    @property
    def _requires_live_manual_session(self) -> bool:
        return True

    def _spec_json(self) -> str:
        return json.dumps(
            {
                "type": "namespace",
                "name": self.namespace,
                "description": self.namespace_description or "",
                "tools": [
                    {
                        "type": "function",
                        "name": self.name,
                        "description": self.description,
                        "inputSchema": json.loads(self._schema_json),
                        "deferLoading": self.defer_loading,
                    }
                ],
            }
        )

    def _check_context(self, context: object) -> None:
        if context is not self._selected_context:
            raise ValueError(
                "Selected MCP tools require the application context used for discovery"
            )


async def select_mcp_tools(
    namespace: MCPNamespace[ContextT],
    *,
    context: ContextT,
    tool_names: Sequence[str] | None = None,
    filter_context: tuple[RunContextWrapper[ContextT], AgentBase[ContextT]] | None = None,
    approval: _Approval[ContextT] | None = None,
) -> tuple[FunctionTool[_Arguments, ToolOutput, _Binder[ContextT]], ...]:
    """Authenticate, discover, and capture tools from one selected namespace.

    Call after the application chooses a namespace and before opening a Session.
    Pass the returned tools and the same context object to the native Agent or
    Session. Omit tool_names to select every tool that passes the client's filter,
    or pass exact peer names to select a subset. An explicit empty list selects
    nothing. A requested name that is missing or filtered out fails selection.
    Discovery with a callable Classic filter requires its actual (run_context,
    agent) pair, with the same application context.

    Native projected names for model-facing tools; each returned tool's ``ref``
    for native selection. Original peer names for ``tool_names`` and borrowed
    dispatch. Namespace characters: ASCII letters, digits, underscores, or hyphens.
    The discovered catalog is limited to 16,384 tools, 4,096 UTF-8 bytes per tool
    name, and 1 MiB of tool-name bytes in total, before native name projection.

    Selection and native Session cleanup leave connection ownership with the
    caller. Keep the client's authentication scope stable while its bindings are
    in use. A fresh selection refreshes supported client caches for future
    bindings. Selection awaits authentication and discovery in the caller's task,
    so callbacks should propagate cancellation after their own cleanup.

    To restore an idle checkpoint, select tools from the current clients with
    the destination application's context. Supply bindings that match the saved
    namespace, tool name and schema. A Session with selected MCP tools in its
    catalog cannot suspend for portable manual restore. Reply in the live Session.

    The optional per-call approval receives the native ToolContext and returns
    Allow() or Deny(reason), directly or through an awaitable. Classic approval
    mappings apply only to selected names. Use Classic Runner for tools that
    require Classic approval or for servers with metadata and result hooks,
    error policies, or guardrails.
    """
    if isinstance(tool_names, (str, bytes)):
        raise TypeError("MCP tool_names must be a sequence of names")
    requested = None if tool_names is None else tuple(tool_names)
    if requested is not None:
        for name in requested:
            _launch_text(name, "tool name")
        if len(set(requested)) != len(requested):
            raise ValueError("MCP tool_names must be unique")
    if approval is not None and not callable(approval):
        raise TypeError("MCP approval must be callable")
    if filter_context is not None:
        run_context, agent = filter_context
        if (
            not isinstance(run_context, RunContextWrapper)
            or run_context.context is not context
            or not isinstance(agent, AgentBase)
        ):
            raise TypeError("MCP filter_context requires matching Classic run context and agent")
    if requested == ():
        return ()

    server = namespace.server
    classic_approval = server._needs_approval_policy

    def requires_classic_approval(names: tuple[str, ...]) -> bool:
        if isinstance(classic_approval, dict):
            return any(classic_approval.get(name, False) for name in names)
        return classic_approval is not False

    unsupported_policy = (
        "Selected MCP tools cannot use Classic execution policies; use Classic Runner"
    )
    if (
        requires_classic_approval(() if requested is None else requested)
        or server.tool_meta_resolver is not None
        or server.custom_data_extractor is not None
        or server.tool_input_guardrails
        or server.tool_output_guardrails
        or server._failure_error_function is not _UNSET
    ):
        raise ValueError(unsupported_policy)
    if filter_context is None and callable(getattr(server, "tool_filter", None)):
        raise TypeError("Callable MCP filters require filter_context")

    call_tool = server.call_tool
    use_structured_content = server.use_structured_content
    if namespace.authenticate is not None:
        authenticated = namespace.authenticate(context)
        if inspect.isawaitable(authenticated):
            await authenticated
    invalidate = getattr(server, "invalidate_tools_cache", None)
    if invalidate is not None:
        invalidated = invalidate()
        if inspect.isawaitable(invalidated):
            await invalidated
    discovered = (
        await server.list_tools()
        if filter_context is None
        else await server.list_tools(*filter_context)
    )
    if len(discovered) > _MAX_DISCOVERED_TOOLS:
        raise ValueError("MCP discovery exceeds the 16384-tool limit")
    by_name: dict[str, MCPTool] = {}
    name_budget = 0
    for tool in discovered:
        if not isinstance(tool.name, str) or not tool.name:
            raise ValueError("MCP discovery requires nonempty tool names")
        if len(tool.name) > _MAX_DISCOVERED_NAME_BYTES:
            raise ValueError("MCP discovery exceeds the 4096-byte tool name limit")
        name_bytes: int | None
        try:
            name_bytes = len(tool.name.encode("utf-8"))
        except UnicodeError:
            name_bytes = None
        if name_bytes is None:
            raise ValueError("MCP discovery tool names must contain valid Unicode")
        if name_bytes > _MAX_DISCOVERED_NAME_BYTES:
            raise ValueError("MCP discovery exceeds the 4096-byte tool name limit")
        name_budget += name_bytes
        if name_budget > _MAX_DISCOVERED_NAME_BUDGET:
            raise ValueError("MCP discovery exceeds the 1048576-byte name budget")
        if tool.name in by_name:
            raise ValueError("MCP discovery returned duplicate tool names")
        by_name[tool.name] = tool
    names = tuple(by_name) if requested is None else requested
    if requested is None and requires_classic_approval(names):
        raise ValueError(unsupported_policy)
    if any(name not in by_name for name in names):
        raise ValueError("Requested MCP tools were not discovered or were filtered out")

    projected = _project_tool_names([(namespace.name, tuple(by_name))], prefix=False)
    return tuple(
        _capture_tool(
            namespace,
            context,
            by_name[name],
            call_tool,
            use_structured_content,
            approval,
            ToolRef(**projected[namespace.name, name]),
        )
        for name in names
    )


def _capture_tool(
    namespace: MCPNamespace[ContextT],
    context: ContextT,
    tool: MCPTool,
    call_tool: Callable[[str, dict[str, Any]], Awaitable[CallToolResult]],
    use_structured_content: bool,
    approval: _Approval[ContextT] | None,
    model_ref: ToolRef,
) -> _SelectedMCPTool[ContextT]:
    raw_name = tool.name
    schema = tool_input_schema(tool)
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise ValueError("Selected MCP tools require an object input schema")
    if "type" not in schema:
        schema = {"properties": {}, **schema, "type": "object"}
    description = "" if tool.description is None else tool.description
    _launch_text(description, "tool description", allow_empty=True)
    schema_json = json.dumps(schema, allow_nan=False)
    output_adapter = TypeAdapter(ToolOutput)

    async def invoke(*, root: dict[str, Any]) -> ToolOutput:
        try:
            return _mcp_output(await call_tool(raw_name, root), use_structured_content)
        except Exception:
            # Peer errors and invalid results can contain private payloads.
            pass
        raise RuntimeError("Selected MCP tool invocation failed")

    def bind(
        session_context: ContextT,
        policy_context: ContextT,
        declaration: FunctionTool[Any, Any, Any],
    ) -> _CapturedFunction:
        assert session_context is policy_context
        declaration._check_context(session_context)
        if declaration.input_type is not _Arguments or declaration.output_type is not ToolOutput:
            raise TypeError("Selected MCP tool input and output types are fixed")
        ref = declaration.ref
        return _CapturedMCPTool(
            name=ref.name,
            spec_json=declaration._spec_json(),
            model=_Arguments,
            field_names=("root",),
            output_adapter=output_adapter,
            callback=invoke,
            context_parameter=None,
            callback_timeout=declaration.callback_timeout,
            timeout_behavior=declaration.timeout_behavior,
            timeout_error_function=declaration.timeout_error_function,
            error_function=declaration.error_function,
            _allow_run_error_formatter=bool(declaration._allow_run_error_formatter),
            before=declaration.before,
            after=declaration.after,
            context=session_context,
            namespace=ref.namespace,
        )

    return _SelectedMCPTool(
        name=model_ref.name,
        description=description,
        input_type=_Arguments,
        output_type=ToolOutput,
        namespace=model_ref.namespace,
        namespace_description=namespace.description,
        _schema_json=schema_json,
        _selected_context=context,
        _bind=bind,
        before=approval,
    )


def _mcp_output(result: CallToolResult, use_structured_content: bool) -> ToolOutput:
    success = not result_is_error(result)
    structured = result_structured_content(result)
    if success and use_structured_content and structured:
        return ToolOutput.text(json.dumps(structured, allow_nan=False))
    items: list[dict[str, Any] | ToolOutputText | ToolOutputImage] = []
    for block in result.content:
        if isinstance(block, TextContent):
            items.append(ToolOutputText(block.text))
        elif isinstance(block, ImageContent):
            items.append(ToolOutputImage(f"data:{image_mime_type(block)};base64,{block.data}"))
        else:
            # Keep fallback blocks as data without forwarding MCP metadata or resolving URLs.
            items.append(
                ToolOutputText(
                    block.model_dump_json(
                        by_alias=True, exclude={"meta": True, "resource": {"meta"}}
                    )
                )
            )
    return ToolOutput(items, success=success)

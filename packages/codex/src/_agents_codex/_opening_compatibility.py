"""Private compatibility checks for captured Session opening declarations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ._function_tools import FunctionTool, Tool
from .canonical_storage import CustomStorage
from .mcp import _MCP
from .native_agents import NativeAgents
from .state import LocalStorage
from .tools import WebSearch


def _validate_custom_storage_capabilities(
    storage: LocalStorage | CustomStorage | None,
    *,
    native_agents: NativeAgents[Any] | None,
    goals: bool,
    programmatic_tool_calling: bool,
    checklist: bool,
    mcp_tools: Sequence[_MCP[Any]],
    has_environment: bool,
    available: Sequence[Tool[Any]],
    tools: Sequence[FunctionTool[Any, Any, Any]],
) -> None:
    if isinstance(storage, CustomStorage) and (
        native_agents is not None
        or goals
        or programmatic_tool_calling
        or checklist
        or mcp_tools
        or has_environment
        or any(isinstance(tool, WebSearch) and tool.route != "hosted" for tool in available)
        or any(tool._bind is None or tool.needs_approval is not False for tool in tools)
    ):
        raise ValueError(
            "CustomStorage preview does not support children, goals, checklist, "
            "code mode, manual tools, standalone native tools or environments"
        )

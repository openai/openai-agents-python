from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .manager import MCPServerManager
    from .server import (
        LocalMCPApprovalCallable,
        MCPServer,
        MCPServerSse,
        MCPServerSseParams,
        MCPServerStdio,
        MCPServerStdioParams,
        MCPServerStreamableHttp,
        MCPServerStreamableHttpParams,
    )

from .util import (
    MCPToolMetaContext,
    MCPToolMetaResolver,
    MCPUtil,
    ToolFilter,
    ToolFilterCallable,
    ToolFilterContext,
    ToolFilterStatic,
    create_static_tool_filter,
)

_LAZY_EXPORTS = {
    "MCPServer": ".server",
    "MCPServerSse": ".server",
    "MCPServerSseParams": ".server",
    "MCPServerStdio": ".server",
    "MCPServerStdioParams": ".server",
    "MCPServerStreamableHttp": ".server",
    "MCPServerStreamableHttpParams": ".server",
    "MCPServerManager": ".manager",
    "LocalMCPApprovalCallable": ".server",
}

__all__ = [
    "MCPServer",
    "MCPServerSse",
    "MCPServerSseParams",
    "MCPServerStdio",
    "MCPServerStdioParams",
    "MCPServerStreamableHttp",
    "MCPServerStreamableHttpParams",
    "MCPServerManager",
    "LocalMCPApprovalCallable",
    "MCPUtil",
    "MCPToolMetaContext",
    "MCPToolMetaResolver",
    "ToolFilter",
    "ToolFilterCallable",
    "ToolFilterContext",
    "ToolFilterStatic",
    "create_static_tool_filter",
]


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name = _LAZY_EXPORTS[name]
    try:
        module = import_module(module_name, __name__)
    except ImportError as exc:
        raise ImportError(
            f"Failed to import {name} from agents.mcp. "
            f"The agents.mcp{module_name} module could not be imported; "
            "see the chained ImportError for details."
        ) from exc

    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

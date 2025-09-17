"""Model Context Protocol (MCP) for OpenAI Agents SDK.

Provides server implementations and utilities for Model Context Protocol,
enabling standardized communication between agents and external tools.
"""

try:
    from .server import (
        MCPServer,
        MCPServerSse,
        MCPServerSseParams,
        MCPServerStdio,
        MCPServerStdioParams,
        MCPServerStreamableHttp,
        MCPServerStreamableHttpParams,
    )
except ImportError:
    pass

from .util import (
    MCPUtil,
    ToolFilter,
    ToolFilterCallable,
    ToolFilterContext,
    ToolFilterStatic,
    create_static_tool_filter,
)

__all__ = [
    "MCPServer",
    "MCPServerSse",
    "MCPServerSseParams",
    "MCPServerStdio",
    "MCPServerStdioParams",
    "MCPServerStreamableHttp",
    "MCPServerStreamableHttpParams",
    "MCPUtil",
    "ToolFilter",
    "ToolFilterCallable",
    "ToolFilterContext",
    "ToolFilterStatic",
    "create_static_tool_filter",
]

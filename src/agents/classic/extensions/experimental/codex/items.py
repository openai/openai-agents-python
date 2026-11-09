"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from agents._codex_cli import items as _implementation
from agents._codex_cli.items import (
    AgentMessageItem as AgentMessageItem,
    CommandExecutionItem as CommandExecutionItem,
    CommandExecutionStatus as CommandExecutionStatus,
    ErrorItem as ErrorItem,
    FileChangeItem as FileChangeItem,
    FileUpdateChange as FileUpdateChange,
    McpToolCallError as McpToolCallError,
    McpToolCallItem as McpToolCallItem,
    McpToolCallResult as McpToolCallResult,
    McpToolCallStatus as McpToolCallStatus,
    PatchApplyStatus as PatchApplyStatus,
    PatchChangeKind as PatchChangeKind,
    ReasoningItem as ReasoningItem,
    ThreadItem as ThreadItem,
    TodoItem as TodoItem,
    TodoListItem as TodoListItem,
    WebSearchItem as WebSearchItem,
    _coerce_file_update_change as _coerce_file_update_change,
    _coerce_mcp_tool_call_error as _coerce_mcp_tool_call_error,
    _coerce_mcp_tool_call_result as _coerce_mcp_tool_call_result,
    _UnknownThreadItem as _UnknownThreadItem,
    coerce_thread_item as coerce_thread_item,
    is_agent_message_item as is_agent_message_item,
)

_sys.modules[__name__] = _implementation

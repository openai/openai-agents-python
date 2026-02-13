from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Union, cast

from typing_extensions import Literal, TypeAlias, TypeGuard

from .payloads import _DictLike

# Item payloads are emitted inside item.* events from the Codex CLI JSONL stream.

if TYPE_CHECKING:
    from mcp.types import ContentBlock as McpContentBlock
else:
    McpContentBlock = Any  # type: ignore[assignment]

CommandExecutionStatus = Literal["in_progress", "completed", "failed"]
PatchChangeKind = Literal["add", "delete", "update"]
PatchApplyStatus = Literal["completed", "failed"]
McpToolCallStatus = Literal["in_progress", "completed", "failed"]


@dataclass(frozen=True)
class CommandExecutionItem(_DictLike):
    id: str
    command: str
    status: CommandExecutionStatus
    aggregated_output: str = ""
    exit_code: int | None = None
    type: Literal["command_execution"] = field(default="command_execution", init=False)


@dataclass(frozen=True)
class FileUpdateChange(_DictLike):
    path: str
    kind: PatchChangeKind


@dataclass(frozen=True)
class FileChangeItem(_DictLike):
    id: str
    changes: list[FileUpdateChange]
    status: PatchApplyStatus
    type: Literal["file_change"] = field(default="file_change", init=False)


@dataclass(frozen=True)
class McpToolCallResult(_DictLike):
    content: list[McpContentBlock]
    structured_content: Any


@dataclass(frozen=True)
class McpToolCallError(_DictLike):
    message: str


@dataclass(frozen=True)
class McpToolCallItem(_DictLike):
    id: str
    server: str
    tool: str
    arguments: Any
    status: McpToolCallStatus
    result: McpToolCallResult | None = None
    error: McpToolCallError | None = None
    type: Literal["mcp_tool_call"] = field(default="mcp_tool_call", init=False)


@dataclass(frozen=True)
class AgentMessageItem(_DictLike):
    id: str
    text: str
    type: Literal["agent_message"] = field(default="agent_message", init=False)


@dataclass(frozen=True)
class ReasoningItem(_DictLike):
    id: str
    text: str
    type: Literal["reasoning"] = field(default="reasoning", init=False)


@dataclass(frozen=True)
class WebSearchItem(_DictLike):
    id: str
    query: str
    type: Literal["web_search"] = field(default="web_search", init=False)


@dataclass(frozen=True)
class ErrorItem(_DictLike):
    id: str
    message: str
    type: Literal["error"] = field(default="error", init=False)


@dataclass(frozen=True)
class TodoItem(_DictLike):
    text: str
    completed: bool


@dataclass(frozen=True)
class TodoListItem(_DictLike):
    id: str
    items: list[TodoItem]
    type: Literal["todo_list"] = field(default="todo_list", init=False)


@dataclass(frozen=True)
class _UnknownThreadItem(_DictLike):
    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: str | None = None


ThreadItem: TypeAlias = Union[
    AgentMessageItem,
    ReasoningItem,
    CommandExecutionItem,
    FileChangeItem,
    McpToolCallItem,
    WebSearchItem,
    TodoListItem,
    ErrorItem,
    _UnknownThreadItem,
]


def is_agent_message_item(item: ThreadItem) -> TypeGuard[AgentMessageItem]:
    return isinstance(item, AgentMessageItem)


def _coerce_file_update_change(
    raw: FileUpdateChange | Mapping[str, Any],
) -> FileUpdateChange:
    if isinstance(raw, FileUpdateChange):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("FileUpdateChange must be a mapping.")
    return FileUpdateChange(
        path=cast(str, raw["path"]),
        kind=cast(PatchChangeKind, raw["kind"]),
    )


def _coerce_mcp_tool_call_result(
    raw: McpToolCallResult | Mapping[str, Any],
) -> McpToolCallResult:
    if isinstance(raw, McpToolCallResult):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("McpToolCallResult must be a mapping.")
    content = cast(list[McpContentBlock], raw.get("content", []))
    return McpToolCallResult(
        content=content,
        structured_content=raw.get("structured_content"),
    )


def _coerce_mcp_tool_call_error(
    raw: McpToolCallError | Mapping[str, Any],
) -> McpToolCallError:
    if isinstance(raw, McpToolCallError):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("McpToolCallError must be a mapping.")
    return McpToolCallError(message=cast(str, raw.get("message", "")))


def coerce_thread_item(raw: ThreadItem | Mapping[str, Any]) -> ThreadItem:
    if isinstance(raw, _DictLike):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("Thread item payload must be a mapping.")

    item_type = raw.get("type")
    if item_type == "command_execution":
        return CommandExecutionItem(
            id=cast(str, raw["id"]),
            command=cast(str, raw["command"]),
            aggregated_output=cast(str, raw.get("aggregated_output", "")),
            status=cast(CommandExecutionStatus, raw["status"]),
            exit_code=cast(Optional[int], raw.get("exit_code")),
        )
    if item_type == "file_change":
        changes = [_coerce_file_update_change(change) for change in raw.get("changes", [])]
        return FileChangeItem(
            id=cast(str, raw["id"]),
            changes=changes,
            status=cast(PatchApplyStatus, raw["status"]),
        )
    if item_type == "mcp_tool_call":
        result_raw = raw.get("result")
        error_raw = raw.get("error")
        result = None
        error = None
        if result_raw is not None:
            result = _coerce_mcp_tool_call_result(cast(Mapping[str, Any], result_raw))
        if error_raw is not None:
            error = _coerce_mcp_tool_call_error(cast(Mapping[str, Any], error_raw))
        return McpToolCallItem(
            id=cast(str, raw["id"]),
            server=cast(str, raw["server"]),
            tool=cast(str, raw["tool"]),
            arguments=raw.get("arguments"),
            status=cast(McpToolCallStatus, raw["status"]),
            result=result,
            error=error,
        )
    if item_type == "agent_message":
        return AgentMessageItem(
            id=cast(str, raw["id"]),
            text=cast(str, raw.get("text", "")),
        )
    if item_type == "reasoning":
        return ReasoningItem(
            id=cast(str, raw["id"]),
            text=cast(str, raw.get("text", "")),
        )
    if item_type == "web_search":
        return WebSearchItem(
            id=cast(str, raw["id"]),
            query=cast(str, raw.get("query", "")),
        )
    if item_type == "todo_list":
        items_raw = raw.get("items", [])
        items = [
            TodoItem(text=cast(str, item.get("text", "")), completed=bool(item.get("completed")))
            for item in cast(list[Mapping[str, Any]], items_raw)
        ]
        return TodoListItem(id=cast(str, raw["id"]), items=items)
    if item_type == "error":
        return ErrorItem(
            id=cast(str, raw.get("id", "")),
            message=cast(str, raw.get("message", "")),
        )

    return _UnknownThreadItem(
        type=cast(str, item_type) if item_type is not None else "unknown",
        payload=dict(raw),
        id=cast(Optional[str], raw.get("id")),
    )

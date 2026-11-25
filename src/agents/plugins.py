"""Named contributions to an Agent. Loading a package alone never activates it."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Literal, TypeAlias, TypeVar

from .exceptions import UserError
from .run_context import TContext

if TYPE_CHECKING:
    from .agent import Agent
    from .plugin_http import HTTPHooks
    from .run_config import ModelInputData
    from .run_context import RunContextWrapper
    from .stream_events import ApprovedText, RunCompleted
    from .tool import FunctionTool

_Scalar: TypeAlias = str | int | float | bool | None
_Frozen: TypeAlias = _Scalar | Mapping[str, "_Frozen"] | tuple["_Frozen", ...]
_Value: TypeAlias = _Scalar | Mapping[str, "_Value"] | tuple["_Value", ...] | list["_Value"]


_EventKind: TypeAlias = Literal[
    "agent_start", "agent_end", "model_start", "model_end", "tool_start", "tool_end", "handoff"
]


@dataclass(frozen=True)
class AgentEvent:
    """An SDK lifecycle phase with names from application definitions only."""

    kind: _EventKind
    agent: str
    tool: str | None = None
    namespace: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    """One submission's final status, with no input, output, credentials or error payload."""

    status: Literal["completed", "paused", "failed", "cancelled"]
    elapsed_seconds: float


@dataclass(frozen=True)
class ToolCall:
    """The target and arguments. Only the policy receives application-supplied fields."""

    name: str
    namespace: str | None
    owner: str | None
    call_id: str
    arguments: Mapping[str, _Frozen]
    context: Mapping[str, _Frozen]


@dataclass(frozen=True)
class ToolProgress:
    submission_id: str
    owner: str | None
    call_id: str
    sequence: int
    dropped: int
    message: str


@dataclass(frozen=True)
class ReplaceArguments:
    arguments: Mapping[str, _Value]


@dataclass(frozen=True)
class RequireApproval:
    """Require an explicit decision for this call."""


@dataclass(frozen=True)
class Deny:
    """Refuse a call. The reason is private to the application."""

    reason: str


@dataclass(frozen=True)
class ToolResult:
    """A text result before the SDK's output guardrails."""

    call: ToolCall
    text: str


_E = TypeVar("_E")
_R = TypeVar("_R")
_Handler: TypeAlias = Callable[[_E], _R | Awaitable[_R]]


@dataclass(frozen=True, kw_only=True)
class PluginHooks:
    filter_input: _Handler[ModelInputData, ModelInputData] | None = None
    """Prune user messages or replace instructions before the app's model input filter."""

    prepare_tool: _Handler[ToolCall, ReplaceArguments | None] | None = None
    check_tool: _Handler[ToolCall, Deny | RequireApproval | None] | None = None
    filter_result: _Handler[ToolResult, str | None] | None = None
    on_tool_progress: _Handler[ToolProgress, None] | None = None
    on_preview: _Handler[ApprovedText, None] | None = None
    on_stream_completed: _Handler[RunCompleted, None] | None = None
    on_event: _Handler[AgentEvent, None] | None = None
    check_own_tool: _Handler[ToolCall, Deny | RequireApproval | None] | None = None
    """Check only this plugin's function tools. Agent-wide checks still apply."""
    on_run_end: _Handler[RunOutcome, None] | None = None
    """Observe the starting agent's submission after settlement. Drain streams to receive it."""


@dataclass(frozen=True, kw_only=True)
class Plugin(Generic[TContext]):
    """Reusable tools, guidance, hooks and optional local commands for one agent."""

    name: str
    tools: Sequence[FunctionTool] = ()
    instructions: (
        str | Callable[[RunContextWrapper[TContext], Agent[TContext]], str | Awaitable[str]] | None
    ) = None
    hooks: PluginHooks | None = None
    commands: Mapping[str, Callable[[TContext, str], str | Awaitable[str]]] = field(
        default_factory=dict
    )
    http: HTTPHooks | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", k)
            for k in (self.name, *self.commands)
        ):
            raise UserError("Invalid plugin or command name.")
        if any(not callable(handler) for handler in self.commands.values()):
            raise UserError("Invalid plugin command handler.")
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))

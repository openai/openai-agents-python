"""Run-wide formatting for admitted native function failures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass as dataclass, field
from typing import Any, Generic, Literal, TypeAlias, TypeVar

from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._public import preserve_module_identity as _preserve_module_identity
from .tools import ToolCallInfo

ContextT = TypeVar("ContextT")


@_sdk_dataclass(frozen=True, kw_only=True)
class ToolErrorContext(Generic[ContextT]):
    """A recoverable function failure. Return only application-approved model text.

    Agent.session(), Agent.run(), and Agent.run_sync() have been accepting
    tool_error_formatter callbacks with this context. The callback has been
    receiving original argument or body failures, plus rejected unknown
    direct functions when the turn permits a recovery reply. The original
    error and application context stay local.

    A returned string, including an empty string, has been supplying a failed
    non-terminal tool reply. None, another return type, or a formatter exception
    during active admission has been selecting default_message. Per-tool input
    and body handlers have been retaining precedence for their own phases;
    Declaring a tool with explicit error_function=None has been suppressing
    run-wide recovery. dataclasses.replace() has been carrying that original
    inheritance choice, including same-value error_function=None copies.

    A default managed function denial has been receiving reason="approval_rejected"
    and a local PermissionError. An explicit denial reason has been bypassing
    this formatter, including a copy of the default message.

    Async formatters have been receiving asyncio cancellation directly. The
    owner has been joining cleanup and discarding late replies, with cleanup
    errors retaining their original cause. Timeouts, rewritten input, strict
    unknown-tool rejection, and native MCP and executor failures keep their
    own behavior.
    """

    context: ContextT = field(repr=False)
    call: ToolCallInfo
    phase: Literal["arguments", "execution"]
    default_message: str
    error: Exception = field(repr=False)
    reason: Literal["tool_not_found", "approval_rejected"] | None = None


ToolErrorFormatter: TypeAlias = Callable[
    [ToolErrorContext[Any]], str | None | Awaitable[str | None]
]

_preserve_module_identity(globals(), "agents._tool_errors")

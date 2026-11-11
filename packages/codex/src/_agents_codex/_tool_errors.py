"""Run-wide formatting for admitted native function failures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeAlias, TypeVar

from ._public import preserve_module_identity as _preserve_module_identity
from .tools import ToolCallInfo

ContextT = TypeVar("ContextT")


@dataclass(frozen=True, kw_only=True)
class ToolErrorContext(Generic[ContextT]):
    """A recoverable function failure. Return only application-approved model text.

    The error and application context remain local to the callback. Formatting
    never authorizes a retired call or changes callback timeout policy.
    """

    context: ContextT = field(repr=False)
    call: ToolCallInfo
    phase: Literal["arguments", "execution"]
    default_message: str
    error: Exception = field(repr=False)


ToolErrorFormatter: TypeAlias = Callable[
    [ToolErrorContext[Any]], str | None | Awaitable[str | None]
]

_preserve_module_identity(globals(), "agents._tool_errors")

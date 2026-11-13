"""Typed transfer callbacks executed by the native tool owner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._function_tools import FunctionTool, _PythonTool
from ._public import preserve_module_identity

if TYPE_CHECKING:
    from .agent import Agent

ContextT = TypeVar("ContextT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class HandoffInput(Generic[ContextT]):
    """Detached application presentation at the accepted native transfer boundary.

    input has been retaining the operation's initial input; previous has been
    retaining earlier generated items; generated has been including the settled
    current response and sibling outputs. context has been retaining the current
    application's context. Returning groups has not been changing canonical
    effects, protected instructions, tools or destination identity.
    """

    input: tuple[dict[str, Any], ...]
    previous: tuple[dict[str, Any], ...]
    generated: tuple[dict[str, Any], ...]
    context: ContextT | None = field(default=None, repr=False, compare=False)
    target: Agent[ContextT, Any] = field(kw_only=True, repr=False, compare=False)


_PrepareHandoffInput = Callable[
    [HandoffInput[ContextT]], HandoffInput[ContextT] | Awaitable[HandoffInput[ContextT]]
]


@dataclass(frozen=True)
class Handoff(Generic[ContextT, OutputT]):
    """Callback-backed native transfer to a captured Agent.

    The callback has been supplying the name, description, input schema and
    acknowledgement. The existing tool owner has been validating arguments and
    awaiting callback cleanup before publishing an accepted target. Failed or
    cancelled callbacks have not been activating the target.

    Handoff callbacks must allow direct model calls and use the default
    function namespace. The target Agent owns the final output.
    """

    target: Agent[ContextT, OutputT]
    callback: _PythonTool[ContextT, ContextT]

    def __post_init__(self) -> None:
        from .agent import Agent

        if not isinstance(self.target, Agent):
            raise TypeError("A handoff target must be a native Agent")
        callback = self.callback
        if not isinstance(callback, FunctionTool) or callback._bind is None:
            raise TypeError("A handoff requires a registered native function callback")
        if callback.needs_approval is not False:
            raise ValueError("A handoff callback must run without a managed approval")
        if callback.finish_turn:
            raise ValueError("A handoff callback cannot finish the turn")
        if callback.namespace not in (None, "functions"):
            raise ValueError("A handoff callback must use the default function namespace")


preserve_module_identity(globals(), "agents.handoffs")

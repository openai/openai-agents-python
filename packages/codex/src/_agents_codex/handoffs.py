"""Typed transfer callbacks executed by the native tool owner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass as dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._function_tools import FunctionTool, _PythonTool
from ._public import preserve_module_identity

if TYPE_CHECKING:
    from .agent import Agent

ContextT = TypeVar("ContextT")
OutputT = TypeVar("OutputT")


@_sdk_dataclass(frozen=True)
class HandoffInput(Generic[ContextT]):
    """Detached application presentation at the accepted native transfer boundary.

    input contains the initial input. previous contains earlier generated
    items, and generated contains the current settled response and sibling
    outputs. context carries the application's current context.

    Return presentation groups to change what the destination sees. Use
    recorded_generated to select which original generated records stay in
    canonical history. You can omit closed assistant text or an ordinary
    settled function pair from the current response. Omitting either part
    removes both from canonical history. Retain the transfer receipt and
    authority-bearing records. Keep selected objects
    unchanged and in order. To append plain assistant text, include the
    same new message object in generated and at the end of recorded_generated.
    Supply only the assistant role, message type, and nonempty output text.
    Core assigns the message ID. Leave recorded_generated unset to keep the
    original canonical history.
    Earlier audit records can still appear in exported SessionState.
    """

    input: tuple[dict[str, Any], ...]
    previous: tuple[dict[str, Any], ...]
    generated: tuple[dict[str, Any], ...]
    context: ContextT | None = field(default=None, repr=False, compare=False)
    target: Agent[ContextT, Any] = field(kw_only=True, repr=False, compare=False)
    # Retained source objects followed by explicit application text additions.
    recorded_generated: tuple[dict[str, Any], ...] | None = field(default=None, kw_only=True)


_PrepareHandoffInput = Callable[
    [HandoffInput[ContextT]], HandoffInput[ContextT] | Awaitable[HandoffInput[ContextT]]
]


@_sdk_dataclass(frozen=True)
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


def remove_all_tools(value: HandoffInput[ContextT]) -> HandoffInput[ContextT]:
    """Omit tool calls, results, and reasoning from the destination's input.

    Canonical history and recorded_generated stay unchanged. Apply this filter
    before nesting history if the summary must exclude tool data.
    """
    from .handoff_history import _remove_all_tools

    return _remove_all_tools(value)


def nest_handoff_history(
    value: HandoffInput[ContextT],
    *,
    history_mapper: Callable[[list[dict[str, Any]]], Sequence[dict[str, Any]]] | None = None,
    history_wrappers: tuple[str, str] = (
        "<CONVERSATION HISTORY>",
        "</CONVERSATION HISTORY>",
    ),
) -> HandoffInput[ContextT]:
    """Summarize model presentation while preserving canonical source history.

    Default summaries have been flattening before the next summary. Current
    messages have been retaining their order between summary segments. A custom
    mapper receives copied source items, expanding earlier summaries generated
    by this helper and retained by the native session. Its return value supplies
    native input dictionaries.
    Tool arguments and outputs have been remaining in the summary; callers have
    been owning redaction. Target, context and recorded_generated have been
    retaining their original identities.
    """
    from .handoff_history import _nest_handoff_history

    return _nest_handoff_history(
        value, history_mapper=history_mapper, history_wrappers=history_wrappers
    )


preserve_module_identity(globals(), "agents.handoffs")

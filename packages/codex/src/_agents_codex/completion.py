"""Application acceptance of a native turn's proposed completion."""

from collections.abc import Awaitable as Awaitable, Callable as Callable
from dataclasses import dataclass as dataclass, field as field
from typing import Any as Any, Generic as Generic, TypeAlias as TypeAlias, TypeVar as TypeVar

from ._public import preserve_module_identity as _preserve_module_identity
from .native_interception import StopTurn as StopTurn
from .tools import ToolCallInfo as ToolCallInfo

ContextT = TypeVar("ContextT")


@dataclass(frozen=True)
class ContinueTurn:
    """Reject completion and continue the same turn with these instructions.

    Instructions must be nonempty. They become conversation data visible to
    the model and saved history. This does not change tools or permissions.
    Bound continuation with ``RunBudget`` or return ``StopTurn`` from the hook.
    """

    instructions: str = field(repr=False)


@dataclass(frozen=True)
class CompletionContext(Generic[ContextT]):
    """Proposed native completion for an awaited ``before_complete`` callback.

    Return ``None`` to accept, ``ContinueTurn`` to supply feedback and continue,
    or ``StopTurn`` to abort by policy. The callback runs after a successful
    model step that would end the turn, before the turn or native child is
    reported complete. It does not run for provider errors, cancellation,
    compaction, or the internal prompted-output repair agent.

    ``text`` is the proposed assistant text, before output-type conversion;
    it is ``None`` when the model produced no final text. Already streamed text
    and recorded history are not retracted when completion is rejected.
    ``continuation_count`` counts prior requests to continue in the
    current native turn loop. Restoring a checkpoint starts a new count and uses
    current callbacks and application context.

    Native children share their parent Python Session's ``session_id`` and
    have their own ``turn_id`` and ``subagent_id``. Async callbacks run on the
    Session event loop; sync callbacks run in a worker. Cancellation joins
    admitted work and discards late results. Raising fails the turn. A callback
    must not mutate its owning Session. Root ``RunBudget`` limits also apply
    to continuation requests; use the count and ``StopTurn`` for child limits.
    """

    context: ContextT = field(repr=False)
    text: str | None = field(repr=False)
    session_id: str
    turn_id: str
    subagent_id: str | None
    model: str
    continuation_count: int
    tool_call: ToolCallInfo | None = field(default=None, kw_only=True)
    final_output: Any = field(default=None, kw_only=True, repr=False)


_BeforeComplete: TypeAlias = Callable[
    [CompletionContext[ContextT]],
    ContinueTurn | StopTurn | None | Awaitable[ContinueTurn | StopTurn | None],
]


_preserve_module_identity(globals(), "agents.completion")

"""Application acceptance of a native turn's proposed completion."""

from __future__ import annotations

from collections.abc import Awaitable as Awaitable, Callable as Callable, Sequence as Sequence
from dataclasses import dataclass as dataclass, field as field
from typing import (
    TYPE_CHECKING,
    Any,
    Generic as Generic,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
    overload,
)

if TYPE_CHECKING:
    from .decisions import DecisionInput, DecisionProvider

from ._public import preserve_module_identity as _preserve_module_identity
from .native_interception import StopTurn as StopTurn
from .tools import ToolCallInfo as ToolCallInfo

ContextT = TypeVar("ContextT")
_DecisionT = TypeVar("_DecisionT")


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
    compaction, or the internal prompted-output repair agent. Repaired text
    passes through this same original callback before output-type conversion;
    ContinueTurn resumes the original turn, not the formatting agent.

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

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: None = None,
        output_type: None = None,
    ) -> bool: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[_DecisionT],
        output_type: None = None,
    ) -> _DecisionT: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: type[_DecisionT],
        choices: None = None,
    ) -> _DecisionT: ...

    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[Any] | None = None,
        output_type: Any = None,
    ) -> Any:
        """Answer a typed question during this exact completion callback.

        Input is explicit. The callback's captured provider is the default;
        provider overrides it for this call. Completed usage is recorded before
        local validation. Calls outside the callback or from detached tasks fail.
        """
        from .decisions import _bound_decide

        return await _bound_decide(
            self, question, input=input, provider=provider, choices=choices, output_type=output_type
        )


_BeforeComplete: TypeAlias = Callable[
    [CompletionContext[ContextT]],
    ContinueTurn | StopTurn | None | Awaitable[ContinueTurn | StopTurn | None],
]


_preserve_module_identity(globals(), "agents.completion")

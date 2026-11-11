"""Live references to exact turns, shared by root sessions and subagents."""

from __future__ import annotations

import asyncio as asyncio
from collections.abc import Iterator as Iterator, Sequence as Sequence
from dataclasses import dataclass as dataclass, field as field
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    Protocol as Protocol,
    cast as cast,
    overload as overload,
)

from typing_extensions import TypeVar as TypeVar

from ._mcp_errors import _protect_mcp_methods, _strip_mcp_frames, _uses_mcp
from ._public import preserve_module_identity as _preserve_module_identity
from .usage import RunUsage, _UsageCollector

if TYPE_CHECKING:
    from ._completed_turn_owner import _CompletedTurnOwner
    from .decisions import DecisionInput, DecisionProvider
    from .input import UserInput
    from .manual import Interaction
    from .observation import EventStream
    from .rate_limits import ApiRateLimits
    from .session import Completed, NativeError, Session, TurnStoppedError
    from .state import ApplicationToolOutput

OutputT = TypeVar("OutputT")
_DecisionT = TypeVar("_DecisionT")
TurnT = TypeVar("TurnT", default="Turn[OutputT]")


@dataclass(frozen=True)
class PendingRequests(Generic[OutputT, TurnT]):
    """Current requests of one Turn. Another consumer may answer first."""

    turn: TurnT
    requests: tuple[Interaction, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SteeringReceipt:
    """Native acceptance of one input for an exact root turn.

    Acceptance does not establish that the model sampled the input.
    """

    session_id: str
    turn_id: str
    submission_id: str
    input_id: str


@dataclass(frozen=True, slots=True)
class TurnNeedsAttention:
    """A settled turn and the latest Session-level API limits at read time."""

    reason: Literal[
        "interrupted", "connection_lost", "quota_blocked", "rate_limited", "budget_exceeded"
    ]
    error: NativeError | TurnStoppedError
    rate_limits: ApiRateLimits


@dataclass(frozen=True, slots=True, init=False)
class Turn(Generic[OutputT]):
    """One submitted root task, or a separately scoped native child turn.

    A root has no native ID until actual admission. It then retains that first
    ID through accepted steering continuations. Reading the result is passive,
    even after later work starts or the Session closes. Turns cannot be restored from IDs.
    """

    session_id: str
    turn_id: str | None = field(compare=False)
    submission_id: str
    child_id: str | None
    instance_id: int | None
    _session: Session[Any, Any] | _CompletedTurnOwner = field(repr=False, compare=False)
    _owner: object = field(repr=False, compare=False)
    _run_usage: _UsageCollector = field(repr=False, compare=False)
    _completion: asyncio.Future[Any] | None = field(repr=False, compare=False)
    _continuation: asyncio.Future[Turn[OutputT]] | None = field(repr=False, compare=False)
    _result: asyncio.Task[Completed[OutputT]] | None = field(repr=False, compare=False)
    _native: Any = field(repr=False, compare=False)
    _observation_after: int = field(repr=False, compare=False)
    _guardrail_results: list[Any] = field(repr=False, compare=False)

    @property
    def application_tool_outputs(self) -> tuple[ApplicationToolOutput, ...]:
        """Detached application data from accepted tool results on this turn."""
        return tuple(
            record.model_copy(deep=True)
            for record in self._session._application_tool_outputs
            if record.submission_id == self.submission_id
        )

    @property
    def guardrail_results(self) -> tuple[Any, ...]:
        """Completed guardrail diagnostics on this exact owning run."""
        if self.child_id is not None and hasattr(self._session, "_child_callbacks"):
            session = cast("Session[Any, Any]", self._session)
            turn_id = self.turn_id
            state = (
                None
                if turn_id is None
                else session._child_callbacks.get(
                    (self.child_id, self._native.incarnation, turn_id)
                )
            )
            if state is not None:
                object.__setattr__(self, "_guardrail_results", state.guardrail_results)
        return tuple(self._guardrail_results)

    @property
    def run_usage(self) -> RunUsage:
        """Detached measured usage for this logical run, excluding unrelated children."""
        if self.child_id is not None and hasattr(self._session, "_child_callbacks"):
            session = cast("Session[Any, Any]", self._session)
            turn_id = self.turn_id
            state = (
                None
                if turn_id is None
                else session._child_callbacks.get(
                    (self.child_id, self._native.incarnation, turn_id)
                )
            )
            if state is not None:
                object.__setattr__(self, "_run_usage", state.run_usage)
        return self._run_usage.snapshot()

    def __new__(cls, *args: Any, **kwargs: Any) -> Turn[Any]:
        raise TypeError("Turns are issued by a Session or Subagent")

    def __reduce__(self) -> Any:
        raise TypeError("Turn belongs to a live Session and cannot be serialized")

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

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: Any,
        choices: None = None,
    ) -> Any: ...

    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[Any] | None = None,
        output_type: Any = None,
    ) -> Any:
        """Answer a typed question during this exact Turn's admitted callback.

        Input is explicit. The callback's captured provider is the default;
        provider overrides it for this call. Completed usage is recorded before
        local validation. For overlapping decisions, await ``asyncio.gather()`` in this callback.
        """
        from .decisions import _bound_decide

        return await _bound_decide(
            self, question, input=input, provider=provider, choices=choices, output_type=output_type
        )

    async def get_result(self) -> Completed[OutputT]:
        """Wait for all this root's work without cancelling it if a waiter stops."""
        return await self._session._wait(self)

    async def wait_for_request_or_result(self) -> Completed[OutputT] | PendingRequests[OutputT]:
        """Wait for this turn's result or its own live, answerable requests.

        Use Session.interactions() to also receive requests from other turns.
        Like get_result(), cancelling this waiter leaves the Turn alive. A root
        includes its owned continuation requests, never descendant requests.
        A child includes only requests owned by that exact child turn.
        """
        return await self._session._wait_for_request_or_result(self)

    async def wait_for_attention(
        self,
    ) -> Completed[OutputT] | PendingRequests[OutputT] | TurnNeedsAttention:
        """Wait for a result, an answerable request, or a known supervisory stop.

        Other failures still raise. Use an asyncio timeout for a caller deadline.
        Timing out cancels only this wait. A connection failure can leave earlier
        tool effects uncertain, so reconcile them before attempting new work.
        """
        from .session import NativeError, TurnStoppedError

        reason: Literal[
            "interrupted", "connection_lost", "quota_blocked", "rate_limited", "budget_exceeded"
        ]
        try:
            return await self.wait_for_request_or_result()
        except TurnStoppedError as error:
            if _uses_mcp(
                (self,),
                {},
                lambda owner: (owner._session._opening_agent, *owner._session._opening_agent.tools),
            ):
                _strip_mcp_frames(error)
            return TurnNeedsAttention("interrupted", error, self._session.rate_limits())
        except NativeError as error:
            if error.code in {"run_budget_exceeded", "run_budget_usage_unavailable"}:
                reason = "budget_exceeded"
            elif error._interrupted:
                reason = "interrupted"
            elif error.category == "connection_failed":
                reason = "connection_lost"
            elif error.category == "rate_limit_exceeded":
                reason = "rate_limited"
            elif error.category in {
                "session_budget_exceeded",
                "usage_limit_exceeded",
                "credit_balance_exhausted",
                "project_spend_limit_exceeded",
                "organization_spend_limit_exceeded",
                "organization_usage_limit_exceeded",
                "billing_not_active",
            }:
                reason = "quota_blocked"
            else:
                raise
            if _uses_mcp(
                (self,),
                {},
                lambda owner: (owner._session._opening_agent, *owner._session._opening_agent.tools),
            ):
                _strip_mcp_frames(error)
            return TurnNeedsAttention(reason, error, self._session.rate_limits())

    async def steer(self, input: UserInput) -> None:
        """Steer this submitted task's current native step, never another task."""
        await self._session._steer(self, input)

    async def steer_exact(self, input: UserInput, *, expected_turn_id: str) -> SteeringReceipt:
        """Steer one exact native root turn and return its acceptance receipt.

        Pass the ID from a TurnStarted observation for strict steering. A stale
        target raises StaleTurnError. Strict input cannot start a continuation.
        Use one steering mode per native turn. If the caller cancels the wait
        after sending input, its acceptance may be unknown.
        """
        if not isinstance(expected_turn_id, str) or not expected_turn_id:
            raise ValueError("expected_turn_id must be a nonempty native root turn ID")
        receipt = await self._session._steer(self, input, expected_turn_id=expected_turn_id)
        assert receipt is not None
        return receipt

    async def cancel(self, *, mode: Literal["graceful", "force"] = "force") -> None:
        """Stop and join this root's callbacks and cells. A settled root is a no-op.

        Child turns require Subagent.try_cancel, whose guarantee is weaker.
        """
        await self._session._cancel_turn(self, mode)

    def events(self, *, after: int = 0, include_execution_details: bool = False) -> EventStream:
        """Observe owned steps; all retention omits gaps from before admission.

        TurnFinished describes an actual native step; get_result() is overall completion.
        The iterator ends when the owned steps settle. For a yielded cell or background
        command that completes later, keep reading Session.events(). To replay a late
        completion still in the buffer, open a new turn cursor and pass the last
        sequence you read as after.
        Set include_execution_details=True for a trusted reader that needs
        command, patch, MCP, or provider-hosted tool payloads. Hosted completions
        default to HostedToolCallSummary; hosted progress includes only its event type.
        """
        from .observation import EventStream

        self._session._check_owner(self)
        self._session._check_callback_operation("observe its own Session")
        return EventStream(
            self._session._observations,
            after=after,
            _turn=self,
            _include_execution_details=include_execution_details,
        )

    def _steps(self) -> Iterator[Turn[OutputT]]:
        step = self
        while True:
            yield step
            successor = step._continuation
            if (
                successor is None
                or not successor.done()
                or successor.cancelled()
                or successor.exception() is not None
            ):
                return
            step = successor.result()

    def _root_result_ready(self) -> bool:
        """Check whether a root result can be read without waiting on native work."""
        step = list(self._steps())[-1]
        completion = step._completion
        return (
            completion is not None
            and completion.done()
            and (step._continuation is None or step._continuation.done())
        )


class ChildTurn(Protocol):
    """Supported view of an exact native child turn. Cancel through Subagent.try_cancel."""

    @property
    def session_id(self) -> str: ...
    @property
    def turn_id(self) -> str | None: ...
    @property
    def submission_id(self) -> str: ...
    @property
    def child_id(self) -> str | None: ...
    @property
    def instance_id(self) -> int | None: ...

    @property
    def guardrail_results(self) -> tuple[Any, ...]: ...

    @property
    def application_tool_outputs(self) -> tuple[ApplicationToolOutput, ...]: ...

    @property
    def run_usage(self) -> RunUsage: ...

    async def get_result(self) -> Completed[str, ChildTurn]: ...
    async def wait_for_request_or_result(
        self,
    ) -> Completed[str, ChildTurn] | PendingRequests[str, ChildTurn]: ...
    async def wait_for_attention(
        self,
    ) -> Completed[str, ChildTurn] | PendingRequests[str, ChildTurn] | TurnNeedsAttention: ...
    async def steer(self, input: UserInput) -> None: ...
    def events(self, *, after: int = 0, include_execution_details: bool = False) -> EventStream: ...


def _root_turn(
    session: Session[Any, Any] | _CompletedTurnOwner,
    turn_id: str | None,
    submission_id: str,
    completion: asyncio.Future[Any],
    observation_after: int = 0,
    guardrail_results: list[Any] | None = None,
) -> Turn[Any]:
    result = object.__new__(Turn)
    for name, value in {
        "session_id": session.id,
        "turn_id": turn_id,
        "submission_id": submission_id,
        "child_id": None,
        "instance_id": None,
        "_session": session,
        "_owner": session._owner,
        "_run_usage": _UsageCollector(),
        "_completion": completion,
        "_continuation": None,
        "_result": None,
        "_native": None,
        "_observation_after": observation_after,
        "_guardrail_results": [] if guardrail_results is None else guardrail_results,
    }.items():
        object.__setattr__(result, name, value)
    return result


def _child_turn(session: Session[Any, Any], native: Any) -> Turn[str]:
    incarnation = native.incarnation
    if incarnation is None:
        from .session import NativeError

        raise NativeError("Native child turn has no captured SDK incarnation")
    observed = session._child_callbacks.get((native.child_id, incarnation, native.turn_id))
    result = object.__new__(Turn)
    for name, value in {
        "session_id": session.id,
        "turn_id": native.turn_id,
        "submission_id": _child_submission_id(native.child_id, incarnation, native.turn_id),
        "child_id": native.child_id,
        "instance_id": native.instance_id,
        "_session": session,
        "_owner": session._owner,
        "_run_usage": observed.run_usage if observed is not None else _UsageCollector(),
        "_completion": None,
        "_continuation": None,
        "_result": None,
        "_native": native,
        "_guardrail_results": observed.guardrail_results if observed is not None else [],
        "_observation_after": observed.observation_after
        if observed is not None
        else session._observations.sequence,
    }.items():
        object.__setattr__(result, name, value)
    return result


def _child_submission_id(child_id: str, incarnation: str, turn_id: str) -> str:
    return f"child:{child_id}:{incarnation}:{turn_id}"


_protect_mcp_methods(
    Turn, lambda owner: (owner._session._opening_agent, *owner._session._opening_agent.tools)
)
_preserve_module_identity(globals(), "agents.turn")

"""Bounded, passive observations of a native Session.

Sequences belong to one Session. Observations are an in-memory suffix, not a
durable log. Use Turn.get_result and canonical history for authoritative results.
"""

from __future__ import annotations

import asyncio
import json
import weakref
from collections import deque
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypeVar, cast

from ._events import ExecutionItem
from .rate_limits import ApiRateLimits
from .usage import TokenUsageInfo

if TYPE_CHECKING:
    from .turn import Turn

__all__ = [
    "ApiRateLimitsUpdated",
    "ContextCompacted",
    "EventGap",
    "EventStream",
    "ExecutionItemCompleted",
    "Observation",
    "OutputTextDelta",
    "SubagentLifecycle",
    "SessionEnded",
    "TurnFinished",
    "TurnStarted",
    "ToolFinished",
    "ToolStarted",
    "UsageUpdated",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Event:
    session_id: str
    sequence: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class _SubmissionEvent(_Event):
    """An actual submission; parent_submission_id links only derived repair work."""

    submission_id: str
    turn_id: str
    parent_submission_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStarted(_SubmissionEvent):
    """Native accepted this exact submission."""

    type: Literal["turn_started"] = field(default="turn_started", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnFinished(_SubmissionEvent):
    """The submission and its owned callback cleanup have settled."""

    status: Literal["completed", "failed", "cancelled"]
    error_type: str | None = None
    type: Literal["turn_finished"] = field(default="turn_finished", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputTextDelta(_SubmissionEvent):
    """A native output-text delta with its original native item identity."""

    item_id: str
    delta: str
    type: Literal["output_text_delta"] = field(default="output_text_delta", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageUpdated(_SubmissionEvent):
    """A native token-count snapshot emitted while this submission was active.

    Usage may repeat when native rate limits update. None means native usage
    is not available; it does not mean zero. Do not add these snapshots.
    """

    usage: TokenUsageInfo | None
    type: Literal["usage_updated"] = field(default="usage_updated", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiRateLimitsUpdated(_SubmissionEvent):
    """Rate-limit fields reported on a provider response for this turn."""

    snapshot: ApiRateLimits
    type: Literal["api_rate_limits_updated"] = field(default="api_rate_limits_updated", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCompacted(_SubmissionEvent):
    """Core completed this native context-compaction item."""

    item_id: str
    type: Literal["context_compacted"] = field(default="context_compacted", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStarted(_SubmissionEvent):
    """A function was admitted, or native MCP handling began before any approval.

    MCP start does not prove that the server has executed the call.
    """

    call_id: str
    name: str
    namespace: str | None
    family: Literal["function", "mcp"] = "function"
    type: Literal["tool_started"] = field(default="tool_started", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFinished(_SubmissionEvent):
    """A function settled, or Core delivered an MCP end with its result status.

    MCP cancellation may have no end event. Status does not imply effect rollback.
    """

    call_id: str
    name: str
    namespace: str | None
    status: Literal["completed", "failed", "cancelled"]
    family: Literal["function", "mcp"] = "function"
    type: Literal["tool_finished"] = field(default="tool_finished", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionItemCompleted(_SubmissionEvent):
    """Core reported the end of a command, patch, or MCP call.

    Match the item's thread, turn, and item IDs to its retained record in
    Session.items() after an EventGap or reopen. Pass subagent_id for a child
    thread. Core bounds command output and MCP results before retention.
    Calls interrupted before Core reports a completion may have no item.
    """

    item: ExecutionItem
    type: Literal["execution_item_completed"] = field(
        default="execution_item_completed", init=False
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentLifecycle(_Event):
    """A change in the native spawn graph, or a restored graph snapshot.

    Use kind="created" to distinguish a new spawn from existing descendants
    reported when a Session opens. status describes the graph edge, not whether
    the child currently has a loaded runtime.
    """

    subagent_id: str
    parent_agent_id: str
    status: str
    kind: Literal["created", "active", "closed", "snapshot"] = "snapshot"
    type: Literal["subagent_lifecycle"] = field(default="subagent_lifecycle", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionEnded(_Event):
    """No further observations will arrive; the Session context still owns cleanup."""

    reason: Literal["closed", "failed"]
    type: Literal["session_ended"] = field(default="session_ended", init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class EventGap(_Event):
    """Global sequences first_sequence through sequence are no longer retained.

    The gap does not mean that native work or authoritative history was lost.
    """

    first_sequence: int
    type: Literal["gap"] = field(default="gap", init=False)


Observation: TypeAlias = (
    TurnStarted
    | TurnFinished
    | SubagentLifecycle
    | OutputTextDelta
    | UsageUpdated
    | ApiRateLimitsUpdated
    | ContextCompacted
    | ToolStarted
    | ToolFinished
    | ExecutionItemCompleted
    | SessionEnded
    | EventGap
)

_ObservationT = TypeVar("_ObservationT", bound=Observation)


class _ObservationBuffer:
    """One bounded suffix; publishing never awaits or invokes application code."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.loop = asyncio.get_running_loop()
        self.records: deque[tuple[Observation, int]] = deque()
        self.characters = 0
        self.sequence = 0
        self.closed = False
        self.changed = asyncio.Event()
        self._callback_check: weakref.WeakMethod[Callable[[str], None]] | None = None
        self.repair_parent: tuple[_ObservationBuffer, str] | None = None

    def _check_read(self) -> None:
        if self._callback_check is not None:
            check = self._callback_check()
            if check is not None:
                check("read events from its own Session")

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def turn_started(self, turn_id: str) -> TurnStarted | None:
        """Find the retained owner of a late native completion."""
        for event, _ in reversed(self.records):
            if isinstance(event, TurnStarted) and event.turn_id == turn_id:
                return event
        return None

    def _mark_gap(self) -> None:
        if self.closed:
            return
        self.sequence += 1
        # Drop the prefix so the retained observations form a contiguous suffix.
        self.records.clear()
        self.characters = 0
        self.notify()

    def append(self, event: _ObservationT) -> _ObservationT | None:
        if self.closed:
            return None
        event = replace(event, sequence=self.sequence + 1)
        size = sum(
            len(value)
            for definition in fields(event)
            if isinstance(value := getattr(event, definition.name), str)
        )
        if isinstance(event, ExecutionItemCompleted):
            size += len(json.dumps(event.item, ensure_ascii=False, separators=(",", ":")))
        if size > 65_536:
            self._mark_gap()
        else:
            self.sequence += 1
            self.records.append((event, size))
            self.characters += size
            while len(self.records) > 256 or self.characters > 65_536:
                _, removed = self.records.popleft()
                self.characters -= removed
            self.notify()
        if self.repair_parent is not None and isinstance(event, _SubmissionEvent):
            buffer, parent_id = self.repair_parent
            buffer.append(cast(Observation, replace(event, parent_submission_id=parent_id)))
        return event

    def finish(self, reason: Literal["closed", "failed"]) -> None:
        if not self.closed:
            self.append(SessionEnded(session_id=self.session_id, reason=reason))
            self.closed = True
            self.notify()


class EventStream(AsyncIterator[Observation]):
    """An independent cursor over a Session, optionally restricted to one turn.

    Closing or cancelling this reader leaves native work running. Live requests
    are available through Session.interactions, Session.pending_interactions,
    or Turn.wait_for_request_or_result.
    """

    def __init__(
        self, buffer: _ObservationBuffer, *, after: int, _turn: Turn[Any] | None = None
    ) -> None:
        if after < 0 or after > buffer.sequence:
            raise ValueError("after must be between zero and the current event sequence")
        self._buffer = buffer
        self._after = after
        self._closed = False
        self._reading = False
        self._turn = _turn

    def __aiter__(self) -> EventStream:
        return self

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._buffer.loop:
            raise RuntimeError("EventStream must be used on its creating event loop")

    async def __anext__(self) -> Observation:
        self._check_loop()
        self._buffer._check_read()
        if self._reading:
            raise RuntimeError("EventStream already has a pending reader")
        self._reading = True
        try:
            while not self._closed:
                signal = self._buffer.changed
                records = self._buffer.records
                oldest = records[0][0].sequence if records else self._buffer.sequence + 1
                if self._after < oldest - 1:
                    first = self._after + 1
                    self._after = oldest - 1
                    return EventGap(
                        session_id=self._buffer.session_id,
                        sequence=self._after,
                        first_sequence=first,
                    )
                for event, _ in records:
                    if event.sequence > self._after:
                        self._after = event.sequence
                        if isinstance(event, ExecutionItemCompleted):
                            event = replace(event, item=deepcopy(event.item))
                        if self._turn is None:
                            return event
                        owners = {step.submission_id for step in self._turn._steps()}
                        if getattr(event, "submission_id", None) in owners:
                            if isinstance(event, TurnFinished) and self._turn.child_id is not None:
                                self._closed = True
                            return event
                        if getattr(event, "parent_submission_id", None) in owners:
                            return event
                if self._buffer.closed:
                    break
                if self._turn is not None:
                    step = list(self._turn._steps())[-1]
                    completion = step._completion
                    if (
                        completion is not None
                        and completion.done()
                        and (step._continuation is None or step._continuation.done())
                    ):
                        break
                    children = self._turn._session._callbacks_for_child(self._turn)
                    if children and all(child.completion.done() for child in children):
                        break
                await signal.wait()
            self._closed = True
            raise StopAsyncIteration
        finally:
            self._reading = False

    async def aclose(self) -> None:
        """Wake and close only this reader."""
        self._check_loop()
        self._closed = True
        self._buffer.notify()

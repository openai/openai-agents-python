"""Run-local input for an already active asynchronous Runner invocation."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from .exceptions import InputGuardrailTripwireTriggered, UserError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .items import InputItem, TResponseInputItem
    from .run_internal.oai_conversation import OpenAIServerConversationTracker

__all__ = ["RunControl", "SteeringReceipt", "SteeringOutcome"]


@dataclass(frozen=True)
class SteeringOutcome:
    """What the SDK could establish about a correction at its target request."""

    status: Literal["submitted", "not_submitted", "unknown"]
    reason: str
    turn_number: int | None
    retained: bool
    """Whether the run admitted or staged the item. It may already be in recoverable history."""


class SteeringReceipt:
    """Observe one correction without exposing its text or cancelling the run."""

    def __init__(self) -> None:
        self.id = uuid4().hex
        self._future: asyncio.Future[SteeringOutcome] = asyncio.get_running_loop().create_future()

    @property
    def status(self) -> Literal["queued", "submitted", "not_submitted", "unknown"]:
        return self._future.result().status if self._future.done() else "queued"

    async def wait(self) -> SteeringOutcome:
        """Wait for dispatch or the end of the opportunity to dispatch this message."""
        return await asyncio.shield(self._future)


class RunControl:
    """Send user text to the next scheduled request in one asynchronous run.

    Pass a fresh handle to Runner.run or Runner.run_streamed. Call send on the
    run's event loop after the first request starts, or during resumed tool work.
    Sending never interrupts a tool, approves an action or starts an extra turn.
    The queue is in memory. After an approval pause, use RunState.add_input on
    an eligible paused state and a fresh handle for the resumed invocation.
    """

    def __init__(self, *, max_pending: int = 100) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._limit = max_pending
        self._loop: asyncio.AbstractEventLoop | None = None
        self._used = False
        self._end_reason = "run_ended"
        self._accepting = False
        self._pending: list[tuple[str, SteeringReceipt]] = []
        self._batch: list[SteeringReceipt] = []
        self._turn: int | None = None
        self._retained = False
        self._items: list[InputItem] = []
        self._before: list[TResponseInputItem] = []
        self._targets: list[TResponseInputItem | None] = []

    def send(self, text: str) -> SteeringReceipt:
        """Queue a nonempty user message. Never wait inside the tool that must finish first."""
        if not self._accepting:
            raise UserError("RunControl is not accepting input for an active run")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not self._loop:
            raise UserError("Send input on the run's event loop")
        if not isinstance(text, str) or not text.strip():
            raise UserError("RunControl.send requires non-empty user text")
        if len(self._pending) >= self._limit:
            raise UserError("RunControl input queue is full")
        receipt = SteeringReceipt()
        self._pending.append((text, receipt))
        return receipt

    def _reserve(self) -> None:
        if self._used:
            raise UserError("RunControl belongs to a single run. Use a fresh handle.")
        self._used = True

    def _take(self, turn: int) -> list[TResponseInputItem]:
        from ._redaction import redact_items
        from .items import ItemHelpers

        if not self._pending:
            return []
        pending, self._pending = self._pending, []
        self._batch = [receipt for _, receipt in pending]
        self._turn = turn
        items = [item for text, _ in pending for item in ItemHelpers.input_to_new_input_list(text)]
        return redact_items(items)

    def _admitted(self, items: list[InputItem]) -> None:
        if self._batch:
            self._items = items[-len(self._batch) :]

    def _before_filter(
        self,
        items: list[TResponseInputItem],
        tracker: OpenAIServerConversationTracker | None,
    ) -> None:
        if not self._batch:
            return
        self._before = list(items)
        if tracker is not None:
            by_id = {
                entry[2]: item
                for item in items
                if (entry := tracker.prepared_item_sources.get(id(item))) is not None
                and entry[0] is item
                and entry[2] is not None
            }
            self._targets = [by_id.get(item.input_id) for item in self._items]
        else:
            # Live items form the final, ordered suffix of the local conversation.
            tail = items[-len(self._items) :]
            self._targets = [
                tail[i] if len(tail) == len(self._items) and tail[i] == item.raw_item else None
                for i, item in enumerate(self._items)
            ]

    def _dispatch(self, items: list[TResponseInputItem]) -> None:
        if self._loop is None:
            return
        self._accepting = True
        if not self._batch:
            return
        original_ids = {id(item) for item in self._before}
        final_ids = {id(item) for item in items}
        for receipt, target, admitted in zip(self._batch, self._targets, self._items, strict=True):
            outcome: SteeringOutcome
            if target is not None and id(target) in final_ids and target == admitted.raw_item:
                outcome = SteeringOutcome("submitted", "adapter_dispatch", self._turn, True)
            elif target is not None and id(target) not in final_ids and final_ids <= original_ids:
                outcome = SteeringOutcome("not_submitted", "filtered", self._turn, True)
            else:
                outcome = SteeringOutcome("unknown", "input_transformed", self._turn, True)
            receipt._future.set_result(outcome)
        self._clear_batch()

    def _clear_batch(self) -> None:
        self._batch.clear()
        self._items.clear()
        self._before.clear()
        self._targets.clear()
        self._turn = None
        self._retained = False

    def _close(self, reason: str) -> None:
        self._accepting = False
        for receipt in self._batch:
            if not receipt._future.done():
                receipt._future.set_result(
                    SteeringOutcome("not_submitted", reason, self._turn, self._retained)
                )
        for _, receipt in self._pending:
            receipt._future.set_result(SteeringOutcome("not_submitted", reason, None, False))
        self._pending.clear()
        self._clear_batch()
        self._loop = None


_current: ContextVar[RunControl | None] = ContextVar("agents_run_control", default=None)


def current_control() -> RunControl | None:
    return _current.get()


@contextmanager
def control_scope(control: RunControl | None) -> Iterator[None]:
    # Always mask the parent handle, including when Agent.as_tool starts a nested run.
    if control is not None:
        control._loop = asyncio.get_running_loop()
    token = _current.set(control)
    reason = "run_ended"
    try:
        yield
    except InputGuardrailTripwireTriggered:
        reason = "guardrail"
        raise
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    except BaseException:
        reason = "run_failed"
        raise
    finally:
        _current.reset(token)
        if control is not None:
            control._close(control._end_reason if reason == "run_ended" else reason)

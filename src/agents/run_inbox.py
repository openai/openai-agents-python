"""Run-local input for an already active asynchronous Runner invocation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence, Set
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from .exceptions import InputGuardrailTripwireTriggered, UserError
from .run_budget import RunBudgetError, _BudgetState

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from .items import InputItem, TResponseInputItem
    from .run_internal.oai_conversation import OpenAIServerConversationTracker
    from .run_state import RunState

__all__ = ["RunInbox", "MessageReceipt", "MessageOutcome"]


def _snapshot_answer(answer: Any) -> Any:
    """Copy answer data using Python's copy protocol, leaving private model state alone."""
    # A Pydantic model may own a private client or lock. Seed deepcopy with
    # shallow model copies, then detach their public fields using the same memo.
    memo: dict[int, Any] = {}
    models: list[BaseModel] = []
    seen: set[int] = set()

    def stored_fields(model: BaseModel) -> dict[str, Any]:
        # Reading the stored values preserves cached aliases without calling getters.
        cls = type(model)
        return {
            key: value
            for key, value in vars(model).items()
            if key in cls.model_fields or key in cls.model_computed_fields
        }

    def find_models(value: Any) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, BaseModel):
            memo[id(value)] = value.model_copy()
            models.append(value)
            for item in stored_fields(value).values():
                find_models(item)
            find_models(value.model_extra)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                find_models(key)
                find_models(item)
        elif isinstance(value, (Sequence, Set)) and not isinstance(
            value, (str, bytes, bytearray, memoryview)
        ):
            for item in value:
                find_models(item)
        elif is_dataclass(value) and not isinstance(value, type):
            for field in fields(value):
                find_models(getattr(value, field.name))

    find_models(answer)
    for model in models:
        clone = memo[id(model)]
        for name, value in stored_fields(model).items():
            vars(clone)[name] = deepcopy(value, memo)
        if model.model_extra is not None:
            clone.model_extra.update(deepcopy(model.model_extra, memo))
    return deepcopy(answer, memo)


@dataclass(frozen=True)
class MessageOutcome:
    """What happened on the first turn that tried to send this message.

    Submission means the SDK passed the message to the adapter. A filter can
    omit a message on this turn and a later request can still include it.
    Check retained before resending. Later turns do not update this receipt.
    """

    status: Literal["submitted", "not_submitted", "unknown"]
    reason: str
    turn_number: int | None
    retained: bool
    """Whether the run retained the item in history or staged it for possible later delivery."""


class MessageReceipt:
    """Track SDK submission without retaining the message text or assuming provider acceptance."""

    def __init__(self) -> None:
        self.id = uuid4().hex
        self._future: asyncio.Future[MessageOutcome] = asyncio.get_running_loop().create_future()

    @property
    def status(self) -> Literal["queued", "submitted", "not_submitted", "unknown"]:
        return self._future.result().status if self._future.done() else "queued"

    async def wait(self) -> MessageOutcome:
        """Wait until Runner knows the outcome.

        Keep consuming stream events while waiting. Waiting inside a tool or the
        stream consumer can prevent the next model request from starting.
        """
        return await asyncio.shield(self._future)


class RunInbox:
    """Send messages to one asynchronous run.

    Pass a fresh inbox to Runner.run(..., inbox=inbox). For Runner.run_streamed,
    you can call steer() and queue() directly on the returned result. Call these
    methods on the run's event loop. Neither interrupts a running tool or model
    request. In Codex mode, messages arrive between Runner requests, never inside
    a native Codex turn.

    Messages stay in memory. Pausing for approval, cancellation, failure, or
    reaching the turn limit ends delivery. Check the receipt before resending.
    For a paused run, use RunState.add_input before resuming with a fresh inbox.
    Do not wait for a receipt inside a tool that must finish before delivery.

    If approval pauses a follow-up, the checkpoint identifies the active task
    so guardrails see the same input after resume. It does not save the inbox
    or messages still waiting in the queue.

    Callbacks should propagate CancelledError after cleanup. On Python 3.10,
    Runner.run cannot detect cancellation if a callback suppresses that exception.
    """

    def __init__(self, *, max_pending: int = 100) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._limit = max_pending
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancelling: Callable[[], int] | None = None
        self._initial_cancellations = 0
        self._used = False
        self._end_reason = "run_ended"
        self._started = False
        self._previous_outputs: tuple[Any, ...] = ()
        self._pending_output: tuple[Any, ...] = ()
        self._steers: list[tuple[str, MessageReceipt]] = []
        self._queued: deque[tuple[str, MessageReceipt]] = deque()
        self._batch: list[MessageReceipt] = []
        self._turn: int | None = None
        self._retained = False
        self._items: list[InputItem] = []
        self._before: list[tuple[TResponseInputItem, bytes | None]] = []
        self._instructions: str | None = None
        self._targets: list[tuple[TResponseInputItem | None, bytes | None]] = []

    def steer(self, text: str) -> MessageReceipt:
        """Guide the current task after the current model request and its tools finish.

        Runner sends all pending steering messages in order before starting a queued
        task. If the model has already answered, Runner continues with the new input.
        """
        receipt = self._new_receipt(text)
        self._steers.append((text, receipt))
        return receipt

    def queue(self, text: str) -> MessageReceipt:
        """Start this follow-up after the current task finishes successfully.

        Runner takes one follow-up at a time in arrival order. The same agent,
        conversation, guardrails and run limits apply. The last answer is final_output.
        """
        receipt = self._new_receipt(text)
        self._queued.append((text, receipt))
        return receipt

    def _new_receipt(self, text: str) -> MessageReceipt:
        if self._loop is None:
            raise UserError("RunInbox is not accepting input for an active run")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not self._loop:
            raise UserError("Send input on the run's event loop")
        if not isinstance(text, str) or not text.strip():
            raise UserError("RunInbox requires non-empty user text")
        if len(self._steers) + len(self._queued) >= self._limit:
            raise UserError("RunInbox input queue is full")
        return MessageReceipt()

    def _reserve(self) -> None:
        if self._used:
            raise UserError("RunInbox belongs to a single run. Use a fresh inbox.")
        self._used = True

    def _bind(self, *, owner: asyncio.Task[Any] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        # Only an awaited run belongs to the calling task. Python 3.10 has no
        # public cancellation count, so it relies on CancelledError propagating.
        self._cancelling = getattr(owner, "cancelling", None)
        if self._cancelling is not None:
            self._initial_cancellations = self._cancelling()

    def _check_owner_cancellation(self) -> None:
        if self._cancelling is not None and self._cancelling() > self._initial_cancellations:
            raise asyncio.CancelledError

    @property
    def _has_pending(self) -> bool:
        return bool(self._steers or self._queued)

    def _stop_accepting(self) -> None:
        self._loop = None

    def _can_admit_input(
        self, current_turn: int, max_turns: int | None, budget: _BudgetState | None
    ) -> bool:
        """Check limits before admitting optional input. Dispatch charges the budget."""
        if max_turns is not None and current_turn >= max_turns:
            self._end_reason = "max_turns"
            self._stop_accepting()
            return False
        if budget is not None:
            try:
                budget.check()
            except RunBudgetError as error:
                self._end_reason = error.code
                self._stop_accepting()
                return False
        return True

    def _advance(
        self, current_turn: int, max_turns: int | None, budget: _BudgetState | None
    ) -> bool:
        """Select the next task only after the current answer has passed its guardrails."""
        if not self._has_pending:
            self._stop_accepting()
            return False
        if not self._can_admit_input(current_turn, max_turns, budget):
            return False
        if not self._steers:
            self._steers.append(self._queued.popleft())
        return True

    def _stage_output(self, output: Any) -> None:
        # Tool callbacks can return private values that do not belong in model
        # input or session history. Keep a caller-only snapshot after guardrails.
        from ._redaction import require_clean

        self._pending_output = (_snapshot_answer(output),)
        require_clean([*self._previous_outputs, *self._pending_output])

    def _begin_turn(self) -> tuple[Any, ...]:
        if self._batch:
            self._check_owner_cancellation()
        # Cancellation during admission leaves the answer as final_output.
        if self._pending_output:
            self._previous_outputs += self._pending_output
            self._pending_output = ()
        return self._previous_outputs

    def _check_final_output(self, output: Any) -> None:
        if self._previous_outputs:
            from ._redaction import require_clean

            require_clean([*self._previous_outputs, output])

    def _take(
        self, current_turn: int, max_turns: int | None, budget: _BudgetState | None
    ) -> list[TResponseInputItem]:
        from ._redaction import redact_items
        from .items import ItemHelpers

        if not self._started or not self._steers:
            return []
        if not self._can_admit_input(current_turn, max_turns, budget):
            return []
        self._check_owner_cancellation()
        pending, self._steers = self._steers, []
        self._batch = [receipt for _, receipt in pending]
        self._turn = current_turn + 1
        items = [item for text, _ in pending for item in ItemHelpers.input_to_new_input_list(text)]
        return redact_items(items)

    def _stage(self, items: list[TResponseInputItem], run_state: RunState[Any]) -> None:
        run_state._pending_input.extend(items)
        self._retained = True
        if self._pending_output:
            # A follow-up starts a new task. A steer during a tool turn
            # stays with the current task. Retries must keep that distinction.
            run_state._task_input_ids = run_state._track_pending_input()[-len(items) :]

    def _admitted(self, items: list[InputItem]) -> None:
        if self._batch:
            self._items = items[-len(self._batch) :]

    @staticmethod
    def _item_digest(item: TResponseInputItem) -> bytes | None:
        # Replay fingerprints normalize fields and stringify opaque values. Receipts
        # need the exact content, without invoking caller-defined serializers.
        from .run_state import _validate_run_state_json_value

        try:
            _validate_run_state_json_value(item)
            encoded = json.dumps(item, ensure_ascii=False, allow_nan=False).encode()
            return hashlib.sha256(encoded).digest()
        except (TypeError, ValueError, RecursionError):
            return None

    def _before_filter(
        self,
        items: list[TResponseInputItem],
        tracker: OpenAIServerConversationTracker | None,
        *,
        filter_input: list[TResponseInputItem] | None,
        instructions: str | None,
    ) -> None:
        if not self._batch:
            return
        self._targets.clear()
        self._before.clear()
        if tracker is not None:
            by_id = {
                entry[2]: item
                for item in items
                if (entry := tracker.prepared_item_sources.get(id(item))) is not None
                and entry[0] is item
                and entry[2] is not None
            }
            targets = [by_id.get(item.input_id) for item in self._items]
        else:
            # Live items form the final, ordered suffix before policy feedback.
            count = len(self._items)
            targets = [items[i - count] if len(items) >= count else None for i in range(count)]
        for target, admitted in zip(targets, self._items, strict=True):
            expected = self._item_digest(admitted.raw_item)
            if target is None or expected is None or self._item_digest(target) != expected:
                self._targets.append((None, None))
            else:
                self._targets.append((target, expected))
        if filter_input is not None:
            self._before = [(item, self._item_digest(item)) for item in filter_input]
            self._instructions = instructions

    def _dispatch(self, items: list[TResponseInputItem], instructions: str | None) -> None:
        # A retry is another dispatch, even after the first attempt settled the receipts.
        self._check_owner_cancellation()
        self._started = True
        if not self._batch:
            return
        target_ids = {id(target) for target, _ in self._targets if target is not None}
        present = {id(item) for item in items if id(item) in target_ids}
        # A missing occurrence proves omission only when the request is an
        # unchanged subsequence, including any synthetic policy feedback.
        omission_only = False
        same_instructions = (
            type(instructions) is type(self._instructions)
            and (instructions is None or type(instructions) is str)
            and instructions == self._instructions
        )
        if self._before and present != target_ids and same_instructions:
            before = iter(self._before)
            for item in items:
                digest = next((digest for old, digest in before if old is item), None)
                if digest is None or digest != self._item_digest(item):
                    break
            else:
                omission_only = True
        for receipt, (target, expected) in zip(self._batch, self._targets, strict=True):
            outcome: MessageOutcome
            if (
                target is not None
                and id(target) in present
                and self._item_digest(target) == expected
            ):
                outcome = MessageOutcome("submitted", "adapter_dispatch", self._turn, True)
            elif target is not None and id(target) not in present and omission_only:
                outcome = MessageOutcome("not_submitted", "filtered", self._turn, True)
            else:
                outcome = MessageOutcome("unknown", "input_transformed", self._turn, True)
            receipt._future.set_result(outcome)
        self._clear_batch()

    def _clear_batch(self) -> None:
        self._batch.clear()
        self._items.clear()
        self._before.clear()
        self._instructions = None
        self._targets.clear()
        self._turn = None
        self._retained = False

    def _close(self, reason: str) -> None:
        for receipt in self._batch:
            if not receipt._future.done():
                receipt._future.set_result(
                    MessageOutcome("not_submitted", reason, self._turn, self._retained)
                )
        for _, receipt in (*self._steers, *self._queued):
            receipt._future.set_result(MessageOutcome("not_submitted", reason, None, False))
        self._previous_outputs = self._pending_output = ()
        self._steers.clear()
        self._queued.clear()
        self._clear_batch()
        self._loop = None
        self._cancelling = None


_current: ContextVar[RunInbox | None] = ContextVar("agents_run_inbox", default=None)


def current_inbox() -> RunInbox | None:
    return _current.get()


def stop_inbox_intake() -> None:
    """Reject new messages while the runner saves results or cleans up a terminal turn."""
    if (inbox := current_inbox()) is not None:
        inbox._stop_accepting()


@contextmanager
def inbox_scope(inbox: RunInbox | None) -> Iterator[None]:
    # Always mask the parent handle, including when Agent.as_tool starts a nested run.
    token = _current.set(inbox)
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
        if inbox is not None:
            inbox._close(inbox._end_reason if reason == "run_ended" else reason)

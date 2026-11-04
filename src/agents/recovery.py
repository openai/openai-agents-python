"""Application reconciliation of tool calls with missing local receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from ._events import FunctionCallItem, SessionItem
from .history import RecordedTurn

__all__ = ["LocalRecoveryRequired", "ReconciledCall", "UncertainCall"]


@dataclass(frozen=True, slots=True, kw_only=True)
class UncertainCall:
    """A recorded invocation for which local storage has no result receipt."""

    thread_id: str
    turn_id: str
    call_id: str
    name: str
    arguments: JsonValue = field(repr=False)

    def reconcile(
        self, *, outcome: Literal["applied", "not_applied"], result: str
    ) -> ReconciledCall:
        """Create a confirmation using this call's IDs and your verified outcome."""
        return ReconciledCall(
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            call_id=self.call_id,
            outcome=outcome,
            result=result,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciledCall:
    """An outcome that the application verified using its own records.

    Pass this to Agent.session when reopening the original LocalStorage
    conversation. Include only result details the model may read.
    """

    thread_id: str
    turn_id: str
    call_id: str
    outcome: Literal["applied", "not_applied"]
    result: str = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.thread_id, self.turn_id, self.call_id)
        ):
            raise ValueError("Reconciliation requires native thread, turn and call IDs")
        if self.outcome not in ("applied", "not_applied") or not isinstance(self.result, str):
            raise ValueError("Reconciliation requires a confirmed outcome and a text result")


class LocalRecoveryRequired(RuntimeError):
    """Local recovery stopped before model work because tool outcomes are unknown."""

    def __init__(
        self,
        calls: tuple[UncertainCall, ...],
        turns: tuple[RecordedTurn, ...],
        items: tuple[SessionItem, ...],
    ) -> None:
        super().__init__(
            "Native local history has an unknown tool outcome. "
            "Reconcile the recorded calls before reopening."
        )
        self.calls = calls
        self.turns = turns
        self.items = items


def _uncertain_call(thread_id: str, item: FunctionCallItem) -> UncertainCall:
    arguments = item["arguments"]
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            # Some tools, including apply_patch, use plain text.
            pass
    return UncertainCall(
        thread_id=thread_id,
        turn_id=item["turn_id"],
        call_id=item["call_id"],
        name=item["name"],
        arguments=arguments,
    )

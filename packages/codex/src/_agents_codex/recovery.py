"""Application reconciliation of tool calls with missing local receipts."""

from __future__ import annotations

import json as json
from dataclasses import dataclass as dataclass, field as field
from typing import Any, Literal as Literal

from pydantic import JsonValue as JsonValue

from ._events import FunctionCallItem as FunctionCallItem, SessionItem as SessionItem
from ._public import preserve_module_identity as _preserve_module_identity
from .history import RecordedTurn as RecordedTurn
from .shell import ShellOutput as ShellOutput


@dataclass(frozen=True, slots=True, kw_only=True)
class UncertainCall:
    """A recorded invocation for which local storage has no result receipt."""

    thread_id: str
    turn_id: str
    call_id: str
    name: str
    arguments: JsonValue = field(repr=False)

    def reconcile(
        self, *, outcome: Literal["applied", "not_applied"], result: str | ShellOutput
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
    result: str | ShellOutput = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.thread_id, self.turn_id, self.call_id)
        ):
            raise ValueError("Reconciliation requires native thread, turn and call IDs")
        if self.outcome not in ("applied", "not_applied") or not isinstance(
            self.result, (str, ShellOutput)
        ):
            raise ValueError(
                "Reconciliation requires a confirmed outcome and a text or ShellOutput result"
            )

    def _wire(self) -> dict[str, str | bool]:
        data: dict[str, str | bool] = {
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "call_id": self.call_id,
            "outcome": self.outcome,
        }
        if isinstance(self.result, ShellOutput):
            data["result"] = self.result.model_dump_json()
            data["shell"] = True
        else:
            data["result"] = self.result
        return data


class LocalRecoveryRequired(RuntimeError):
    """Local recovery stopped before model work because tool outcomes are unknown.

    Each call identifies its owning thread and original turn. With ``LocalStorage``,
    reopen the root Session with verified results for all listed calls. With
    ``CustomStorage``, record the results through ``CustomStorage.reconcile()``
    before reopening.
    """

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


def _uncertain_call(
    thread_id: str, item: FunctionCallItem, *, original_input: JsonValue | None
) -> UncertainCall:
    arguments = original_input if original_input is not None else item["arguments"]
    if original_input is None and isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    return UncertainCall(
        thread_id=thread_id,
        turn_id=item["turn_id"],
        call_id=item["call_id"],
        name=item["name"],
        arguments=arguments,
    )


def _application_shell_recovery(
    history: list[dict[str, Any]], thread_id: str
) -> tuple[list[UncertainCall], set[tuple[str, str]]]:
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    completed: set[tuple[str, str]] = set()
    reconciled: set[tuple[str, str]] = set()
    for record in history:
        if record["type"] != "response_item":
            continue
        item = record["payload"]
        metadata = item.get("internal_chat_message_metadata_passthrough") or {}
        turn_id = metadata.get("turn_id")
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(turn_id, str) or not isinstance(call_id, str):
            continue
        key = (turn_id, call_id)
        if (
            item["type"] == "shell_call"
            and (record.get("metadata") or {}).get("sdk_application_tool") == "shell"
        ):
            calls[key] = item
        elif item["type"] == "shell_call_output":
            completed.add(key)
            if metadata.get("content_item_kinds") in (
                ["sdk_reconciliation.applied"],
                ["sdk_reconciliation.not_applied"],
            ):
                reconciled.add(key)
    return (
        [
            UncertainCall(
                thread_id=thread_id, turn_id=turn, call_id=call, name="shell", arguments=arguments
            )
            for (turn, call), arguments in calls.items()
            if (turn, call) not in completed
        ],
        reconciled & calls.keys(),
    )


_preserve_module_identity(globals(), "agents.recovery")

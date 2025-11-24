from __future__ import annotations

from collections.abc import Iterable, Sequence as ABCSequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from .agent import Agent
    from .items import InjectedInputItem, RunItem, TResponseInputItem
else:  # pragma: no cover - runtime fallbacks to break import cycles
    Agent = Any  # type: ignore[assignment]
    InjectedInputItem = RunItem = TResponseInputItem = Any  # type: ignore[assignment]


def _input_to_new_input_list(
    value: str | TResponseInputItem | ABCSequence[TResponseInputItem],
) -> list[TResponseInputItem]:
    from .items import ItemHelpers

    if isinstance(value, str):
        return ItemHelpers.input_to_new_input_list(value)
    if isinstance(value, list):
        return ItemHelpers.input_to_new_input_list(value)
    if isinstance(value, ABCSequence):
        sequence_value = cast(Iterable[TResponseInputItem], value)
        return ItemHelpers.input_to_new_input_list(list(sequence_value))
    return ItemHelpers.input_to_new_input_list([value])


InjectedMessageStage = Literal[
    "agent_start",
    "before_llm",
    "after_llm",
    "before_tool",
    "after_tool",
    "unspecified",
]


@dataclass
class _StageMarker:
    stage: InjectedMessageStage
    call_id: str | None


@dataclass
class InjectedMessageRecord:
    item: InjectedInputItem
    stage: InjectedMessageStage
    call_id: str | None
    order: int


@dataclass
class MessageHistory:
    """Tracks the conversation history visible to hooks and allows modifications."""

    _original_input: str | list[TResponseInputItem] | None = None
    _generated_items: list[RunItem] | None = None
    _pending_injected_items: list[InjectedMessageRecord] = field(default_factory=list)
    _next_turn_override: list[TResponseInputItem] | None = None
    _live_input_buffer: list[TResponseInputItem] | None = field(default=None, repr=False)
    _stage_markers: list[_StageMarker] = field(default_factory=list, repr=False)
    _next_order: int = 0

    def set_original_input(self, original_input: str | list[TResponseInputItem]) -> None:
        """Update the original input reference for the current run."""

        self._original_input = original_input

    def bind_generated_items(self, generated_items: list[RunItem]) -> None:
        """Bind the list of generated items accumulated so far."""

        self._generated_items = generated_items

    def get_messages(self) -> list[TResponseInputItem]:
        """Return a snapshot of the current transcript, including pending injections."""

        messages: list[TResponseInputItem] = []
        if self._original_input is not None:
            messages.extend(_input_to_new_input_list(self._original_input))
        if self._generated_items:
            messages.extend(item.to_input_item() for item in self._generated_items)
        if self._pending_injected_items:
            messages.extend(record.item.to_input_item() for record in self._pending_injected_items)
        return messages

    def add_message(
        self,
        *,
        agent: Agent[Any],
        message: str | TResponseInputItem | ABCSequence[TResponseInputItem],
    ) -> None:
        """Queue one or more messages to be appended to the history."""

        from .items import InjectedInputItem

        new_items = _input_to_new_input_list(message)
        for item in new_items:
            normalized = deepcopy(item)
            injected_item = InjectedInputItem(agent=agent, raw_item=normalized)
            stage = self._stage_markers[-1].stage if self._stage_markers else "unspecified"
            call_id = self._stage_markers[-1].call_id if self._stage_markers else None
            self._pending_injected_items.append(
                InjectedMessageRecord(
                    item=injected_item,
                    stage=stage,
                    call_id=call_id,
                    order=self._next_order,
                )
            )
            self._next_order += 1
            if self._live_input_buffer is not None:
                self._live_input_buffer.append(deepcopy(normalized))

    def pending_input_items(self) -> list[TResponseInputItem]:
        """Return pending injected messages as input items without clearing them."""

        return [record.item.to_input_item() for record in self._pending_injected_items]

    def flush_pending_items(self) -> list[InjectedMessageRecord]:
        """Return and clear pending injected messages with metadata."""

        pending = self._pending_injected_items
        self._pending_injected_items = []
        return pending

    def override_next_turn(self, messages: ABCSequence[TResponseInputItem]) -> None:
        """Replace the next model call's input history with a custom list."""

        override_messages = [deepcopy(message) for message in messages]
        self._next_turn_override = override_messages
        if self._live_input_buffer is not None:
            self._live_input_buffer.clear()
            self._live_input_buffer.extend(deepcopy(message) for message in override_messages)

    def consume_next_turn_override(self) -> list[TResponseInputItem] | None:
        """Pop the next turn override if set."""

        if self._next_turn_override is None:
            return None
        override = [deepcopy(message) for message in self._next_turn_override]
        self._next_turn_override = None
        return override

    def clear(self) -> None:
        """Reset all pending state."""

        self._pending_injected_items.clear()
        self._next_turn_override = None
        self._live_input_buffer = None

    def bind_live_input_buffer(self, buffer: list[TResponseInputItem]) -> None:
        """Bind the live model input list so hook mutations apply immediately."""

        self._live_input_buffer = buffer

    def release_live_input_buffer(self) -> None:
        """Stop tracking the live model input once the LLM call completes."""

        self._live_input_buffer = None

    def begin_injection_stage(
        self, stage: InjectedMessageStage, call_id: str | None = None
    ) -> _StageMarker:
        marker = _StageMarker(stage=stage, call_id=call_id)
        self._stage_markers.append(marker)
        return marker

    def end_injection_stage(self, marker: _StageMarker | None) -> None:
        if marker is None:
            return
        if not self._stage_markers:
            return
        if self._stage_markers and self._stage_markers[-1] is marker:
            self._stage_markers.pop()
            return
        try:
            self._stage_markers.remove(marker)
        except ValueError:
            pass

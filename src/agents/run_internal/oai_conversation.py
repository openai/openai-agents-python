"""
Conversation-state helpers used during agent runs. This module should only host internal
tracking and normalization logic for conversation-aware execution, not public-facing APIs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from ..items import ItemHelpers, ModelResponse, RunItem, TResponseInputItem
from ..logger import logger
from ..models.fake_id import FAKE_RESPONSES_ID
from .items import drop_orphan_function_calls, fingerprint_input_item, normalize_input_items_for_api

# --------------------------
# Private helpers (no public exports in this module)
# --------------------------


def _normalize_server_item_id(value: Any) -> str | None:
    """Return a stable server item id, ignoring placeholder IDs."""
    if value == FAKE_RESPONSES_ID:
        # Fake IDs are placeholders from non-Responses providers; ignore them for dedupe.
        return None
    return value if isinstance(value, str) else None


def _fingerprint_for_tracker(item: Any) -> str | None:
    """Return a stable fingerprint for dedupe, ignoring failures."""
    return fingerprint_input_item(item)


@dataclass
class OpenAIServerConversationTracker:
    """Track server-side conversation state for conversation-aware runs."""

    conversation_id: str | None = None
    previous_response_id: str | None = None
    auto_previous_response_id: bool = False
    sent_items: set[int] = field(default_factory=set)
    server_items: set[int] = field(default_factory=set)
    server_item_ids: set[str] = field(default_factory=set)
    server_tool_call_ids: set[str] = field(default_factory=set)
    sent_item_fingerprints: set[str] = field(default_factory=set)
    sent_initial_input: bool = False
    remaining_initial_input: list[TResponseInputItem] | None = None
    primed_from_state: bool = False

    def __post_init__(self):
        """Log initial tracker state to make conversation resume behavior debuggable."""
        logger.debug(
            "Created OpenAIServerConversationTracker for conv_id=%s, prev_resp_id=%s",
            self.conversation_id,
            self.previous_response_id,
        )

    def hydrate_from_state(
        self,
        *,
        original_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
        model_responses: list[ModelResponse],
        session_items: list[TResponseInputItem] | None = None,
    ) -> None:
        """Seed tracking from prior state so resumed runs do not replay already-sent content."""
        if self.sent_initial_input:
            return

        normalized_input = original_input
        if isinstance(original_input, list):
            normalized = normalize_input_items_for_api(original_input)
            normalized_input = drop_orphan_function_calls(normalized)

        for item in ItemHelpers.input_to_new_input_list(normalized_input):
            if item is None:
                continue
            self.sent_items.add(id(item))
            item_id = _normalize_server_item_id(
                item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
            )
            if item_id is not None:
                self.server_item_ids.add(item_id)
            fp = _fingerprint_for_tracker(item)
            if fp:
                self.sent_item_fingerprints.add(fp)

        self.sent_initial_input = True
        self.remaining_initial_input = None

        latest_response = model_responses[-1] if model_responses else None
        for response in model_responses:
            for output_item in response.output:
                if output_item is None:
                    continue
                self.server_items.add(id(output_item))
                item_id = _normalize_server_item_id(
                    output_item.get("id")
                    if isinstance(output_item, dict)
                    else getattr(output_item, "id", None)
                )
                if item_id is not None:
                    self.server_item_ids.add(item_id)
                call_id = (
                    output_item.get("call_id")
                    if isinstance(output_item, dict)
                    else getattr(output_item, "call_id", None)
                )
                has_output_payload = isinstance(output_item, dict) and "output" in output_item
                has_output_payload = has_output_payload or hasattr(output_item, "output")
                if isinstance(call_id, str) and has_output_payload:
                    self.server_tool_call_ids.add(call_id)

        if self.conversation_id is None and latest_response and latest_response.response_id:
            self.previous_response_id = latest_response.response_id

        if session_items:
            for item in session_items:
                item_id = _normalize_server_item_id(
                    item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                )
                if item_id is not None:
                    self.server_item_ids.add(item_id)
                call_id = (
                    item.get("call_id")
                    if isinstance(item, dict)
                    else getattr(item, "call_id", None)
                )
                has_output = isinstance(item, dict) and "output" in item
                has_output = has_output or hasattr(item, "output")
                if isinstance(call_id, str) and has_output:
                    self.server_tool_call_ids.add(call_id)
                fp = _fingerprint_for_tracker(item)
                if fp:
                    self.sent_item_fingerprints.add(fp)
        for item in generated_items:  # type: ignore[assignment]
            run_item: RunItem = cast(RunItem, item)
            raw_item = run_item.raw_item
            if raw_item is None:
                continue
            is_tool_call_item = run_item.type in {"tool_call_item", "handoff_call_item"}

            if isinstance(raw_item, dict):
                item_id = _normalize_server_item_id(raw_item.get("id"))
                call_id = raw_item.get("call_id")
                has_output_payload = "output" in raw_item
                has_output_payload = has_output_payload or hasattr(raw_item, "output")
                has_call_id = isinstance(call_id, str)
                should_mark = item_id is not None or (
                    has_call_id and (has_output_payload or is_tool_call_item)
                )
                if not should_mark:
                    continue

                raw_item_id = id(raw_item)
                self.sent_items.add(raw_item_id)
                fp = _fingerprint_for_tracker(raw_item)
                if fp:
                    self.sent_item_fingerprints.add(fp)

                if item_id is not None:
                    self.server_item_ids.add(item_id)
                if isinstance(call_id, str) and has_output_payload:
                    self.server_tool_call_ids.add(call_id)
            else:
                item_id = _normalize_server_item_id(getattr(raw_item, "id", None))
                call_id = getattr(raw_item, "call_id", None)
                has_output_payload = hasattr(raw_item, "output")
                has_call_id = isinstance(call_id, str)
                should_mark = item_id is not None or (
                    has_call_id and (has_output_payload or is_tool_call_item)
                )
                if not should_mark:
                    continue

                self.sent_items.add(id(raw_item))
                fp = _fingerprint_for_tracker(raw_item)
                if fp:
                    self.sent_item_fingerprints.add(fp)
                if item_id is not None:
                    self.server_item_ids.add(item_id)
                if isinstance(call_id, str) and has_output_payload:
                    self.server_tool_call_ids.add(call_id)
        self.primed_from_state = True

    def track_server_items(self, model_response: ModelResponse | None) -> None:
        """Track server-acknowledged outputs to avoid re-sending them on retries."""
        if model_response is None:
            return

        server_item_fingerprints: set[str] = set()
        for output_item in model_response.output:
            if output_item is None:
                continue
            self.server_items.add(id(output_item))
            item_id = _normalize_server_item_id(
                output_item.get("id")
                if isinstance(output_item, dict)
                else getattr(output_item, "id", None)
            )
            if item_id is not None:
                self.server_item_ids.add(item_id)
            call_id = (
                output_item.get("call_id")
                if isinstance(output_item, dict)
                else getattr(output_item, "call_id", None)
            )
            has_output_payload = isinstance(output_item, dict) and "output" in output_item
            has_output_payload = has_output_payload or hasattr(output_item, "output")
            if isinstance(call_id, str) and has_output_payload:
                self.server_tool_call_ids.add(call_id)
            fp = _fingerprint_for_tracker(output_item)
            if fp:
                self.sent_item_fingerprints.add(fp)
                server_item_fingerprints.add(fp)

        if self.remaining_initial_input and server_item_fingerprints:
            remaining: list[TResponseInputItem] = []
            for pending in self.remaining_initial_input:
                pending_fp = _fingerprint_for_tracker(pending)
                if pending_fp and pending_fp in server_item_fingerprints:
                    continue
                remaining.append(pending)
            self.remaining_initial_input = remaining or None

        if (
            self.conversation_id is None
            and (self.previous_response_id is not None or self.auto_previous_response_id)
            and model_response.response_id is not None
        ):
            self.previous_response_id = model_response.response_id

    def mark_input_as_sent(self, items: Sequence[TResponseInputItem]) -> None:
        """Mark delivered inputs so we do not send them again after pauses or retries."""
        if not items:
            return

        delivered_ids: set[int] = set()
        for item in items:
            if item is None:
                continue
            delivered_ids.add(id(item))
            self.sent_items.add(id(item))

        if not self.remaining_initial_input:
            return

        delivered_by_content: set[str] = set()
        for item in items:
            fp = _fingerprint_for_tracker(item)
            if fp:
                delivered_by_content.add(fp)

        remaining: list[TResponseInputItem] = []
        for pending in self.remaining_initial_input:
            if id(pending) in delivered_ids:
                continue
            pending_fp = _fingerprint_for_tracker(pending)
            if pending_fp and pending_fp in delivered_by_content:
                continue
            remaining.append(pending)

        self.remaining_initial_input = remaining or None

    def rewind_input(self, items: Sequence[TResponseInputItem]) -> None:
        """Rewind previously marked inputs so they can be resent."""
        if not items:
            return

        rewind_items: list[TResponseInputItem] = []
        for item in items:
            if item is None:
                continue
            rewind_items.append(item)
            self.sent_items.discard(id(item))
            fp = _fingerprint_for_tracker(item)
            if fp:
                self.sent_item_fingerprints.discard(fp)

        if not rewind_items:
            return

        logger.debug("Queued %d items to resend after conversation retry", len(rewind_items))
        existing = self.remaining_initial_input or []
        self.remaining_initial_input = rewind_items + existing

    def prepare_input(
        self,
        original_input: str | list[TResponseInputItem],
        generated_items: list[RunItem],
    ) -> list[TResponseInputItem]:
        """Assemble the next model input while skipping duplicates and approvals."""
        input_items: list[TResponseInputItem] = []

        if not self.sent_initial_input:
            initial_items = ItemHelpers.input_to_new_input_list(original_input)
            input_items.extend(initial_items)
            filtered_initials = []
            for item in initial_items:
                if item is None or isinstance(item, (str, bytes)):
                    continue
                filtered_initials.append(item)
            self.remaining_initial_input = filtered_initials or None
            self.sent_initial_input = True
        elif self.remaining_initial_input:
            input_items.extend(self.remaining_initial_input)

        for item in generated_items:  # type: ignore[assignment]
            run_item: RunItem = cast(RunItem, item)
            if run_item.type == "tool_approval_item":
                continue

            raw_item = run_item.raw_item
            if raw_item is None:
                continue

            item_id = _normalize_server_item_id(
                raw_item.get("id") if isinstance(raw_item, dict) else getattr(raw_item, "id", None)
            )
            if item_id is not None and item_id in self.server_item_ids:
                continue

            call_id = (
                raw_item.get("call_id")
                if isinstance(raw_item, dict)
                else getattr(raw_item, "call_id", None)
            )
            has_output_payload = isinstance(raw_item, dict) and "output" in raw_item
            has_output_payload = has_output_payload or hasattr(raw_item, "output")
            if (
                isinstance(call_id, str)
                and has_output_payload
                and call_id in self.server_tool_call_ids
            ):
                continue

            raw_item_id = id(raw_item)
            if raw_item_id in self.sent_items or raw_item_id in self.server_items:
                continue

            to_input = getattr(run_item, "to_input_item", None)
            input_item = to_input() if callable(to_input) else cast(TResponseInputItem, raw_item)

            fp = _fingerprint_for_tracker(input_item)
            if fp and self.primed_from_state and fp in self.sent_item_fingerprints:
                continue

            input_items.append(input_item)

            self.sent_items.add(raw_item_id)

        return input_items

"""
Conversation-state helpers used during agent runs. This module should only host internal
tracking and normalization logic for conversation-aware execution, not public-facing APIs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from ..items import (
    ItemHelpers,
    ModelResponse,
    RunItem,
    TResponseInputItem,
    _output_item_to_input_item,
)
from ..logger import logger
from ..models.fake_id import FAKE_RESPONSES_ID
from .items import (
    ReasoningItemIdPolicy,
    drop_orphan_function_calls,
    fingerprint_input_item,
    normalize_input_items_for_api,
    prepare_model_input_items,
    run_item_to_input_item,
)

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
    if _is_tool_search_item(item):
        try:
            replayable_item = _output_item_to_input_item(item)
            item_id = _normalize_server_item_id(
                replayable_item.get("id")
                if isinstance(replayable_item, dict)
                else getattr(replayable_item, "id", None)
            )
            call_id = (
                replayable_item.get("call_id")
                if isinstance(replayable_item, dict)
                else getattr(replayable_item, "call_id", None)
            )
            return fingerprint_input_item(
                replayable_item,
                ignore_ids_for_matching=item_id is None and not isinstance(call_id, str),
            )
        except Exception:
            return None
    return fingerprint_input_item(item)


def _anonymous_tool_search_fingerprint(item: Any) -> str | None:
    """Return a content-only fingerprint for restored anonymous tool_search items."""
    if not _is_tool_search_item(item):
        return None

    try:
        return fingerprint_input_item(
            _output_item_to_input_item(item),
            ignore_ids_for_matching=True,
        )
    except Exception:
        return None


def _is_tool_search_item(item: Any) -> bool:
    """Return True for tool_search items that currently lack stable provider identifiers."""
    item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
    return item_type in {"tool_search_call", "tool_search_output"}


def _extract_call_id(item: Any) -> str | None:
    """Return a tool call id from mapping or object payloads."""
    call_id = item.get("call_id") if isinstance(item, dict) else getattr(item, "call_id", None)
    return call_id if isinstance(call_id, str) else None


def _has_output_payload(item: Any) -> bool:
    """Return True when an item carries a local tool output payload."""
    return (isinstance(item, dict) and "output" in item) or hasattr(item, "output")


def _is_tracked_object(items: Sequence[Any], candidate: Any) -> bool:
    """Return True when the exact object instance is already tracked."""
    return any(item is candidate for item in items)


def _track_object_once(items: list[Any], candidate: Any) -> None:
    """Track an object instance once, keeping it alive while identity dedupe is needed."""
    if not _is_tracked_object(items, candidate):
        items.append(candidate)


def _untrack_object(items: list[Any], candidate: Any) -> None:
    """Remove an object instance from an identity-tracking list."""
    for index, item in enumerate(items):
        if item is candidate:
            items.pop(index)
            return


@dataclass
class OpenAIServerConversationTracker:
    """Track server-side conversation state for conversation-aware runs.

    This tracker keeps three complementary views of what has already been acknowledged:

    - Object identity for prepared items in the current Python process.
    - Stable server item IDs and tool call IDs returned by the provider.
    - Content fingerprints for retry/resume paths where object identity changes.

    The runner uses these sets together to decide which deltas are still safe to send when a
    run is resumed, retried after a transient failure, or rebuilt from serialized RunState.
    """

    conversation_id: str | None = None
    previous_response_id: str | None = None
    auto_previous_response_id: bool = False

    # In-process object identity for delivered or acknowledged items. Keep object references
    # instead of id(obj) integers so a later allocation cannot reuse a stale address.
    sent_items: list[Any] = field(default_factory=list)
    server_items: list[Any] = field(default_factory=list)

    # Stable provider identifiers returned by the Responses API.
    server_item_ids: set[str] = field(default_factory=set)
    server_tool_call_ids: set[str] = field(default_factory=set)
    server_output_fingerprints: set[str] = field(default_factory=set)

    # Content-based dedupe for resume/retry paths where objects are reconstructed.
    sent_item_fingerprints: set[str] = field(default_factory=set)
    restored_anonymous_tool_search_fingerprints: set[str] = field(default_factory=set)
    sent_initial_input: bool = False
    remaining_initial_input: list[TResponseInputItem] | None = None
    primed_from_state: bool = False
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None

    # Mapping from normalized prepared items back to their original source objects so that
    # mark_input_as_sent() can mark the right object identities after the model call succeeds.
    prepared_item_sources: dict[int, TResponseInputItem] = field(default_factory=dict)
    prepared_item_sources_by_fingerprint: dict[str, list[TResponseInputItem]] = field(
        default_factory=dict
    )

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
        unsent_tool_call_ids: set[str] | None = None,
    ) -> None:
        """Seed tracking from prior state so resumed runs do not replay already-sent content.

        This reconstructs the tracker from the original input, saved model responses, generated
        run items, and optional session history. After hydration, retry logic can treat rebuilt
        items as already acknowledged even though their Python object identities may differ from
        the original run.
        """
        if self.sent_initial_input:
            return
        unsent_tool_call_ids = unsent_tool_call_ids or set()

        normalized_input = original_input
        if isinstance(original_input, list):
            normalized_input = prepare_model_input_items(original_input)

        # Hydrated initial input is reconstructed during resume, so object identity is not a
        # stable dedupe key and can later collide with unrelated freshly allocated items.
        for item in ItemHelpers.input_to_new_input_list(normalized_input):
            if item is None:
                continue
            item_id = _normalize_server_item_id(
                item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
            )
            if item_id is not None:
                self.server_item_ids.add(item_id)
            fp = _fingerprint_for_tracker(item)
            if fp:
                self.sent_item_fingerprints.add(fp)
            anonymous_tool_search_fp = _anonymous_tool_search_fingerprint(item)
            if anonymous_tool_search_fp:
                self.restored_anonymous_tool_search_fingerprints.add(anonymous_tool_search_fp)

        self.sent_initial_input = True
        self.remaining_initial_input = None

        # Pick the most recent response that actually carries an id; live runs preserve the
        # last-known id via track_server_items, so resume must mirror that behavior instead of
        # blindly using model_responses[-1] (which may have response_id=None for non-Responses
        # providers and would silently break the chain).
        latest_response_id: str | None = None
        for response in model_responses:
            if response.response_id is not None:
                latest_response_id = response.response_id
            for output_item in response.output:
                if output_item is None:
                    continue
                _track_object_once(self.server_items, output_item)
                item_id = _normalize_server_item_id(
                    output_item.get("id")
                    if isinstance(output_item, dict)
                    else getattr(output_item, "id", None)
                )
                if item_id is not None:
                    self.server_item_ids.add(item_id)
                call_id = _extract_call_id(output_item)
                has_output_payload = _has_output_payload(output_item)
                if isinstance(call_id, str) and has_output_payload:
                    self.server_tool_call_ids.add(call_id)

        if self.conversation_id is None and latest_response_id is not None:
            self.previous_response_id = latest_response_id

        if session_items:
            for item in session_items:
                item_id = _normalize_server_item_id(
                    item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                )
                if item_id is not None:
                    self.server_item_ids.add(item_id)
                call_id = _extract_call_id(item)
                has_output = _has_output_payload(item)
                if isinstance(call_id, str) and has_output:
                    self.server_tool_call_ids.add(call_id)
                fp = _fingerprint_for_tracker(item)
                if fp:
                    self.sent_item_fingerprints.add(fp)
                anonymous_tool_search_fp = _anonymous_tool_search_fingerprint(item)
                if anonymous_tool_search_fp:
                    self.restored_anonymous_tool_search_fingerprints.add(anonymous_tool_search_fp)
        for item in generated_items:  # type: ignore[assignment]
            run_item: RunItem = cast(RunItem, item)
            raw_item = run_item.raw_item
            if raw_item is None:
                continue
            is_tool_call_item = run_item.type in {"tool_call_item", "handoff_call_item"}
            is_tool_search_item = run_item.type in {
                "tool_search_call_item",
                "tool_search_output_item",
            }

            if isinstance(raw_item, dict):
                item_id = _normalize_server_item_id(raw_item.get("id"))
                call_id = _extract_call_id(raw_item)
                has_output_payload = _has_output_payload(raw_item)
                has_call_id = isinstance(call_id, str)
                if (
                    isinstance(call_id, str)
                    and has_output_payload
                    and call_id in unsent_tool_call_ids
                ):
                    continue
                should_mark = (
                    item_id is not None
                    or (has_call_id and (has_output_payload or is_tool_call_item))
                    or is_tool_search_item
                )
                if not should_mark:
                    continue

                _track_object_once(self.sent_items, raw_item)
                fp = _fingerprint_for_tracker(raw_item)
                if fp:
                    self.sent_item_fingerprints.add(fp)
                    if is_tool_search_item:
                        self.server_output_fingerprints.add(fp)
                anonymous_tool_search_fp = _anonymous_tool_search_fingerprint(raw_item)
                if anonymous_tool_search_fp:
                    self.restored_anonymous_tool_search_fingerprints.add(anonymous_tool_search_fp)

                if item_id is not None:
                    self.server_item_ids.add(item_id)
                if isinstance(call_id, str) and has_output_payload:
                    self.server_tool_call_ids.add(call_id)
            else:
                item_id = _normalize_server_item_id(getattr(raw_item, "id", None))
                call_id = _extract_call_id(raw_item)
                has_output_payload = _has_output_payload(raw_item)
                has_call_id = isinstance(call_id, str)
                if (
                    isinstance(call_id, str)
                    and has_output_payload
                    and call_id in unsent_tool_call_ids
                ):
                    continue
                should_mark = (
                    item_id is not None
                    or (has_call_id and (has_output_payload or is_tool_call_item))
                    or is_tool_search_item
                )
                if not should_mark:
                    continue

                _track_object_once(self.sent_items, raw_item)
                fp = _fingerprint_for_tracker(raw_item)
                if fp:
                    self.sent_item_fingerprints.add(fp)
                    if is_tool_search_item:
                        self.server_output_fingerprints.add(fp)
                anonymous_tool_search_fp = _anonymous_tool_search_fingerprint(raw_item)
                if anonymous_tool_search_fp:
                    self.restored_anonymous_tool_search_fingerprints.add(anonymous_tool_search_fp)
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
            _track_object_once(self.server_items, output_item)
            item_id = _normalize_server_item_id(
                output_item.get("id")
                if isinstance(output_item, dict)
                else getattr(output_item, "id", None)
            )
            if item_id is not None:
                self.server_item_ids.add(item_id)
            call_id = _extract_call_id(output_item)
            has_output_payload = _has_output_payload(output_item)
            if isinstance(call_id, str) and has_output_payload:
                self.server_tool_call_ids.add(call_id)
            fp = _fingerprint_for_tracker(output_item)
            if fp:
                self.sent_item_fingerprints.add(fp)
                server_item_fingerprints.add(fp)
                if _is_tool_search_item(output_item):
                    self.server_output_fingerprints.add(fp)

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

        delivered_sources: list[TResponseInputItem] = []
        delivered_by_content: set[str] = set()
        for item in items:
            if item is None:
                continue
            source_item = self._consume_prepared_item_source(item)
            if _is_tracked_object(delivered_sources, source_item):
                continue
            delivered_sources.append(source_item)
            _track_object_once(self.sent_items, source_item)
            fp = _fingerprint_for_tracker(source_item)
            if fp:
                delivered_by_content.add(fp)
                self.sent_item_fingerprints.add(fp)

        if not self.remaining_initial_input:
            return

        remaining: list[TResponseInputItem] = []
        for pending in self.remaining_initial_input:
            if _is_tracked_object(delivered_sources, pending):
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
            source_item = self._consume_prepared_item_source(item)
            rewind_items.append(source_item)
            _untrack_object(self.sent_items, source_item)
            fp = _fingerprint_for_tracker(source_item)
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
        prepared_initial_items: list[TResponseInputItem] = []
        prepared_generated_items: list[TResponseInputItem] = []
        generated_item_sources: dict[int, TResponseInputItem] = {}

        if not self.sent_initial_input:
            initial_items = ItemHelpers.input_to_new_input_list(original_input)
            prepared_initial_items = normalize_input_items_for_api(initial_items)
            for prepared_item, source_item in zip(
                prepared_initial_items, initial_items, strict=False
            ):
                self._register_prepared_item_source(prepared_item, source_item)
            filtered_initials = []
            for item in initial_items:
                if item is None or isinstance(item, str | bytes):
                    continue
                filtered_initials.append(item)
            self.remaining_initial_input = filtered_initials or None
            self.sent_initial_input = True
        elif self.remaining_initial_input:
            prepared_initial_items = normalize_input_items_for_api(self.remaining_initial_input)
            for prepared_item, source_item in zip(
                prepared_initial_items, self.remaining_initial_input, strict=False
            ):
                self._register_prepared_item_source(prepared_item, source_item)

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

            call_id = _extract_call_id(raw_item)
            has_output_payload = _has_output_payload(raw_item)
            if (
                isinstance(call_id, str)
                and has_output_payload
                and call_id in self.server_tool_call_ids
            ):
                continue

            if _is_tracked_object(self.sent_items, raw_item) or _is_tracked_object(
                self.server_items, raw_item
            ):
                continue

            converted_input_item = run_item_to_input_item(run_item, self.reasoning_item_id_policy)
            if converted_input_item is None:
                continue
            fp = _fingerprint_for_tracker(converted_input_item)
            if fp and fp in self.server_output_fingerprints:
                continue
            if fp and self.primed_from_state and fp in self.sent_item_fingerprints:
                continue
            anonymous_tool_search_fp = _anonymous_tool_search_fingerprint(converted_input_item)
            if (
                self.primed_from_state
                and anonymous_tool_search_fp
                and item_id is None
                and not isinstance(call_id, str)
                and anonymous_tool_search_fp in self.restored_anonymous_tool_search_fingerprints
            ):
                continue

            prepared_generated_items.append(converted_input_item)
            generated_item_sources[id(converted_input_item)] = cast(TResponseInputItem, raw_item)

        normalized_generated_items = normalize_input_items_for_api(prepared_generated_items)
        normalized_generated_sources = {
            id(normalized_item): generated_item_sources[id(source_item)]
            for normalized_item, source_item in zip(
                normalized_generated_items, prepared_generated_items, strict=False
            )
        }
        filtered_generated_items = drop_orphan_function_calls(normalized_generated_items)
        for item in filtered_generated_items:
            prepared_source_item = normalized_generated_sources.get(id(item))
            if prepared_source_item is not None:
                self._register_prepared_item_source(item, prepared_source_item)

        return prepared_initial_items + filtered_generated_items

    def _register_prepared_item_source(
        self, prepared_item: TResponseInputItem, source_item: TResponseInputItem | None = None
    ) -> None:
        if source_item is None:
            source_item = prepared_item
        self.prepared_item_sources[id(prepared_item)] = source_item
        fingerprint = _fingerprint_for_tracker(prepared_item)
        if fingerprint:
            self.prepared_item_sources_by_fingerprint.setdefault(fingerprint, []).append(
                source_item
            )

    def _resolve_prepared_item_source(self, item: TResponseInputItem) -> TResponseInputItem:
        source_item = self.prepared_item_sources.get(id(item))
        if source_item is not None:
            return source_item

        fingerprint = _fingerprint_for_tracker(item)
        if not fingerprint:
            return item

        source_items = self.prepared_item_sources_by_fingerprint.get(fingerprint)
        if not source_items:
            return item
        return source_items[0]

    def _consume_prepared_item_source(self, item: TResponseInputItem) -> TResponseInputItem:
        source_item = self._resolve_prepared_item_source(item)
        direct_source = self.prepared_item_sources.pop(id(item), None)

        fingerprint = _fingerprint_for_tracker(item)
        if not fingerprint:
            return source_item

        source_items = self.prepared_item_sources_by_fingerprint.get(fingerprint)
        if not source_items:
            return source_item

        target_source = direct_source if direct_source is not None else source_item
        for index, candidate in enumerate(source_items):
            if candidate is target_source:
                source_items.pop(index)
                break
        else:
            source_items.pop(0)

        if not source_items:
            self.prepared_item_sources_by_fingerprint.pop(fingerprint, None)

        return source_item

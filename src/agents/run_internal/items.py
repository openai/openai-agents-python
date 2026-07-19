"""
Item utilities for the run pipeline. Hosts input normalization helpers and lightweight builders
for synthetic run items or IDs used during tool execution. Internal use only.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast
from uuid import uuid4

from openai.types.responses import ResponseFunctionToolCall
from pydantic import BaseModel

from ..agent_tool_state import drop_agent_tool_run_result
from ..items import ItemHelpers, RunItem, ToolCallOutputItem, TResponseInputItem
from ..models.fake_id import FAKE_RESPONSES_ID
from ..tool import DEFAULT_APPROVAL_REJECTION_MESSAGE

REJECTION_MESSAGE = DEFAULT_APPROVAL_REJECTION_MESSAGE
TOOL_CALL_SESSION_DESCRIPTION_KEY = "_agents_tool_description"
TOOL_CALL_SESSION_TITLE_KEY = "_agents_tool_title"
_NESTED_HISTORY_RUN_ITEM_OCCURRENCE_KEY = "_agents_nested_history_occurrence_key"
_TOOL_CALL_TO_OUTPUT_TYPE: dict[str, str] = {
    "program": "program_output",
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
    "shell_call": "shell_call_output",
    "apply_patch_call": "apply_patch_call_output",
    "computer_call": "computer_call_output",
    "local_shell_call": "local_shell_call_output",
    "tool_search_call": "tool_search_output",
}
_PROGRAM_OWNED_HOSTED_ITEM_TYPES = frozenset(
    {
        "hosted_tool_call",
        "file_search_call",
        "web_search_call",
        "code_interpreter_call",
        "image_generation_call",
        "mcp_list_tools",
        "mcp_call",
        "mcp_approval_request",
        "mcp_approval_response",
    }
)

__all__ = [
    "NestedHistoryOwnedItemRef",
    "NestedHistoryOwnedItem",
    "ReasoningItemIdPolicy",
    "REJECTION_MESSAGE",
    "TOOL_CALL_SESSION_DESCRIPTION_KEY",
    "TOOL_CALL_SESSION_TITLE_KEY",
    "copy_input_items",
    "drop_orphan_function_calls",
    "ensure_input_item_format",
    "prepare_model_input_items",
    "run_item_to_input_item",
    "run_items_to_input_items",
    "normalize_input_items_for_api",
    "normalize_resumed_input",
    "fingerprint_input_item",
    "digest_input_item",
    "ensure_nested_history_run_item_occurrence_key",
    "nested_history_run_item_occurrence_key",
    "reconcile_nested_history_owned_input_after_rewrite",
    "filter_nested_history_owned_item_refs_for_input",
    "rebase_nested_history_owned_item_refs",
    "resolve_nested_history_owned_item_indexes",
    "deduplicate_input_items",
    "deduplicate_input_items_preferring_latest",
    "strip_internal_input_item_metadata",
    "function_rejection_item",
    "shell_rejection_item",
    "apply_patch_rejection_item",
    "extract_mcp_request_id",
    "extract_mcp_request_id_from_run",
]


@dataclass(frozen=True)
class NestedHistoryOwnedItem:
    """A run item and the exact nested-input occurrence that represents it."""

    run_item: RunItem | None
    input_index: int
    digest: str
    input_item: TResponseInputItem | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class NestedHistoryOwnedItemRef:
    """Durable coordinates plus the live object for one owned session occurrence."""

    session_index: int
    digest: str
    input_index: int
    run_item: RunItem | None = field(default=None, compare=False, repr=False)
    input_item: TResponseInputItem | None = field(default=None, compare=False, repr=False)


def nested_history_run_item_occurrence_key(run_item: RunItem | None) -> str | None:
    """Return the private copy-lineage key for a run item, when one exists."""
    if run_item is None:
        return None
    key = getattr(run_item, _NESTED_HISTORY_RUN_ITEM_OCCURRENCE_KEY, None)
    return key if isinstance(key, str) and key else None


def ensure_nested_history_run_item_occurrence_key(run_item: RunItem) -> str:
    """Bind an ephemeral key that survives object copies but never enters model payloads."""
    key = nested_history_run_item_occurrence_key(run_item)
    if key is None:
        key = uuid4().hex
        setattr(run_item, _NESTED_HISTORY_RUN_ITEM_OCCURRENCE_KEY, key)
    return key


ReasoningItemIdPolicy = Literal["preserve", "omit"]


def copy_input_items(value: str | list[TResponseInputItem]) -> str | list[TResponseInputItem]:
    """Return a shallow copy of input items so mutations do not leak between turns."""
    return value if isinstance(value, str) else value.copy()


def run_item_to_input_item(
    run_item: RunItem,
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None,
) -> TResponseInputItem | None:
    """Convert a run item to model input, optionally stripping reasoning IDs."""
    if run_item.type == "tool_approval_item":
        return None
    to_input = getattr(run_item, "to_input_item", None)
    input_item = to_input() if callable(to_input) else cast(TResponseInputItem, run_item.raw_item)
    if isinstance(input_item, dict) and input_item.get("status") is None:
        input_item = {k: v for k, v in input_item.items() if k != "status"}
    if (
        _should_omit_reasoning_item_ids(reasoning_item_id_policy)
        and run_item.type == "reasoning_item"
    ):
        return _without_reasoning_item_id(input_item)
    return cast(TResponseInputItem, input_item)


def run_items_to_input_items(
    run_items: Sequence[RunItem],
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None,
) -> list[TResponseInputItem]:
    """Convert run items to model input items while skipping approvals."""
    converted: list[TResponseInputItem] = []
    for run_item in run_items:
        item = run_item_to_input_item(run_item, reasoning_item_id_policy)
        if item is not None:
            converted.append(item)
    return converted


def drop_orphan_function_calls(
    items: list[TResponseInputItem],
    *,
    pruning_indexes: set[int] | None = None,
) -> list[TResponseInputItem]:
    """
    Remove tool and program call items that do not have corresponding outputs so resumptions or
    retries do not replay stale calls. Program-owned items are removed with an orphan program,
    while programs with retained hosted calls or tool outputs remain available for continuation.
    Reasoning items that immediately precede a call dropped by this pass are also removed, since
    the Responses API rejects reasoning items that are not followed by their associated
    model-emitted item (``Item 'rs_...' of type 'reasoning' was provided without its required
    following item``).
    """

    completed_call_ids = _completed_call_ids_by_type(items)
    matched_anonymous_tool_search_calls = _matched_anonymous_tool_search_call_indexes(items)
    active_program_call_ids: set[str] = set()
    orphan_program_call_ids: set[str] = set()

    for index, entry in enumerate(items):
        if pruning_indexes is not None and index not in pruning_indexes:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "program":
            continue
        call_id = entry.get("call_id")
        if not isinstance(call_id, str):
            continue
        if call_id in completed_call_ids["program_output"]:
            continue
        if any(
            _get_program_caller_id(candidate) == call_id
            and _is_retained_program_owned_item(candidate, candidate_index, pruning_indexes)
            for candidate_index, candidate in enumerate(items)
        ):
            active_program_call_ids.add(call_id)
        else:
            orphan_program_call_ids.add(call_id)

    dropped_indexes: set[int] = set()
    filtered: list[TResponseInputItem] = []
    for index, entry in enumerate(items):
        if not isinstance(entry, dict):
            filtered.append(entry)
            continue
        entry_type = entry.get("type")
        if not isinstance(entry_type, str):
            filtered.append(entry)
            continue
        if pruning_indexes is not None and index not in pruning_indexes:
            filtered.append(entry)
            continue
        program_caller_id = _get_program_caller_id(entry)
        if program_caller_id is not None and program_caller_id in orphan_program_call_ids:
            dropped_indexes.add(index)
            continue
        output_type = _TOOL_CALL_TO_OUTPUT_TYPE.get(entry_type)
        if output_type is None:
            filtered.append(entry)
            continue
        call_id = entry.get("call_id")
        if program_caller_id is not None and _is_pending_hosted_shell_call(entry):
            filtered.append(entry)
            continue
        if entry_type == "program" and call_id in active_program_call_ids:
            filtered.append(entry)
            continue
        if isinstance(call_id, str) and call_id in completed_call_ids.get(output_type, set()):
            filtered.append(entry)
            continue
        if (
            entry_type == "tool_search_call"
            and not isinstance(call_id, str)
            and index in matched_anonymous_tool_search_calls
        ):
            filtered.append(entry)
            continue
        # Tool call entry will be dropped; record so we can also drop preceding reasoning items.
        dropped_indexes.add(index)

    if not dropped_indexes:
        return filtered
    return _drop_reasoning_items_preceding_dropped_calls(items, dropped_indexes)


def _drop_reasoning_items_preceding_dropped_calls(
    items: list[TResponseInputItem],
    dropped_indexes: set[int],
) -> list[TResponseInputItem]:
    """Drop reasoning items whose tied tool call was just dropped as orphan.

    A reasoning item is considered tied to the next non-reasoning model-emitted item. If that
    item was dropped, the reasoning item is now dangling and would be rejected by the Responses
    API with ``reasoning was provided without its required following item``.
    """
    drop_reasoning: set[int] = set()
    for index in range(len(items) - 1, -1, -1):
        entry = items[index]
        if (
            not isinstance(entry, dict)
            or entry.get("type") != "reasoning"
            or index in dropped_indexes
        ):
            continue
        for next_index in range(index + 1, len(items)):
            if next_index in drop_reasoning:
                continue
            next_entry = items[next_index]
            if isinstance(next_entry, dict) and next_entry.get("type") == "reasoning":
                continue
            if next_index in dropped_indexes:
                drop_reasoning.add(index)
            break
    excluded = dropped_indexes | drop_reasoning
    return [entry for idx, entry in enumerate(items) if idx not in excluded]


def ensure_input_item_format(item: TResponseInputItem) -> TResponseInputItem:
    """Ensure a single item is normalized for model input."""
    coerced = _coerce_to_dict(item)
    if coerced is None:
        return item

    return cast(TResponseInputItem, coerced)


def normalize_input_items_for_api(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    """Normalize input items for API submission."""

    normalized: list[TResponseInputItem] = []
    for item in items:
        coerced = _coerce_to_dict(item)
        if coerced is None:
            normalized.append(item)
            continue

        normalized_item = strip_internal_input_item_metadata(cast(TResponseInputItem, coerced))
        normalized.append(normalized_item)
    return normalized


def prepare_model_input_items(
    caller_items: Sequence[TResponseInputItem],
    generated_items: Sequence[TResponseInputItem] = (),
) -> list[TResponseInputItem]:
    """Normalize model input while pruning orphans only from runner-generated history."""
    normalized_caller_items = normalize_input_items_for_api(list(caller_items))
    if not generated_items:
        return normalized_caller_items

    normalized_generated_items = normalize_input_items_for_api(list(generated_items))
    filtered_generated_items = drop_orphan_function_calls(normalized_generated_items)
    return normalized_caller_items + filtered_generated_items


def normalize_resumed_input(
    raw_input: str | list[TResponseInputItem],
) -> str | list[TResponseInputItem]:
    """Normalize resumed list inputs and drop orphan tool calls."""
    if isinstance(raw_input, list):
        normalized = normalize_input_items_for_api(raw_input)
        return drop_orphan_function_calls(normalized)
    return raw_input


def fingerprint_input_item(item: Any, *, ignore_ids_for_matching: bool = False) -> str | None:
    """Hashable fingerprint used to dedupe or rewind input items across resumes."""
    if item is None:
        return None

    try:
        payload: Any
        if hasattr(item, "model_dump"):
            payload = _model_dump_without_warnings(item)
            if payload is None:
                return None
            if isinstance(payload, dict):
                payload = cast(
                    dict[str, Any],
                    strip_internal_input_item_metadata(cast(TResponseInputItem, payload)),
                )
        elif isinstance(item, dict):
            payload = cast(
                dict[str, Any],
                strip_internal_input_item_metadata(cast(TResponseInputItem, item)),
            )
            if ignore_ids_for_matching:
                payload.pop("id", None)
        else:
            payload = ensure_input_item_format(item)
            if isinstance(payload, dict):
                payload = cast(
                    dict[str, Any],
                    strip_internal_input_item_metadata(cast(TResponseInputItem, payload)),
                )
            if ignore_ids_for_matching and isinstance(payload, dict):
                payload.pop("id", None)

        return json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        return None


def digest_input_item(item: Any) -> str | None:
    """Return a fixed-size digest of an input item for durable occurrence tracking."""
    coerced = _coerce_to_dict(item)
    if coerced is not None:
        coerced = cast(
            dict[str, Any],
            strip_internal_input_item_metadata(cast(TResponseInputItem, coerced)),
        )
        if coerced.get("role") == "assistant":
            content = coerced.get("content")
            if isinstance(content, str):
                coerced["content"] = [{"type": "output_text", "text": content}]
            if coerced.get("status") in {None, "completed"}:
                coerced.pop("status", None)
        item = coerced

    fingerprint = fingerprint_input_item(item)
    if fingerprint is None:
        return None
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def filter_nested_history_owned_item_refs_for_input(
    input: str | Sequence[TResponseInputItem],
    owned_item_refs: Sequence[NestedHistoryOwnedItemRef],
) -> list[NestedHistoryOwnedItemRef]:
    """Keep ownership whose exact clean input occurrence is still present."""
    if isinstance(input, str) or not owned_item_refs:
        return []

    input_digests = [digest_input_item(item) for item in input]
    input_indexes_by_identity: dict[tuple[int, str], deque[int]] = {}
    for index, (item, digest) in enumerate(zip(input, input_digests, strict=True)):
        if digest is not None:
            input_indexes_by_identity.setdefault((id(item), digest), deque()).append(index)

    retained: list[NestedHistoryOwnedItemRef] = []
    used_input_indexes: set[int] = set()

    def _take_unused(candidates: deque[int] | None) -> int | None:
        while candidates:
            candidate = candidates.popleft()
            if candidate not in used_input_indexes:
                return candidate
        return None

    for item_ref in owned_item_refs:
        input_index = (
            _take_unused(input_indexes_by_identity.get((id(item_ref.input_item), item_ref.digest)))
            if item_ref.input_item is not None
            else None
        )
        if input_index is None and item_ref.input_item is None:
            candidate_index = item_ref.input_index
            if (
                0 <= candidate_index < len(input)
                and candidate_index not in used_input_indexes
                and input_digests[candidate_index] == item_ref.digest
            ):
                input_index = candidate_index
        if input_index is None:
            continue
        used_input_indexes.add(input_index)
        retained.append(replace(item_ref, input_index=input_index, input_item=input[input_index]))
    return retained


def reconcile_nested_history_owned_input_after_rewrite(
    previous_input: str | Sequence[TResponseInputItem],
    rewritten_input: str | Sequence[TResponseInputItem],
    owned_item_refs: Sequence[NestedHistoryOwnedItemRef],
) -> tuple[str | list[TResponseInputItem], list[NestedHistoryOwnedItemRef]]:
    """Rebind ownership after an unambiguous input rewrite."""
    if isinstance(rewritten_input, str) or not owned_item_refs:
        return (
            rewritten_input if isinstance(rewritten_input, str) else list(rewritten_input),
            [],
        )
    if isinstance(previous_input, str):
        return list(rewritten_input), []

    rewritten = list(rewritten_input)
    previous = list(previous_input)
    previous_digests = [digest_input_item(item) for item in previous]
    rewritten_digests = [digest_input_item(item) for item in rewritten]
    previous_digest_counts: dict[str, int] = {}
    rewritten_digest_counts: dict[str, int] = {}
    previous_identity_digests: set[tuple[int, str]] = set()
    rewritten_indexes_by_identity: dict[tuple[int, str], deque[int]] = {}
    rewritten_indexes_by_digest: dict[str, deque[int]] = {}
    for item, digest in zip(previous, previous_digests, strict=True):
        if digest is None:
            continue
        previous_digest_counts[digest] = previous_digest_counts.get(digest, 0) + 1
        previous_identity_digests.add((id(item), digest))
    for index, (item, digest) in enumerate(zip(rewritten, rewritten_digests, strict=True)):
        if digest is None:
            continue
        rewritten_digest_counts[digest] = rewritten_digest_counts.get(digest, 0) + 1
        rewritten_indexes_by_identity.setdefault((id(item), digest), deque()).append(index)
        rewritten_indexes_by_digest.setdefault(digest, deque()).append(index)

    recoverable_ref_counts: dict[str, int] = {}
    for item_ref in owned_item_refs:
        if (
            item_ref.input_item is not None
            and (id(item_ref.input_item), item_ref.digest) in previous_identity_digests
        ):
            recoverable_ref_counts[item_ref.digest] = (
                recoverable_ref_counts.get(item_ref.digest, 0) + 1
            )
    used_indexes: set[int] = set()
    used_digest_counts: dict[str, int] = {}
    retained: list[NestedHistoryOwnedItemRef] = []

    def _take_unused(candidates: deque[int] | None) -> int | None:
        while candidates:
            candidate = candidates.popleft()
            if candidate not in used_indexes:
                return candidate
        return None

    for item_ref in owned_item_refs:
        identity_match = (
            _take_unused(
                rewritten_indexes_by_identity.get((id(item_ref.input_item), item_ref.digest))
            )
            if item_ref.input_item is not None
            else None
        )
        if identity_match is not None:
            used_indexes.add(identity_match)
            used_digest_counts[item_ref.digest] = used_digest_counts.get(item_ref.digest, 0) + 1
            retained.append(
                replace(
                    item_ref,
                    input_index=identity_match,
                    input_item=rewritten[identity_match],
                )
            )
            continue

        previous_match = (
            item_ref.input_item is not None
            and (id(item_ref.input_item), item_ref.digest) in previous_identity_digests
        )
        previous_count = previous_digest_counts.get(item_ref.digest, 0)
        rewritten_count = rewritten_digest_counts.get(item_ref.digest, 0)
        all_equal_occurrences_owned = (
            previous_count == rewritten_count == recoverable_ref_counts.get(item_ref.digest, 0)
        )
        if (
            not previous_match
            or rewritten_count <= used_digest_counts.get(item_ref.digest, 0)
            or not ((previous_count == 1 and rewritten_count == 1) or all_equal_occurrences_owned)
        ):
            continue

        candidate_index = _take_unused(rewritten_indexes_by_digest.get(item_ref.digest))
        if candidate_index is None:
            continue
        used_indexes.add(candidate_index)
        used_digest_counts[item_ref.digest] = used_digest_counts.get(item_ref.digest, 0) + 1
        retained.append(
            replace(
                item_ref,
                input_index=candidate_index,
                input_item=rewritten[candidate_index],
            )
        )

    return rewritten, retained


def resolve_nested_history_owned_item_indexes(
    run_items: Sequence[RunItem],
    owned_item_refs: Sequence[NestedHistoryOwnedItemRef],
) -> set[int]:
    """Resolve ownership references without dropping a different item after list mutation."""
    if not owned_item_refs:
        return set()

    indexes_by_identity_digest: dict[tuple[int, str], deque[int]] = {}
    indexes_by_occurrence_digest: dict[tuple[str, str], deque[int]] = {}
    run_item_digests: list[str | None] = []
    for index, run_item in enumerate(run_items):
        input_item = run_item_to_input_item(run_item)
        digest = digest_input_item(input_item) if input_item is not None else None
        run_item_digests.append(digest)
        if digest is None:
            continue
        indexes_by_identity_digest.setdefault((id(run_item), digest), deque()).append(index)
        occurrence_key = nested_history_run_item_occurrence_key(run_item)
        if occurrence_key is not None:
            indexes_by_occurrence_digest.setdefault((occurrence_key, digest), deque()).append(index)

    resolved: set[int] = set()

    def _peek_unused(candidates: deque[int] | None) -> int | None:
        while candidates and candidates[0] in resolved:
            candidates.popleft()
        return candidates[0] if candidates else None

    for item_ref in owned_item_refs:
        occurrence_key = nested_history_run_item_occurrence_key(item_ref.run_item)
        stored_index = item_ref.session_index
        if (
            0 <= stored_index < len(run_items)
            and stored_index not in resolved
            and run_item_digests[stored_index] == item_ref.digest
            and item_ref.run_item is not None
            and (
                run_items[stored_index] is item_ref.run_item
                or (
                    occurrence_key is not None
                    and nested_history_run_item_occurrence_key(run_items[stored_index])
                    == occurrence_key
                )
            )
        ):
            resolved.add(stored_index)
            continue

        identity_index = (
            _peek_unused(indexes_by_identity_digest.get((id(item_ref.run_item), item_ref.digest)))
            if item_ref.run_item is not None
            else None
        )
        occurrence_index = (
            _peek_unused(indexes_by_occurrence_digest.get((occurrence_key, item_ref.digest)))
            if occurrence_key is not None
            else None
        )
        candidates = [index for index in (identity_index, occurrence_index) if index is not None]
        if candidates:
            resolved.add(min(candidates))

    return resolved


def rebase_nested_history_owned_item_refs(
    input: str | Sequence[TResponseInputItem],
    run_items: Sequence[RunItem],
    owned_item_refs: Sequence[NestedHistoryOwnedItemRef],
) -> list[NestedHistoryOwnedItemRef]:
    """Rebase surviving ownership onto exact live input and session occurrences."""
    retained_refs = filter_nested_history_owned_item_refs_for_input(input, owned_item_refs)
    indexes_by_identity_digest: dict[tuple[int, str], deque[int]] = {}
    indexes_by_occurrence_digest: dict[tuple[str, str], deque[int]] = {}
    for index, run_item in enumerate(run_items):
        input_item = run_item_to_input_item(run_item)
        digest = digest_input_item(input_item) if input_item is not None else None
        if digest is None:
            continue
        indexes_by_identity_digest.setdefault((id(run_item), digest), deque()).append(index)
        occurrence_key = nested_history_run_item_occurrence_key(run_item)
        if occurrence_key is not None:
            indexes_by_occurrence_digest.setdefault((occurrence_key, digest), deque()).append(index)

    rebased: list[NestedHistoryOwnedItemRef] = []
    used_indexes: set[int] = set()

    def _peek_unused(candidates: deque[int] | None) -> int | None:
        while candidates and candidates[0] in used_indexes:
            candidates.popleft()
        return candidates[0] if candidates else None

    for item_ref in retained_refs:
        occurrence_key = nested_history_run_item_occurrence_key(item_ref.run_item)
        identity_index = (
            _peek_unused(indexes_by_identity_digest.get((id(item_ref.run_item), item_ref.digest)))
            if item_ref.run_item is not None
            else None
        )
        occurrence_index = (
            _peek_unused(indexes_by_occurrence_digest.get((occurrence_key, item_ref.digest)))
            if occurrence_key is not None
            else None
        )
        candidates = [index for index in (identity_index, occurrence_index) if index is not None]
        if not candidates:
            continue
        index = min(candidates)
        used_indexes.add(index)
        rebased.append(replace(item_ref, session_index=index, run_item=run_items[index]))
    return rebased


def _dedupe_key(item: TResponseInputItem) -> str | None:
    """Return a stable identity key when items carry explicit identifiers."""
    payload = _coerce_to_dict(item)
    if payload is None:
        return None

    role = payload.get("role")
    item_type = payload.get("type") or role
    if role is not None or item_type == "message":
        return None
    item_id = payload.get("id")
    if item_id == FAKE_RESPONSES_ID:
        # Ignore placeholder IDs so call_id-based dedupe remains possible.
        item_id = None
    if isinstance(item_id, str):
        return f"id:{item_type}:{item_id}"

    call_id = payload.get("call_id")
    if isinstance(call_id, str):
        return f"call_id:{item_type}:{call_id}"

    # points back to the originating approval request ID on hosted MCP responses
    approval_request_id = payload.get("approval_request_id")
    if isinstance(approval_request_id, str):
        return f"approval_request_id:{item_type}:{approval_request_id}"

    return None


def strip_internal_input_item_metadata(item: TResponseInputItem) -> TResponseInputItem:
    """Remove SDK-only session metadata before sending items back to the model."""
    if not isinstance(item, dict):
        return item

    cleaned = dict(item)
    cleaned.pop(TOOL_CALL_SESSION_DESCRIPTION_KEY, None)
    cleaned.pop(TOOL_CALL_SESSION_TITLE_KEY, None)
    return cast(TResponseInputItem, cleaned)


def _should_omit_reasoning_item_ids(reasoning_item_id_policy: ReasoningItemIdPolicy | None) -> bool:
    return reasoning_item_id_policy == "omit"


def _without_reasoning_item_id(item: TResponseInputItem) -> TResponseInputItem:
    if not isinstance(item, dict):
        return item
    if item.get("type") != "reasoning":
        return item
    if "id" not in item:
        return item
    sanitized = dict(item)
    sanitized.pop("id", None)
    return cast(TResponseInputItem, sanitized)


def deduplicate_input_items(items: Sequence[TResponseInputItem]) -> list[TResponseInputItem]:
    """Remove duplicate items that share stable identifiers to avoid re-sending tool outputs."""
    seen_keys: set[str] = set()
    deduplicated: list[TResponseInputItem] = []
    for item in items:
        dedupe_key = _dedupe_key(item)
        if dedupe_key is None:
            deduplicated.append(item)
            continue
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        deduplicated.append(item)
    return deduplicated


def deduplicate_input_items_preferring_latest(
    items: Sequence[TResponseInputItem],
) -> list[TResponseInputItem]:
    """Deduplicate by stable identifiers while keeping the latest occurrence."""
    # deduplicate_input_items keeps the first item per dedupe key. Reverse twice so that
    # the latest item in the original order wins for duplicate IDs/call_ids.
    return list(reversed(deduplicate_input_items(list(reversed(items)))))


def function_rejection_item(
    agent: Any,
    tool_call: Any,
    *,
    rejection_message: str = REJECTION_MESSAGE,
    scope_id: str | None = None,
    tool_origin: Any = None,
) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected function tool call."""
    if isinstance(tool_call, ResponseFunctionToolCall):
        drop_agent_tool_run_result(tool_call, scope_id=scope_id)
    return ToolCallOutputItem(
        output=rejection_message,
        raw_item=ItemHelpers.tool_call_output_item(
            tool_call,
            rejection_message,
        ),
        agent=agent,
        tool_origin=tool_origin,
    )


def shell_rejection_item(
    agent: Any,
    call_id: str,
    *,
    tool_call: Any | None = None,
    rejection_message: str = REJECTION_MESSAGE,
) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected shell call."""
    rejection_output: dict[str, Any] = {
        "stdout": "",
        "stderr": rejection_message,
        "outcome": {"type": "exit", "exit_code": 1},
    }
    rejection_raw_item: dict[str, Any] = {
        "type": "shell_call_output",
        "call_id": call_id,
        "output": [rejection_output],
    }
    if tool_call is not None:
        ItemHelpers.copy_tool_call_caller(tool_call, rejection_raw_item)
    return ToolCallOutputItem(agent=agent, output=rejection_message, raw_item=rejection_raw_item)


def apply_patch_rejection_item(
    agent: Any,
    call_id: str,
    *,
    tool_call: Any | None = None,
    output_type: Literal["apply_patch_call_output", "custom_tool_call_output"] = (
        "apply_patch_call_output"
    ),
    rejection_message: str = REJECTION_MESSAGE,
) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected apply_patch call."""
    rejection_raw_item: dict[str, Any] = {
        "type": output_type,
        "call_id": call_id,
        "output": rejection_message,
    }
    if output_type == "apply_patch_call_output":
        rejection_raw_item["status"] = "failed"
    if tool_call is not None:
        ItemHelpers.copy_tool_call_caller(tool_call, rejection_raw_item)
    return ToolCallOutputItem(
        agent=agent,
        output=rejection_message,
        raw_item=rejection_raw_item,
    )


def extract_mcp_request_id(raw_item: Any) -> str | None:
    """Pull the request id from hosted MCP approval payloads."""
    if isinstance(raw_item, dict):
        provider_data = raw_item.get("provider_data")
        if isinstance(provider_data, dict):
            candidate = provider_data.get("id")
            if isinstance(candidate, str):
                return candidate
        candidate = raw_item.get("id") or raw_item.get("call_id")
        return candidate if isinstance(candidate, str) else None
    try:
        provider_data = getattr(raw_item, "provider_data", None)
    except Exception:
        provider_data = None
    if isinstance(provider_data, dict):
        candidate = provider_data.get("id")
        if isinstance(candidate, str):
            return candidate
    try:
        candidate = getattr(raw_item, "id", None) or getattr(raw_item, "call_id", None)
    except Exception:
        candidate = None
    return candidate if isinstance(candidate, str) else None


def extract_mcp_request_id_from_run(mcp_run: Any) -> str | None:
    """Extract the hosted MCP request id from a streaming run item."""
    request_item = getattr(mcp_run, "request_item", None) or getattr(mcp_run, "requestItem", None)
    if isinstance(request_item, dict):
        provider_data = request_item.get("provider_data")
        if isinstance(provider_data, dict):
            candidate = provider_data.get("id")
            if isinstance(candidate, str):
                return candidate
        candidate = request_item.get("id") or request_item.get("call_id")
    else:
        provider_data = getattr(request_item, "provider_data", None)
        if isinstance(provider_data, dict):
            candidate = provider_data.get("id")
            if isinstance(candidate, str):
                return candidate
        candidate = getattr(request_item, "id", None) or getattr(request_item, "call_id", None)
    return candidate if isinstance(candidate, str) else None


# --------------------------
# Private helpers
# --------------------------


def _completed_call_ids_by_type(payload: list[TResponseInputItem]) -> dict[str, set[str]]:
    """Return call ids that already have outputs, grouped by output type."""
    completed: dict[str, set[str]] = {
        output_type: set() for output_type in _TOOL_CALL_TO_OUTPUT_TYPE.values()
    }
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        item_type = entry.get("type")
        if not isinstance(item_type, str) or item_type not in completed:
            continue
        call_id = entry.get("call_id")
        if isinstance(call_id, str):
            completed[item_type].add(call_id)
    return completed


def _get_program_caller_id(entry: TResponseInputItem) -> str | None:
    """Return the owning program call id for a program-issued item."""
    if not isinstance(entry, dict):
        return None
    caller = entry.get("caller")
    if not isinstance(caller, dict) or caller.get("type") != "program":
        return None
    caller_id = caller.get("caller_id")
    return caller_id if isinstance(caller_id, str) else None


def _is_pending_hosted_shell_call(entry: TResponseInputItem) -> bool:
    """Return whether a hosted shell call can remain pending without an output item."""
    if not isinstance(entry, dict) or entry.get("type") != "shell_call":
        return False
    status = entry.get("status")
    return status is None or status == "in_progress"


def _is_retained_program_owned_item(
    entry: TResponseInputItem,
    index: int,
    pruning_indexes: set[int] | None,
) -> bool:
    """Return whether a program-owned item keeps its parent program active."""
    if pruning_indexes is not None:
        return index not in pruning_indexes
    if _is_pending_hosted_shell_call(entry):
        return True
    if not isinstance(entry, dict):
        return False
    entry_type = cast(dict[str, Any], entry).get("type")
    return (
        entry_type in _PROGRAM_OWNED_HOSTED_ITEM_TYPES
        or entry_type in _TOOL_CALL_TO_OUTPUT_TYPE.values()
    )


def _matched_anonymous_tool_search_call_indexes(payload: list[TResponseInputItem]) -> set[int]:
    """Return anonymous tool_search_call indexes that have a later anonymous output."""
    matched_indexes: set[int] = set()
    pending_anonymous_outputs = 0

    for index in range(len(payload) - 1, -1, -1):
        entry = payload[index]
        if not isinstance(entry, dict):
            continue

        item_type = entry.get("type")
        if item_type == "tool_search_output" and not isinstance(entry.get("call_id"), str):
            pending_anonymous_outputs += 1
            continue

        if (
            item_type == "tool_search_call"
            and not isinstance(entry.get("call_id"), str)
            and pending_anonymous_outputs > 0
        ):
            matched_indexes.add(index)
            pending_anonymous_outputs -= 1

    return matched_indexes


def _coerce_to_dict(value: object) -> dict[str, Any] | None:
    """Convert model items to dicts so fields can be renamed and sanitized."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, BaseModel):
        return _model_dump_without_warnings(value)
    if hasattr(value, "model_dump"):
        return _model_dump_without_warnings(value)
    return None


def _model_dump_without_warnings(value: object) -> dict[str, Any] | None:
    """Best-effort model_dump that avoids noisy serialization warnings from third-party models."""
    if not hasattr(value, "model_dump"):
        return None

    model_dump = cast(Any, value).model_dump
    try:
        return cast(dict[str, Any], model_dump(exclude_unset=True, warnings=False))
    except TypeError:
        # Some model_dump-compatible objects only accept exclude_unset.
        try:
            return cast(dict[str, Any], model_dump(exclude_unset=True))
        except Exception:
            return None
    except Exception:
        return None

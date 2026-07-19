from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from ..items import (
    ItemHelpers,
    RunItem,
    ToolApprovalItem,
    TResponseInputItem,
)

if TYPE_CHECKING:
    from ..run_internal.items import NestedHistoryOwnedItem
    from . import HandoffHistoryMapper, HandoffInputData

__all__ = [
    "default_handoff_history_mapper",
    "get_conversation_history_wrappers",
    "nest_handoff_history",
    "reset_conversation_history_wrappers",
    "set_conversation_history_wrappers",
]

_DEFAULT_CONVERSATION_HISTORY_START = "<CONVERSATION HISTORY>"
_DEFAULT_CONVERSATION_HISTORY_END = "</CONVERSATION HISTORY>"
_CONVERSATION_HISTORY_PREAMBLE = (
    "For context, here is the conversation so far between the user and the previous agent:"
)
_LEGACY_CONVERSATION_HISTORY_PREAMBLE = "For context, here is the conversation so far:"
_SUPPORTED_CONVERSATION_HISTORY_PREAMBLES = {
    _CONVERSATION_HISTORY_PREAMBLE,
    _LEGACY_CONVERSATION_HISTORY_PREAMBLE,
}
_conversation_history_start = _DEFAULT_CONVERSATION_HISTORY_START
_conversation_history_end = _DEFAULT_CONVERSATION_HISTORY_END

# Item types that are summarized in the conversation history.
# They should not be forwarded verbatim to the next agent to avoid duplication.
_SUMMARY_ONLY_INPUT_TYPES = {
    "function_call",
    "function_call_output",
    # Reasoning items can become orphaned after other summarized items are filtered.
    "reasoning",
}


def set_conversation_history_wrappers(
    *,
    start: str | None = None,
    end: str | None = None,
) -> None:
    """Override the markers that wrap the generated conversation summary.

    Pass ``None`` to leave either side unchanged.
    """

    global _conversation_history_start, _conversation_history_end
    if start is not None:
        _conversation_history_start = start
    if end is not None:
        _conversation_history_end = end


def reset_conversation_history_wrappers() -> None:
    """Restore the default ``<CONVERSATION HISTORY>`` markers."""

    global _conversation_history_start, _conversation_history_end
    _conversation_history_start = _DEFAULT_CONVERSATION_HISTORY_START
    _conversation_history_end = _DEFAULT_CONVERSATION_HISTORY_END


def get_conversation_history_wrappers() -> tuple[str, str]:
    """Return the current start/end markers used for the nested conversation summary."""

    return (_conversation_history_start, _conversation_history_end)


def nest_handoff_history(
    handoff_input_data: HandoffInputData,
    *,
    history_mapper: HandoffHistoryMapper | None = None,
) -> HandoffInputData:
    """Summarize the previous transcript for the next agent."""

    nested, _ = _nest_handoff_history_with_provenance(
        handoff_input_data,
        history_mapper=history_mapper,
    )
    return nested


def _nest_handoff_history_with_provenance(
    handoff_input_data: HandoffInputData,
    *,
    history_mapper: HandoffHistoryMapper | None = None,
) -> tuple[HandoffInputData, tuple[NestedHistoryOwnedItem, ...]]:
    """Return nested input and exact provenance for items moved into default history."""

    normalized_history = _normalize_input_history(handoff_input_data.input_history)
    flattened_history = [
        _strip_transcript_item_metadata(item)
        for item in _flatten_nested_history_messages(normalized_history)
    ]

    # Partition items between summary segments and lossless model input while retaining order.
    normalized_pre_items: list[tuple[TResponseInputItem, bool, RunItem]] = []
    for run_item in handoff_input_data.pre_handoff_items:
        if isinstance(run_item, ToolApprovalItem):
            continue
        plain_input = _run_item_to_plain_input(run_item)
        forward_verbatim = _should_forward_pre_item(plain_input)
        normalized_pre_items.append((plain_input, forward_verbatim, run_item))

    normalized_new_items: list[tuple[TResponseInputItem, bool, RunItem]] = []
    for run_item in handoff_input_data.new_items:
        if isinstance(run_item, ToolApprovalItem):
            continue
        plain_input = _run_item_to_plain_input(run_item)
        forward_verbatim = _should_forward_new_item(plain_input)
        normalized_new_items.append((plain_input, forward_verbatim, run_item))

    normalized_items = normalized_pre_items + normalized_new_items

    owned_items: list[NestedHistoryOwnedItem] = []
    if history_mapper is not None:
        transcript = flattened_history + [item for item, _, _ in normalized_items]
        history_items = history_mapper(transcript)
    else:
        history_items, owned_items = _build_ordered_default_history(
            flattened_history,
            normalized_items,
        )

    copied_history = [deepcopy(item) for item in history_items]
    owned_items = [
        replace(
            owned_item,
            input_item=copied_history[owned_item.input_index],
        )
        for owned_item in owned_items
    ]

    nested = handoff_input_data.clone(
        input_history=tuple(copied_history),
        pre_handoff_items=(),
        # The mapped history is the exact model input. New items stay unchanged for session
        # history.
        input_items=(),
    )
    object.__setattr__(nested, "_nested_history_owned_items", tuple(owned_items))

    return nested, tuple(owned_items)


def _get_nested_history_owned_items(
    handoff_input_data: HandoffInputData,
    *,
    source_data: HandoffInputData | None = None,
) -> tuple[NestedHistoryOwnedItem, ...]:
    """Match clean nested input occurrences to their source run items."""
    from ..run_internal.items import (
        NestedHistoryOwnedItem,
        digest_input_item,
    )

    if isinstance(handoff_input_data.input_history, str):
        return ()

    declared_items = tuple(
        item
        for item in getattr(handoff_input_data, "_nested_history_owned_items", ())
        if isinstance(item, NestedHistoryOwnedItem)
    )
    if not declared_items:
        return ()

    current_by_source_id: dict[int, RunItem] = {}
    if source_data is not None:
        current_items = (
            *handoff_input_data.pre_handoff_items,
            *handoff_input_data.new_items,
        )
        source_items = (*source_data.pre_handoff_items, *source_data.new_items)
        mapped_items = _map_run_item_occurrences(current_items, source_items)
        current_by_source_id = {
            id(source_item): current_item
            for current_item, source_item in zip(current_items, mapped_items, strict=True)
            if source_item is not None
        }

    input_digests = [digest_input_item(item) for item in handoff_input_data.input_history]
    input_digest_counts: dict[str, int] = {}
    input_indexes_by_identity: dict[tuple[int, str], deque[int]] = {}
    input_indexes_by_digest: dict[str, deque[int]] = {}
    for index, (item, digest) in enumerate(
        zip(handoff_input_data.input_history, input_digests, strict=True)
    ):
        if digest is not None:
            input_indexes_by_identity.setdefault((id(item), digest), deque()).append(index)
            input_indexes_by_digest.setdefault(digest, deque()).append(index)
            input_digest_counts[digest] = input_digest_counts.get(digest, 0) + 1
    owned_digest_counts: dict[str, int] = {}
    for owned_item in declared_items:
        owned_digest_counts[owned_item.digest] = owned_digest_counts.get(owned_item.digest, 0) + 1

    retained: list[NestedHistoryOwnedItem] = []
    used_input_indexes: set[int] = set()
    used_digest_counts: dict[str, int] = {}

    def _take_unused(candidates: deque[int] | None) -> int | None:
        while candidates:
            candidate = candidates.popleft()
            if candidate not in used_input_indexes:
                return candidate
        return None

    for owned_item in declared_items:
        input_index = None
        if owned_item.input_item is not None:
            input_index = _take_unused(
                input_indexes_by_identity.get((id(owned_item.input_item), owned_item.digest))
            )
        if input_index is None:
            remaining_digest_count = input_digest_counts.get(
                owned_item.digest, 0
            ) - used_digest_counts.get(owned_item.digest, 0)
            all_equal_occurrences_owned = (
                input_digest_counts.get(owned_item.digest, 0)
                == owned_digest_counts[owned_item.digest]
            )
            if remaining_digest_count == 1 or all_equal_occurrences_owned:
                if (
                    0 <= owned_item.input_index < len(input_digests)
                    and owned_item.input_index not in used_input_indexes
                    and input_digests[owned_item.input_index] == owned_item.digest
                ):
                    input_index = owned_item.input_index
                else:
                    input_index = _take_unused(input_indexes_by_digest.get(owned_item.digest))
        if input_index is None:
            continue
        used_input_indexes.add(input_index)
        used_digest_counts[owned_item.digest] = used_digest_counts.get(owned_item.digest, 0) + 1
        input_item = handoff_input_data.input_history[input_index]
        source_run_item = (
            current_by_source_id.get(id(owned_item.run_item), owned_item.run_item)
            if owned_item.run_item is not None
            else None
        )
        retained.append(
            replace(
                owned_item,
                run_item=source_run_item,
                input_index=input_index,
                input_item=input_item,
            )
        )
    return tuple(retained)


def _map_run_item_occurrences(
    current_items: tuple[RunItem, ...],
    source_items: tuple[RunItem, ...],
) -> list[RunItem | None]:
    """Map copied filtered items back to original handoff occurrences when possible."""
    if not source_items:
        return [None] * len(current_items)

    from ..run_internal.items import nested_history_run_item_occurrence_key

    source_indexes_by_identity: dict[int, deque[int]] = {}
    source_indexes_by_occurrence_key: dict[str, deque[int]] = {}
    for index, source_item in enumerate(source_items):
        source_indexes_by_identity.setdefault(id(source_item), deque()).append(index)
        occurrence_key = nested_history_run_item_occurrence_key(source_item)
        if occurrence_key is not None:
            source_indexes_by_occurrence_key.setdefault(occurrence_key, deque()).append(index)

    used_source_indexes: set[int] = set()
    mapped: list[RunItem | None] = []

    def _take_unused(candidates: deque[int] | None) -> int | None:
        while candidates:
            candidate = candidates.popleft()
            if candidate not in used_source_indexes:
                return candidate
        return None

    for current_item in current_items:
        source_index = _take_unused(
            source_indexes_by_identity.get(id(current_item)),
        )
        current_key = nested_history_run_item_occurrence_key(current_item)
        if source_index is None and current_key is not None:
            source_index = _take_unused(
                source_indexes_by_occurrence_key.get(current_key),
            )
        if source_index is None:
            mapped.append(None)
            continue
        used_source_indexes.add(source_index)
        mapped.append(source_items[source_index])
    return mapped


def default_handoff_history_mapper(
    transcript: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """Return a single assistant message summarizing the transcript."""

    summary_message = _build_summary_message(transcript)
    return [summary_message]


def _normalize_input_history(
    input_history: str | tuple[TResponseInputItem, ...],
) -> list[TResponseInputItem]:
    if isinstance(input_history, str):
        return ItemHelpers.input_to_new_input_list(input_history)
    return [deepcopy(item) for item in input_history]


def _run_item_to_plain_input(run_item: RunItem) -> TResponseInputItem:
    from ..run_internal.items import run_item_to_input_item

    input_item = run_item_to_input_item(run_item)
    if input_item is None:
        raise TypeError(f"Unsupported nested handoff run item: {run_item.type}")
    return deepcopy(input_item)


def _build_ordered_default_history(
    flattened_history: list[TResponseInputItem],
    normalized_items: list[tuple[TResponseInputItem, bool, RunItem]],
) -> tuple[list[TResponseInputItem], list[NestedHistoryOwnedItem]]:
    from ..run_internal.items import (
        NestedHistoryOwnedItem,
        digest_input_item,
        ensure_nested_history_run_item_occurrence_key,
    )

    history_items: list[TResponseInputItem] = []
    owned_items: list[NestedHistoryOwnedItem] = []
    pending_summary = list(flattened_history)

    for plain_input, forward_verbatim, run_item in normalized_items:
        if not forward_verbatim:
            pending_summary.append(plain_input)
            continue
        if pending_summary or not history_items:
            history_items.extend(default_handoff_history_mapper(pending_summary))
            pending_summary = []
        digest = digest_input_item(plain_input)
        if digest is not None:
            ensure_nested_history_run_item_occurrence_key(run_item)
            owned_items.append(
                NestedHistoryOwnedItem(
                    run_item=run_item,
                    input_index=len(history_items),
                    digest=digest,
                )
            )
        history_items.append(plain_input)

    if pending_summary or not history_items:
        history_items.extend(default_handoff_history_mapper(pending_summary))

    return history_items, owned_items


def _build_summary_message(transcript: list[TResponseInputItem]) -> TResponseInputItem:
    transcript_copy = [deepcopy(item) for item in transcript]
    if transcript_copy:
        summary_lines = [
            f"{idx + 1}. {_format_transcript_item(item)}"
            for idx, item in enumerate(transcript_copy)
        ]
    else:
        summary_lines = ["(no previous turns recorded)"]

    start_marker, end_marker = get_conversation_history_wrappers()
    content_lines = [
        _CONVERSATION_HISTORY_PREAMBLE,
        start_marker,
        *summary_lines,
        end_marker,
    ]
    content = "\n".join(content_lines)
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    return cast(TResponseInputItem, assistant_message)


def _format_transcript_item(item: TResponseInputItem) -> str:
    item = _strip_transcript_item_metadata(item)
    role = item.get("role")
    if isinstance(role, str):
        content = item.get("content")
        if content is None or (isinstance(content, str) and not _contains_newline(content)):
            return _format_transcript_item_legacy(item)
    return _format_transcript_item_json(item)


def _contains_newline(value: str) -> bool:
    return "\n" in value or "\r" in value


def _format_transcript_item_json(item: TResponseInputItem) -> str:
    payload = cast(dict[str, Any], deepcopy(item))
    payload.pop("provider_data", None)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return _format_transcript_item_legacy(item)


def _format_transcript_item_legacy(item: TResponseInputItem) -> str:
    role = item.get("role")
    if isinstance(role, str):
        prefix = role
        name = item.get("name")
        if isinstance(name, str) and name:
            prefix = f"{prefix} ({name})"
        content_str = _stringify_content(item.get("content"))
        return f"{prefix}: {content_str}" if content_str else prefix

    item_type = item.get("type", "item")
    rest = {k: v for k, v in item.items() if k not in ("type", "provider_data")}
    try:
        serialized = json.dumps(rest, ensure_ascii=False, default=str)
    except TypeError:
        serialized = str(rest)
    return f"{item_type}: {serialized}" if serialized else str(item_type)


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except TypeError:
        return str(content)


def _flatten_nested_history_messages(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    flattened: list[TResponseInputItem] = []
    for item in items:
        nested_transcript = _extract_nested_history_transcript(item)
        if nested_transcript is not None:
            flattened.extend(nested_transcript)
            continue
        flattened.append(deepcopy(item))
    return flattened


def _extract_nested_history_transcript(
    item: TResponseInputItem,
) -> list[TResponseInputItem] | None:
    if item.get("role") != "assistant":
        return None
    content = item.get("content")
    if not isinstance(content, str):
        return None
    start_marker, end_marker = get_conversation_history_wrappers()
    preamble, separator, wrapped_content = content.partition("\n")
    if not separator or preamble not in _SUPPORTED_CONVERSATION_HISTORY_PREAMBLES:
        return None
    start_wrapper = f"{start_marker}\n"
    end_wrapper = f"\n{end_marker}"
    if not wrapped_content.startswith(start_wrapper) or not wrapped_content.endswith(end_wrapper):
        return None
    body = wrapped_content[len(start_wrapper) : -len(end_wrapper)]
    parsed: list[TResponseInputItem] = []
    for line in _split_summary_records(body):
        parsed_item = _parse_summary_line(line)
        if parsed_item is not None:
            parsed.append(parsed_item)
    return parsed


def _split_summary_records(body: str) -> list[str]:
    records: list[str] = []
    current: list[str] = []
    current_is_numbered = False

    for raw_line in body.splitlines():
        if not raw_line.strip():
            continue

        starts_numbered_record = _starts_numbered_summary_record(raw_line)
        if not current:
            current = [raw_line.strip()]
            current_is_numbered = starts_numbered_record
            continue

        if starts_numbered_record or not current_is_numbered:
            records.append("\n".join(current))
            current = [raw_line.strip()]
            current_is_numbered = starts_numbered_record
            continue

        current.append(raw_line.rstrip())

    if current:
        records.append("\n".join(current))

    return records


def _starts_numbered_summary_record(line: str) -> bool:
    stripped = line.lstrip()
    dot_index = stripped.find(".")
    return dot_index != -1 and stripped[:dot_index].isdigit()


def _parse_summary_line(line: str) -> TResponseInputItem | None:
    stripped = line.strip()
    if not stripped:
        return None
    stripped = _strip_summary_line_number(stripped)
    parsed_json = _parse_summary_json_item(stripped)
    if parsed_json is not None:
        return parsed_json

    role_part, sep, remainder = stripped.partition(":")
    if not sep:
        return None
    role_text = role_part.strip()
    if not role_text:
        return None
    role, name = _split_role_and_name(role_text)
    reconstructed: dict[str, Any] = {"role": role}
    if name:
        reconstructed["name"] = name
    content = remainder.strip()
    if content:
        legacy_typed_item = _parse_legacy_typed_item(role, content)
        if legacy_typed_item is not None:
            return legacy_typed_item
        reconstructed["content"] = content
    return cast(TResponseInputItem, reconstructed)


def _strip_summary_line_number(stripped: str) -> str:
    dot_index = stripped.find(".")
    if dot_index != -1 and stripped[:dot_index].isdigit():
        return stripped[dot_index + 1 :].lstrip()
    return stripped


def _parse_summary_json_item(value: str) -> TResponseInputItem | None:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed.pop("provider_data", None)
    return _strip_transcript_item_metadata(cast(TResponseInputItem, parsed))


def _parse_legacy_typed_item(item_type: str, content: str) -> TResponseInputItem | None:
    if item_type in {"assistant", "user", "system", "developer"}:
        return None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed.pop("provider_data", None)
    parsed["type"] = item_type
    return _strip_transcript_item_metadata(cast(TResponseInputItem, parsed))


def _strip_transcript_item_metadata(item: TResponseInputItem) -> TResponseInputItem:
    """Remove SDK-only fields before nested transcript formatting or replay."""
    from ..run_internal.items import strip_internal_input_item_metadata

    return strip_internal_input_item_metadata(item)


def _split_role_and_name(role_text: str) -> tuple[str, str | None]:
    if role_text.endswith(")") and "(" in role_text:
        open_idx = role_text.rfind("(")
        possible_name = role_text[open_idx + 1 : -1].strip()
        role_candidate = role_text[:open_idx].strip()
        if possible_name:
            return (role_candidate or "developer", possible_name)
    return (role_text or "developer", None)


def _should_forward_pre_item(input_item: TResponseInputItem) -> bool:
    """Return False when the previous transcript item is represented in the summary."""
    if _is_programmatic_transcript_item(input_item):
        return False
    role_candidate = input_item.get("role")
    if isinstance(role_candidate, str) and role_candidate == "assistant":
        return False
    type_candidate = input_item.get("type")
    return not (isinstance(type_candidate, str) and type_candidate in _SUMMARY_ONLY_INPUT_TYPES)


def _should_forward_new_item(input_item: TResponseInputItem) -> bool:
    """Return False for tool or side-effect items that the summary already covers."""
    if _is_programmatic_transcript_item(input_item):
        return False
    # Items with a role should always be forwarded.
    role_candidate = input_item.get("role")
    if isinstance(role_candidate, str) and role_candidate:
        return True
    type_candidate = input_item.get("type")
    return not (isinstance(type_candidate, str) and type_candidate in _SUMMARY_ONLY_INPUT_TYPES)


def _is_programmatic_transcript_item(input_item: TResponseInputItem) -> bool:
    """Return whether an item belongs to an indivisible hosted-program transcript."""
    if input_item.get("type") in {"program", "program_output"}:
        return True

    caller = input_item.get("caller")
    if isinstance(caller, Mapping):
        return caller.get("type") == "program"
    return getattr(caller, "type", None) == "program"

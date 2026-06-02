from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from ..items import (
    ItemHelpers,
    RunItem,
    ToolApprovalItem,
    TResponseInputItem,
)

if TYPE_CHECKING:
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

    normalized_history = _normalize_input_history(handoff_input_data.input_history)
    flattened_history = _flatten_nested_history_messages(normalized_history)

    # Convert items to plain inputs for the transcript summary.
    pre_items_as_inputs: list[TResponseInputItem] = []
    filtered_pre_items: list[RunItem] = []
    for run_item in handoff_input_data.pre_handoff_items:
        if isinstance(run_item, ToolApprovalItem):
            continue
        plain_input = _run_item_to_plain_input(run_item)
        pre_items_as_inputs.append(plain_input)
        if _should_forward_pre_item(plain_input):
            filtered_pre_items.append(run_item)

    new_items_as_inputs: list[TResponseInputItem] = []
    filtered_input_items: list[RunItem] = []
    for run_item in handoff_input_data.new_items:
        if isinstance(run_item, ToolApprovalItem):
            continue
        plain_input = _run_item_to_plain_input(run_item)
        new_items_as_inputs.append(plain_input)
        if _should_forward_new_item(plain_input):
            filtered_input_items.append(run_item)

    transcript = flattened_history + pre_items_as_inputs + new_items_as_inputs

    mapper = history_mapper or default_handoff_history_mapper
    history_items = mapper(transcript)

    return handoff_input_data.clone(
        input_history=tuple(deepcopy(item) for item in history_items),
        pre_handoff_items=tuple(filtered_pre_items),
        # new_items stays unchanged for session history.
        input_items=tuple(filtered_input_items),
    )


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
    return deepcopy(run_item.to_input_item())


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
        "For context, here is the conversation so far between the user and the previous agent:",
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
    content = item.get("content")
    if not isinstance(content, str):
        return None
    start_marker, end_marker = get_conversation_history_wrappers()
    start_idx = content.find(start_marker)
    end_idx = content.rfind(end_marker)
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    start_idx += len(start_marker)
    body = content[start_idx:end_idx]
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
    return cast(TResponseInputItem, parsed)


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
    return cast(TResponseInputItem, parsed)


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
    role_candidate = input_item.get("role")
    if isinstance(role_candidate, str) and role_candidate == "assistant":
        return False
    type_candidate = input_item.get("type")
    return not (isinstance(type_candidate, str) and type_candidate in _SUMMARY_ONLY_INPUT_TYPES)


def _should_forward_new_item(input_item: TResponseInputItem) -> bool:
    """Return False for tool or side-effect items that the summary already covers."""
    # Items with a role should always be forwarded.
    role_candidate = input_item.get("role")
    if isinstance(role_candidate, str) and role_candidate:
        return True
    type_candidate = input_item.get("type")
    return not (isinstance(type_candidate, str) and type_candidate in _SUMMARY_ONLY_INPUT_TYPES)

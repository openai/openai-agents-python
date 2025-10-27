from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..handoffs import HandoffInputData
from ..items import (
    HandoffCallItem,
    HandoffOutputItem,
    ItemHelpers,
    ReasoningItem,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)

"""Contains common handoff input filters, for convenience. """


def remove_all_tools(handoff_input_data: HandoffInputData) -> HandoffInputData:
    """Filters out all tool items: file search, web search and function calls+output."""

    history = handoff_input_data.input_history
    new_items = handoff_input_data.new_items

    filtered_history = (
        _remove_tool_types_from_input(history) if isinstance(history, tuple) else history
    )
    filtered_pre_handoff_items = _remove_tools_from_items(handoff_input_data.pre_handoff_items)
    filtered_new_items = _remove_tools_from_items(new_items)

    return HandoffInputData(
        input_history=filtered_history,
        pre_handoff_items=filtered_pre_handoff_items,
        new_items=filtered_new_items,
        run_context=handoff_input_data.run_context,
    )


_CONVERSATION_HISTORY_START = "<CONVERSATION HISTORY>"
_CONVERSATION_HISTORY_END = "</CONVERSATION HISTORY>"
_NEST_HISTORY_METADATA_KEY = "nest_handoff_history"
_NEST_HISTORY_TRANSCRIPT_KEY = "transcript"


def nest_handoff_history(handoff_input_data: HandoffInputData) -> HandoffInputData:
    """Summarizes the previous transcript into a developer message for the next agent."""

    normalized_history = _normalize_input_history(handoff_input_data.input_history)
    flattened_history = _flatten_nested_history_messages(normalized_history)
    pre_items_as_inputs = [
        _run_item_to_plain_input(item) for item in handoff_input_data.pre_handoff_items
    ]
    new_items_as_inputs = [_run_item_to_plain_input(item) for item in handoff_input_data.new_items]
    transcript = flattened_history + pre_items_as_inputs + new_items_as_inputs

    developer_message = _build_developer_message(transcript)
    latest_user = _find_latest_user_turn(transcript)
    history_items: list[TResponseInputItem] = [developer_message]
    if latest_user is not None:
        history_items.append(latest_user)

    filtered_pre_items = tuple(
        item
        for item in handoff_input_data.pre_handoff_items
        if _get_run_item_role(item) != "assistant"
    )

    return handoff_input_data.clone(
        input_history=tuple(history_items),
        pre_handoff_items=filtered_pre_items,
    )


def _normalize_input_history(
    input_history: str | tuple[TResponseInputItem, ...],
) -> list[TResponseInputItem]:
    if isinstance(input_history, str):
        return ItemHelpers.input_to_new_input_list(input_history)
    return [deepcopy(item) for item in input_history]


def _run_item_to_plain_input(run_item: RunItem) -> TResponseInputItem:
    return deepcopy(run_item.to_input_item())


def _build_developer_message(transcript: list[TResponseInputItem]) -> TResponseInputItem:
    transcript_copy = [deepcopy(item) for item in transcript]
    if transcript_copy:
        summary_lines = [
            f"{idx + 1}. {_format_transcript_item(item)}" for idx, item in enumerate(transcript_copy)
        ]
    else:
        summary_lines = ["(no previous turns recorded)"]

    content_lines = [_CONVERSATION_HISTORY_START, *summary_lines, _CONVERSATION_HISTORY_END]
    content = "\n".join(content_lines)
    return {
        "role": "developer",
        "content": content,
        "metadata": {
            _NEST_HISTORY_METADATA_KEY: {_NEST_HISTORY_TRANSCRIPT_KEY: transcript_copy}
        },
    }


def _format_transcript_item(item: TResponseInputItem) -> str:
    role = item.get("role")
    if isinstance(role, str):
        prefix = role
        name = item.get("name")
        if isinstance(name, str) and name:
            prefix = f"{prefix} ({name})"
        content_str = _stringify_content(item.get("content"))
        return f"{prefix}: {content_str}" if content_str else prefix

    item_type = item.get("type", "item")
    rest = {k: v for k, v in item.items() if k != "type"}
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


def _find_latest_user_turn(
    transcript: list[TResponseInputItem],
) -> TResponseInputItem | None:
    for item in reversed(transcript):
        if item.get("role") == "user":
            return deepcopy(item)
    return None


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
    if item.get("role") != "developer":
        return None
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return None
    payload = metadata.get(_NEST_HISTORY_METADATA_KEY)
    if not isinstance(payload, dict):
        return None
    transcript = payload.get(_NEST_HISTORY_TRANSCRIPT_KEY)
    if not isinstance(transcript, list):
        return None
    normalized: list[TResponseInputItem] = []
    for entry in transcript:
        if isinstance(entry, dict):
            normalized.append(deepcopy(entry))
    return normalized if normalized else []


def _get_run_item_role(run_item: RunItem) -> str | None:
    role_candidate = run_item.to_input_item().get("role")
    return role_candidate if isinstance(role_candidate, str) else None


def _remove_tools_from_items(items: tuple[RunItem, ...]) -> tuple[RunItem, ...]:
    filtered_items = []
    for item in items:
        if (
            isinstance(item, HandoffCallItem)
            or isinstance(item, HandoffOutputItem)
            or isinstance(item, ToolCallItem)
            or isinstance(item, ToolCallOutputItem)
            or isinstance(item, ReasoningItem)
        ):
            continue
        filtered_items.append(item)
    return tuple(filtered_items)


def _remove_tool_types_from_input(
    items: tuple[TResponseInputItem, ...],
) -> tuple[TResponseInputItem, ...]:
    tool_types = [
        "function_call",
        "function_call_output",
        "computer_call",
        "computer_call_output",
        "file_search_call",
        "web_search_call",
    ]

    filtered_items: list[TResponseInputItem] = []
    for item in items:
        itype = item.get("type")
        if itype in tool_types:
            continue
        filtered_items.append(item)
    return tuple(filtered_items)

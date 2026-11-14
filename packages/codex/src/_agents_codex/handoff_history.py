"""Application-owned summaries for native handoff presentation."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any, TypeVar

from .handoffs import HandoffInput

_ContextT = TypeVar("_ContextT")
_Item = dict[str, Any]
_HistoryMapper = Callable[[list[_Item]], Sequence[_Item]]
_PREAMBLE = "For context, here is the conversation so far between the user and the previous agent:"
_ROLES = frozenset({"user", "assistant", "system", "developer"})
_SUMMARY_ONLY = frozenset({"function_call", "function_call_output", "reasoning"})
_TOOL_ITEMS = _SUMMARY_ONLY | {
    "apply_patch_call",
    "apply_patch_call_output",
    "code_interpreter_call",
    "computer_call",
    "computer_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "file_search_call",
    "hosted_tool_call",
    "image_generation_call",
    "local_shell_call",
    "local_shell_call_output",
    "mcp_approval_request",
    "mcp_approval_response",
    "mcp_call",
    "mcp_list_tools",
    "program",
    "program_output",
    "shell_call",
    "shell_call_output",
    "tool_search_call",
    "tool_search_output",
    "web_search_call",
}
_METADATA = frozenset(
    {
        "_agents_tool_title",
        "_agents_tool_description",
        "created_by",
        "internal_chat_message_metadata_passthrough",
    }
)


def _bind_handoff_summaries(value: HandoffInput[Any], summaries: Sequence[dict[str, Any]]) -> None:
    object.__setattr__(
        value,
        "_handoff_summaries",
        {
            row["index"]: (
                value.input[row["index"]],
                _summary_text(value.input[row["index"]]),
                tuple(row["wrappers"]),
            )
            for row in summaries
        },
    )


def _capture_handoff_summaries(value: HandoffInput[Any]) -> list[dict[str, Any]]:
    summaries = getattr(value, "_handoff_summaries", {})
    return [
        {"index": index, "wrappers": wrappers}
        for index, (item, text, wrappers) in summaries.items()
        if index < len(value.input) and value.input[index] is item and _summary_text(item) == text
    ]


def _remove_all_tools(value: HandoffInput[_ContextT]) -> HandoffInput[_ContextT]:
    summaries = {row["index"]: row["wrappers"] for row in _capture_handoff_summaries(value)}
    initial: list[_Item] = []
    retained_summaries: list[dict[str, Any]] = []
    for index, item in enumerate(value.input):
        if item.get("type") in _TOOL_ITEMS:
            continue
        if index in summaries:
            retained_summaries.append({"index": len(initial), "wrappers": summaries[index]})
        initial.append(item)

    result = replace(
        value,
        input=tuple(initial),
        previous=tuple(item for item in value.previous if item.get("type") not in _TOOL_ITEMS),
        generated=tuple(item for item in value.generated if item.get("type") not in _TOOL_ITEMS),
    )
    _bind_handoff_summaries(result, retained_summaries)
    return result


def _nest_handoff_history(
    value: HandoffInput[_ContextT],
    *,
    history_mapper: _HistoryMapper | None,
    history_wrappers: tuple[str, str],
) -> HandoffInput[_ContextT]:
    if len(history_wrappers) != 2 or any(
        not isinstance(marker, str) or not marker for marker in history_wrappers
    ):
        raise ValueError("History wrappers must contain two nonempty strings")
    initial, previous, generated = deepcopy((value.input, value.previous, value.generated))
    owned = getattr(value, "_handoff_summaries", None)
    history: list[_Item] = []
    for index, item in enumerate(initial):
        expanded = None
        if owned is not None:
            summary = owned.get(index)
            if (
                summary is not None
                and value.input[index] is summary[0]
                and _summary_text(item) == summary[1]
            ):
                expanded = _read_summary(item, summary[2])
        elif history_mapper is None:
            # Standalone helper calls can nest text without native transfer state.
            expanded = _read_summary(item, history_wrappers)
        history.extend(expanded if expanded is not None else [item])

    summaries = []
    if history_mapper is not None:
        items = list(history_mapper([*history, *previous, *generated]))
    else:
        items = []
        pending = history
        for group, earlier in ((previous, True), (generated, False)):
            for item in group:
                if not _forward(item, earlier=earlier):
                    pending.append(item)
                    continue
                if pending or not items:
                    summaries.append({"index": len(items), "wrappers": history_wrappers})
                    items.append(_summary(pending, history_wrappers))
                    pending = []
                items.append(item)
        if pending or not items:
            summaries.append({"index": len(items), "wrappers": history_wrappers})
            items.append(_summary(pending, history_wrappers))
    result = replace(value, input=tuple(items), previous=(), generated=())
    _bind_handoff_summaries(result, summaries)
    return result


def _forward(item: _Item, *, earlier: bool) -> bool:
    caller = item.get("caller")
    if item.get("type") in {"program", "program_output"} or (
        isinstance(caller, dict) and caller.get("type") == "program"
    ):
        return False
    role = item.get("role")
    if earlier and role == "assistant":
        return False
    if not earlier and isinstance(role, str) and role:
        return True
    return item.get("type") not in _SUMMARY_ONLY


def _format(item: _Item) -> str:
    item = {key: value for key, value in item.items() if key not in _METADATA}
    content = item.get("content")
    # Single-line plain messages retain the v0 default transcript spelling.
    # Native structured messages and named/multiline content use lossless JSON.
    if (
        item.get("role") in _ROLES
        and isinstance(content, str)
        and "\n" not in content
        and "\r" not in content
        and not item.get("name")
    ):
        return f"{item['role']}: {content}"
    item.pop("provider_data", None)
    return json.dumps(item, ensure_ascii=False)


def _summary(items: list[_Item], wrappers: tuple[str, str]) -> _Item:
    records = [f"{index}. {_format(item)}" for index, item in enumerate(items, 1)]
    text = "\n".join(
        [
            _PREAMBLE,
            wrappers[0],
            *(records or ["(no previous turns recorded)"]),
            wrappers[1],
        ]
    )
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _summary_text(item: _Item) -> str | None:
    if item.get("role") != "assistant":
        return None
    content = item.get("content")
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "output_text"
    ):
        content = content[0].get("text")
    return content if isinstance(content, str) else None


def _read_summary(item: _Item, wrappers: tuple[str, str]) -> list[_Item] | None:
    content = _summary_text(item)
    prefix = f"{_PREAMBLE}\n{wrappers[0]}\n"
    suffix = f"\n{wrappers[1]}"
    if (
        not isinstance(content, str)
        or not content.startswith(prefix)
        or not content.endswith(suffix)
    ):
        return None
    body = content[len(prefix) : -len(suffix)]
    if body == "(no previous turns recorded)":
        return []
    items = []
    for index, record in enumerate(body.split("\n"), 1):
        number = f"{index}. "
        if not record.startswith(number):
            return None
        payload = record[len(number) :]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            # This branch reads the plain-message spelling written by _format.
            role, separator, text = payload.partition(": ")
            if not separator or role not in _ROLES:
                return None
            parsed = {"role": role, "content": text}
        if not isinstance(parsed, dict):
            return None
        items.append(parsed)
    return items

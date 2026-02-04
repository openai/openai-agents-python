"""
Item utilities for the run pipeline. Hosts input normalization helpers and lightweight builders
for synthetic run items or IDs used during tool execution. Internal use only.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from openai.types.responses import ResponseFunctionToolCall
from pydantic import BaseModel

from ..agent_tool_state import drop_agent_tool_run_result
from ..items import ItemHelpers, ToolCallOutputItem, TResponseInputItem
from ..models.fake_id import FAKE_RESPONSES_ID
from ..tool import DEFAULT_APPROVAL_REJECTION_MESSAGE

REJECTION_MESSAGE = DEFAULT_APPROVAL_REJECTION_MESSAGE
_TOOL_CALL_TO_OUTPUT_TYPE: dict[str, str] = {
    "function_call": "function_call_output",
    "shell_call": "shell_call_output",
    "apply_patch_call": "apply_patch_call_output",
    "computer_call": "computer_call_output",
    "local_shell_call": "local_shell_call_output",
}

__all__ = [
    "REJECTION_MESSAGE",
    "copy_input_items",
    "drop_orphan_function_calls",
    "ensure_input_item_format",
    "normalize_input_items_for_api",
    "normalize_resumed_input",
    "fingerprint_input_item",
    "deduplicate_input_items",
    "deduplicate_input_items_preferring_latest",
    "function_rejection_item",
    "shell_rejection_item",
    "apply_patch_rejection_item",
    "extract_mcp_request_id",
    "extract_mcp_request_id_from_run",
]


def copy_input_items(value: str | list[TResponseInputItem]) -> str | list[TResponseInputItem]:
    """Return a shallow copy of input items so mutations do not leak between turns."""
    return value if isinstance(value, str) else value.copy()


def drop_orphan_function_calls(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    """
    Remove tool call items that do not have corresponding outputs so resumptions or retries do not
    replay stale tool calls.
    """

    completed_call_ids = _completed_call_ids_by_type(items)

    filtered: list[TResponseInputItem] = []
    for entry in items:
        if not isinstance(entry, dict):
            filtered.append(entry)
            continue
        entry_type = entry.get("type")
        if not isinstance(entry_type, str):
            filtered.append(entry)
            continue
        output_type = _TOOL_CALL_TO_OUTPUT_TYPE.get(entry_type)
        if output_type is None:
            filtered.append(entry)
            continue
        call_id = entry.get("call_id")
        if isinstance(call_id, str) and call_id in completed_call_ids.get(output_type, set()):
            filtered.append(entry)
    return filtered


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

        normalized_item = dict(coerced)
        normalized.append(cast(TResponseInputItem, normalized_item))
    return normalized


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
        if hasattr(item, "model_dump"):
            payload = item.model_dump(exclude_unset=True)
        elif isinstance(item, dict):
            payload = dict(item)
            if ignore_ids_for_matching:
                payload.pop("id", None)
        else:
            payload = ensure_input_item_format(item)
            if ignore_ids_for_matching and isinstance(payload, dict):
                payload.pop("id", None)

        return json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        return None


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
) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected function tool call."""
    if isinstance(tool_call, ResponseFunctionToolCall):
        drop_agent_tool_run_result(tool_call)
    return ToolCallOutputItem(
        output=rejection_message,
        raw_item=ItemHelpers.tool_call_output_item(tool_call, rejection_message),
        agent=agent,
    )


def shell_rejection_item(
    agent: Any,
    call_id: str,
    *,
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
    return ToolCallOutputItem(agent=agent, output=rejection_message, raw_item=rejection_raw_item)


def apply_patch_rejection_item(
    agent: Any,
    call_id: str,
    *,
    rejection_message: str = REJECTION_MESSAGE,
) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected apply_patch call."""
    rejection_raw_item: dict[str, Any] = {
        "type": "apply_patch_call_output",
        "call_id": call_id,
        "status": "failed",
        "output": rejection_message,
    }
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


def _coerce_to_dict(value: object) -> dict[str, Any] | None:
    """Convert model items to dicts so fields can be renamed and sanitized."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, BaseModel):
        try:
            return value.model_dump(exclude_unset=True)
        except Exception:
            return None
    if hasattr(value, "model_dump"):
        try:
            return cast(dict[str, Any], value.model_dump(exclude_unset=True))
        except Exception:
            return None
    return None

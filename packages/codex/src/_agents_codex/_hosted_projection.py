"""Project passive hosted tool results from complete provider payloads."""

from typing import Any as Any

from ._public import preserve_module_identity as _preserve_module_identity

_LIVE_TOOL_ITEM_FIELDS = {
    "tool_search_call": ("call_id", "execution", "arguments"),
    "tool_search_output": ("call_id", "execution", "tools"),
    "mcp_approval_response": ("approval_request_id", "approve", "reason"),
}

_HOSTED_RESPONSE_ITEM_FIELDS = {
    "mcp_list_tools": ("server_label", "tools", "error"),
    "mcp_call": ("server_label", "name", "arguments", "output", "error"),
    "program": ("call_id", "code", "fingerprint"),
    "program_output": ("call_id", "result"),
    "file_search_call": ("queries", "results"),
    "code_interpreter_call": ("container_id", "code", "outputs"),
    "shell_call": ("call_id", "action"),
    "shell_call_output": ("call_id", "output"),
    "image_generation_call": ("result", "revised_prompt"),
}


def project_hosted_response_item(
    raw: dict[str, Any],
    turn_id: str | None,
    *,
    item_id: str | None = None,
    live_item_id: str | None = None,
) -> dict[str, Any] | None:
    """Keep the complete provider payload alongside its public typed fields."""
    raw = {
        key: value
        for key, value in raw.items()
        if key != "internal_chat_message_metadata_passthrough"
    }
    kind = raw.get("type", "")
    live_fields = _LIVE_TOOL_ITEM_FIELDS.get(kind) if live_item_id is not None else None
    fields = live_fields or _HOSTED_RESPONSE_ITEM_FIELDS.get(kind)
    resolved_id = raw.get("id") or item_id
    if live_fields is not None:
        if kind == "mcp_approval_response":
            raw = {key: raw[key] for key in ("type", "id", *live_fields) if key in raw}
        elif resolved_id is None and (call_id := raw.get("call_id")):
            resolved_id = f"{call_id}:output" if kind == "tool_search_output" else call_id
        resolved_id = resolved_id or live_item_id
    if fields is None or not resolved_id:
        return None
    if live_fields is not None:
        status = raw.get("status")
        if status not in {"in_progress", "completed", "failed", "incomplete"}:
            status = "completed"
    elif kind == "program":
        status = raw.get("status", "completed")
    elif kind in {"shell_call", "shell_call_output"}:
        status = raw.get("status") or "unknown"
    elif kind in {"mcp_list_tools", "mcp_call"}:
        status = raw.get("status", "failed" if raw.get("error") is not None else "completed")
    else:
        status = raw["status"]
    return {
        "type": kind,
        "id": resolved_id,
        "turn_id": turn_id,
        "status": status,
        "provider_data": raw,
        **{key: raw.get(key) for key in fields},
    }


_preserve_module_identity(globals(), "agents._projection")

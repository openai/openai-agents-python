"""Project passive hosted tool results from complete provider payloads."""

from typing import Any as Any

from ._public import preserve_module_identity as _preserve_module_identity


def project_hosted_response_item(
    raw: dict[str, Any], turn_id: str | None, *, item_id: str | None = None
) -> dict[str, Any] | None:
    """Keep the complete provider payload alongside its public typed fields."""
    raw = {
        key: value
        for key, value in raw.items()
        if key != "internal_chat_message_metadata_passthrough"
    }
    kind = raw.get("type", "")
    fields = {
        "file_search_call": ("queries", "results"),
        "code_interpreter_call": ("container_id", "code", "outputs"),
        "shell_call": ("call_id", "action"),
        "shell_call_output": ("call_id", "output"),
        "image_generation_call": ("result", "revised_prompt"),
    }.get(kind)
    resolved_id = raw.get("id") or item_id
    if fields is None or not resolved_id:
        return None
    return {
        "type": kind,
        "id": resolved_id,
        "turn_id": turn_id,
        "status": (
            (raw.get("status") or "unknown")
            if kind in {"shell_call", "shell_call_output"}
            else raw["status"]
        ),
        "provider_data": raw,
        **{key: raw.get(key) for key in fields},
    }


_preserve_module_identity(globals(), "agents._projection")

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeGuard

from pydantic import BaseModel

from ._tool_identity import (
    FunctionToolLookupKey,
    get_function_tool_lookup_key_for_call,
    get_hosted_mcp_approval_request_identity,
)

_TOOL_INVOCATION_TYPES = frozenset(
    {
        "apply_patch_call",
        "computer_call",
        "custom_tool_call",
        "function_call",
        "local_shell_call",
        "mcp_approval_request",
        "shell_call",
    }
)
_TOOL_OUTPUT_TYPES = {
    "apply_patch_call_output": "apply_patch_call",
    "computer_call_output": "computer_call",
    "custom_tool_call_output": "custom_tool_call",
    "function_call_output": "function_call",
    "local_shell_call_output": "local_shell_call",
    "mcp_approval_response": "mcp_approval_request",
    "shell_call_output": "shell_call",
}
_SEMANTIC_FIELDS = (
    "type",
    "name",
    "namespace",
    "server_label",
    "arguments",
    "input",
    "action",
    "actions",
    "pending_safety_checks",
    "operation",
    "operations",
    "environment",
    "caller",
)


def is_tool_invocation_type(value: Any) -> TypeGuard[str]:
    """Return whether a value names a canonical tool invocation type."""
    return isinstance(value, str) and value in _TOOL_INVOCATION_TYPES


def is_tool_invocation_digest(value: Any) -> TypeGuard[str]:
    """Return whether a value is a canonical lowercase SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        kwargs = {"exclude_none": True, "exclude_unset": True}
        if isinstance(value, BaseModel):
            kwargs["warnings"] = False
        dumped = model_dump(**kwargs)
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _normalize_value(value: Any, *, exclude_none: bool = False) -> Any:
    mapping = _as_mapping(value)
    if mapping is not None:
        return {
            str(key): _normalize_value(item, exclude_none=exclude_none)
            for key, item in sorted(mapping.items(), key=lambda pair: str(pair[0]))
            if not (exclude_none and item is None)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_value(item, exclude_none=exclude_none) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _normalize_arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return _normalize_value(value)
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"Invalid JSON constant: {constant}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return value
    return _normalize_value(parsed)


def _unwrap_hosted_mcp_approval(raw_item: Any) -> Mapping[str, Any] | None:
    mapping = _as_mapping(raw_item)
    if mapping is None:
        return None
    provider_data = mapping.get("provider_data")
    if (
        mapping.get("type") == "hosted_tool_call"
        and isinstance(provider_data, Mapping)
        and provider_data.get("type") == "mcp_approval_request"
    ):
        request_identity = get_hosted_mcp_approval_request_identity(mapping)
        if request_identity is None:
            return None
        merged = dict(mapping)
        merged.update(provider_data)
        if request_identity.request_id is None:
            merged.pop("id", None)
        else:
            merged["id"] = request_identity.request_id
        if request_identity.tool_name is not None:
            merged["name"] = request_identity.tool_name
        return merged
    return mapping


def tool_invocation_identity(
    raw_item: Any,
    *,
    tool_lookup_key: FunctionToolLookupKey | None = None,
    tool_name: str | None = None,
    invocation_role: str | None = None,
) -> tuple[str, str, str] | None:
    """Return invocation type, provider call ID, and a stable semantic fingerprint."""
    identity = tool_invocation_identity_and_scope(
        raw_item,
        tool_lookup_key=tool_lookup_key,
        tool_name=tool_name,
        invocation_role=invocation_role,
    )
    if identity is None:
        return None
    invocation_type, call_id, _, fingerprint = identity
    return invocation_type, call_id, fingerprint


def tool_invocation_identity_and_scope(
    raw_item: Any,
    *,
    tool_lookup_key: FunctionToolLookupKey | None = None,
    tool_name: str | None = None,
    invocation_role: str | None = None,
) -> tuple[str, str, str, str] | None:
    """Return invocation identity together with its stable approval scope."""
    call_identity = tool_invocation_call_id(raw_item)
    approval_scope_identity = tool_invocation_approval_scope(
        raw_item,
        tool_lookup_key=tool_lookup_key,
        tool_name=tool_name,
        invocation_role=invocation_role,
    )
    if call_identity is None or approval_scope_identity is None:
        return None
    invocation_type, call_id = call_identity
    scope_invocation_type, approval_scope = approval_scope_identity
    if call_id is None or scope_invocation_type != invocation_type:
        return None

    mapping = _unwrap_hosted_mcp_approval(raw_item)
    if mapping is None:
        return None

    if invocation_type == "function_call":
        if "arguments" not in mapping:
            return None
    elif invocation_type == "mcp_approval_request":
        if "arguments" not in mapping:
            return None
    elif invocation_type == "custom_tool_call":
        if not isinstance(mapping.get("name"), str) or not mapping["name"]:
            return None
        if "input" not in mapping:
            return None
    elif invocation_type in {"computer_call", "local_shell_call", "shell_call"}:
        if "action" not in mapping:
            return None
    elif invocation_type == "apply_patch_call":
        if "operation" not in mapping and "operations" not in mapping:
            return None

    semantic_payload: dict[str, Any] = {"approval_scope": approval_scope}
    for field_name in _SEMANTIC_FIELDS:
        if invocation_type == "function_call" and field_name in {"name", "namespace"}:
            continue
        if field_name not in mapping:
            continue
        value = mapping[field_name]
        if value is None:
            continue
        semantic_payload[field_name] = (
            _normalize_arguments(value)
            if field_name == "arguments"
            else _normalize_value(value, exclude_none=True)
        )

    return (
        invocation_type,
        call_id,
        approval_scope,
        _fingerprint(semantic_payload),
    )


def tool_invocation_call_id(raw_item: Any) -> tuple[str, str | None] | None:
    """Return a recognized invocation type and its valid non-empty call ID, if present."""
    mapping = _unwrap_hosted_mcp_approval(raw_item)
    if mapping is None:
        return None
    invocation_type = mapping.get("type")
    if invocation_type not in _TOOL_INVOCATION_TYPES:
        return None
    candidate = (
        mapping.get("id") if invocation_type == "mcp_approval_request" else mapping.get("call_id")
    )
    return invocation_type, candidate if isinstance(candidate, str) and candidate else None


def tool_invocation_approval_scope(
    raw_item: Any,
    *,
    tool_lookup_key: FunctionToolLookupKey | None = None,
    tool_name: str | None = None,
    invocation_role: str | None = None,
) -> tuple[str, str] | None:
    """Return the stable authorization scope for a recognized tool invocation."""
    mapping = _unwrap_hosted_mcp_approval(raw_item)
    if mapping is None:
        return None
    invocation_type = mapping.get("type")
    if invocation_type not in _TOOL_INVOCATION_TYPES:
        return None

    payload: dict[str, Any] = {"type": invocation_type}
    if invocation_role is not None:
        payload["invocation_role"] = invocation_role
    if invocation_type == "function_call":
        resolved_lookup_key = (
            tool_lookup_key
            if tool_lookup_key is not None
            else get_function_tool_lookup_key_for_call(mapping)
        )
        if resolved_lookup_key is None:
            return None
        payload["tool_lookup_key"] = _normalize_value(resolved_lookup_key)
    elif invocation_type == "mcp_approval_request":
        tool_name = mapping.get("name")
        server_label = mapping.get("server_label")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(server_label, str)
            or not server_label
        ):
            return None
        payload["name"] = tool_name
        payload["server_label"] = server_label
    else:
        resolved_tool_name = tool_name or mapping.get("name")
        if isinstance(resolved_tool_name, str) and resolved_tool_name:
            payload["name"] = resolved_tool_name
    return invocation_type, _fingerprint(payload)


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_mcp_approval_invocation(raw_item: Any) -> bool:
    """Return whether an item represents a hosted MCP approval request."""
    mapping = _unwrap_hosted_mcp_approval(raw_item)
    return mapping is not None and mapping.get("type") == "mcp_approval_request"


def tool_output_identity(raw_item: Any) -> tuple[str, str] | None:
    """Return the invocation type and call ID completed by a tool output item."""
    mapping = _as_mapping(raw_item)
    if mapping is None:
        return None
    output_type = mapping.get("type")
    if not isinstance(output_type, str):
        return None
    invocation_type = _TOOL_OUTPUT_TYPES.get(output_type)
    if invocation_type is None:
        return None
    candidate = (
        mapping.get("approval_request_id")
        if output_type == "mcp_approval_response"
        else mapping.get("call_id")
    )
    if not isinstance(candidate, str) or not candidate:
        return None
    return invocation_type, candidate

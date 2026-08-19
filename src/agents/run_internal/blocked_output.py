"""Canonical data-free function-tool payloads rejected by an output guardrail."""

from __future__ import annotations

from typing import Any

from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_function_tool_call import CallerDirect

from ..exceptions import AgentsException

OUTPUT_GUARDRAIL_BLOCKED_TOOL_OUTPUT = "Output withheld by an output guardrail."

_RESPONSE_OUTPUT_STATUSES = frozenset({"in_progress", "completed", "incomplete"})


def _exact_dict_field(values: dict[Any, Any], field: str) -> Any:
    """Read one exact string key without invoking stored-key equality hooks."""
    for key, value in dict.items(values):
        if type(key) is str and str.__eq__(key, field) is True:
            return value
    return None


def _payload_field(raw_item: Any, field: str) -> Any:
    """Read an allowlisted field without copying extras or invoking instance hooks."""
    if type(raw_item) is dict:
        values = raw_item
    elif type(raw_item) is ResponseFunctionToolCall:
        values = object.__getattribute__(raw_item, "__dict__")
    else:
        raise AgentsException("Cannot sanitize an unsupported tool item variant.")
    if type(values) is not dict:
        raise AgentsException("Cannot sanitize an unsupported tool item representation.")
    return _exact_dict_field(values, field)


def _required_string(raw_item: Any, field: str) -> str:
    value = _payload_field(raw_item, field)
    if type(value) is not str or not value:
        raise AgentsException(f"Cannot sanitize a function tool item without {field}.")
    return value


def _copy_optional_string(
    sanitized: dict[str, Any],
    raw_item: Any,
    field: str,
) -> None:
    value = _payload_field(raw_item, field)
    if value is None:
        return
    if type(value) is not str or not value:
        raise AgentsException(f"Cannot sanitize a function tool item with an invalid {field}.")
    sanitized[field] = value


def _copy_optional_status(sanitized: dict[str, Any], raw_item: Any) -> None:
    status = _payload_field(raw_item, "status")
    if status is None:
        return
    if type(status) is not str or status not in _RESPONSE_OUTPUT_STATUSES:
        raise AgentsException("Cannot sanitize a function tool item with an invalid status.")
    sanitized["status"] = status


def _copy_optional_direct_caller(sanitized: dict[str, Any], raw_item: Any) -> None:
    caller = _payload_field(raw_item, "caller")
    if caller is None:
        return
    if type(caller) is CallerDirect:
        values = object.__getattribute__(caller, "__dict__")
        caller_type = _exact_dict_field(values, "type") if type(values) is dict else None
    elif type(caller) is dict:
        caller_type = _exact_dict_field(caller, "type")
    else:
        caller_type = None
    if type(caller_type) is str and str.__eq__(caller_type, "direct") is True:
        sanitized["caller"] = {"type": "direct"}
        return
    raise AgentsException("Cannot sanitize a function tool item with a non-direct caller.")


def blocked_function_call_payload(raw_item: Any) -> dict[str, Any]:
    """Build a provider-valid function call from explicitly allowlisted fields."""
    item_type = _payload_field(raw_item, "type")
    if type(item_type) is not str or str.__eq__(item_type, "function_call") is not True:
        raise AgentsException("Cannot sanitize an unsupported tool call variant.")
    arguments = _payload_field(raw_item, "arguments")
    if type(arguments) is not str:
        raise AgentsException("Cannot sanitize a function tool item without arguments.")
    sanitized: dict[str, Any] = {
        "type": "function_call",
        "name": _required_string(raw_item, "name"),
        "arguments": arguments,
        "call_id": _required_string(raw_item, "call_id"),
    }
    _copy_optional_string(sanitized, raw_item, "id")
    _copy_optional_string(sanitized, raw_item, "namespace")
    _copy_optional_status(sanitized, raw_item)
    _copy_optional_direct_caller(sanitized, raw_item)
    try:
        validated = ResponseFunctionToolCall(**sanitized)
    except Exception:
        raise AgentsException("Sanitized function_call is not valid for replay.") from None
    return validated.model_dump(exclude_unset=True)


def blocked_function_output_payload(raw_item: Any) -> dict[str, Any]:
    """Build a replay-valid function output from explicitly allowlisted fields."""
    item_type = _payload_field(raw_item, "type")
    if type(item_type) is not str or str.__eq__(item_type, "function_call_output") is not True:
        raise AgentsException("Cannot sanitize an unsupported tool output variant.")
    sanitized: dict[str, Any] = {
        "type": "function_call_output",
        "call_id": _required_string(raw_item, "call_id"),
        "output": OUTPUT_GUARDRAIL_BLOCKED_TOOL_OUTPUT,
    }
    _copy_optional_string(sanitized, raw_item, "id")
    _copy_optional_status(sanitized, raw_item)
    _copy_optional_direct_caller(sanitized, raw_item)
    try:
        from ..run_state import _deserialize_tool_call_output_raw_item

        restored = _deserialize_tool_call_output_raw_item(sanitized)
    except Exception:
        raise AgentsException("Sanitized function_call_output is not valid for replay.") from None
    if restored is None:
        raise AgentsException("Sanitized function_call_output is not valid for replay.")
    return sanitized

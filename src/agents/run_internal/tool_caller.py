from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from typing import Any

from ..exceptions import ModelBehaviorError
from ..tool import ToolCaller
from ..tracing import SpanError
from ..util import _error_tracing


def ensure_tool_caller_allowed(
    *,
    tool_call: Any,
    allowed_callers: Sequence[ToolCaller] | None,
    tool_name: str,
    agent_name: str,
) -> None:
    """Reject tool calls whose direct or programmatic caller is not allowed."""
    # Import lazily because tool_execution imports RunState through the tool-use tracker.
    from .tool_execution import extract_tool_call_id, get_mapping_or_attr

    caller_value = get_mapping_or_attr(tool_call, "caller")
    caller_type = get_mapping_or_attr(caller_value, "type")
    if caller_value is None:
        caller: ToolCaller = "direct"
    elif caller_type == "direct":
        caller = "direct"
    elif caller_type == "program":
        caller = "programmatic"
    else:
        message = f"Model invoked tool {tool_name} with unsupported caller type {caller_type!r}."
        _error_tracing.attach_error_to_current_span(
            SpanError(
                message=message,
                data={
                    "agent_name": agent_name,
                    "tool_name": tool_name,
                    "tool_call_id": extract_tool_call_id(tool_call),
                    "tool_caller_type": caller_type,
                },
            )
        )
        raise ModelBehaviorError(message)

    effective_allowed_callers = list(allowed_callers) if allowed_callers is not None else ["direct"]
    if caller in effective_allowed_callers:
        return

    message = (
        f"Model invoked tool {tool_name} with caller {caller}, but the tool allows only "
        f"{json.dumps(effective_allowed_callers)}."
    )
    _error_tracing.attach_error_to_current_span(
        SpanError(
            message=message,
            data={
                "agent_name": agent_name,
                "tool_name": tool_name,
                "tool_call_id": extract_tool_call_id(tool_call),
                "tool_caller": caller,
            },
        )
    )
    raise ModelBehaviorError(message)


def ensure_programmatic_tool_call_parent(
    *,
    tool_call: Any,
    programmatic_tool_present: bool,
    program_call_ids: Collection[str],
    completed_program_call_ids: Collection[str],
    agent_name: str,
) -> None:
    """Reject program-owned calls without an active parent program."""
    # Import lazily because tool_execution imports RunState through the tool-use tracker.
    from .tool_execution import get_mapping_or_attr

    caller = get_mapping_or_attr(tool_call, "caller")
    if get_mapping_or_attr(caller, "type") != "program":
        return

    output_type = get_mapping_or_attr(tool_call, "type")
    caller_id = get_mapping_or_attr(caller, "caller_id")
    if not programmatic_tool_present:
        message = f"Model produced {output_type} item without a programmatic_tool_calling tool."
        _error_tracing.attach_error_to_current_span(
            SpanError(
                message="Programmatic tool not found",
                data={"agent_name": agent_name, "output_type": output_type},
            )
        )
        raise ModelBehaviorError(message)

    if not isinstance(caller_id, str) or caller_id not in program_call_ids:
        message = (
            f"Model produced {output_type} item with a program caller that does not "
            "match a parent program item."
        )
        _error_tracing.attach_error_to_current_span(
            SpanError(
                message="Program parent not found",
                data={
                    "agent_name": agent_name,
                    "output_type": output_type,
                    "caller_id": caller_id,
                },
            )
        )
        raise ModelBehaviorError(message)

    if caller_id in completed_program_call_ids:
        message = (
            f"Model produced {output_type} item with a program caller whose parent "
            "program is already completed."
        )
        _error_tracing.attach_error_to_current_span(
            SpanError(
                message="Program parent already completed",
                data={
                    "agent_name": agent_name,
                    "output_type": output_type,
                    "caller_id": caller_id,
                },
            )
        )
        raise ModelBehaviorError(message)

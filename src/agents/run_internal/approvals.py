"""
Helpers for approval handling within the run loop. Keep only execution-time utilities that
coordinate approval placeholders and normalization; public APIs should stay in run.py or
peer modules.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from openai.types.responses import ResponseFunctionToolCall

from ..agent import Agent
from ..items import ItemHelpers, RunItem, ToolApprovalItem, ToolCallOutputItem, TResponseInputItem

# --------------------------
# Public helpers
# --------------------------


def append_approval_error_output(
    *,
    generated_items: list[RunItem],
    agent: Agent[Any],
    tool_call: Any,
    tool_name: str,
    call_id: str | None,
    message: str,
) -> None:
    """Emit a synthetic tool output so users see why an approval failed."""
    error_tool_call = _build_function_tool_call_for_approval_error(tool_call, tool_name, call_id)
    generated_items.append(
        ToolCallOutputItem(
            output=message,
            raw_item=ItemHelpers.tool_call_output_item(error_tool_call, message),
            agent=agent,
        )
    )


def filter_tool_approvals(interruptions: Sequence[Any]) -> list[ToolApprovalItem]:
    """Keep only approval items from a mixed interruption payload."""
    return [item for item in interruptions if isinstance(item, ToolApprovalItem)]


def approvals_from_step(step: Any) -> list[ToolApprovalItem]:
    """Return approvals from a step that may or may not contain interruptions."""
    interruptions = getattr(step, "interruptions", None)
    if interruptions is None:
        return []
    return filter_tool_approvals(interruptions)


def append_input_items_excluding_approvals(
    base_input: list[TResponseInputItem],
    items: Sequence[RunItem],
) -> None:
    """Append tool outputs to model input while skipping approval placeholders."""
    for item in items:
        if item.type == "tool_approval_item":
            continue
        base_input.append(item.to_input_item())


# --------------------------
# Private helpers
# --------------------------


def _build_function_tool_call_for_approval_error(
    tool_call: Any, tool_name: str, call_id: str | None
) -> ResponseFunctionToolCall:
    """Coerce raw tool call payloads into a normalized function_call for approval errors."""
    if isinstance(tool_call, ResponseFunctionToolCall):
        return tool_call
    return ResponseFunctionToolCall(
        type="function_call",
        name=tool_name,
        call_id=call_id or "unknown",
        status="completed",
        arguments="{}",
    )

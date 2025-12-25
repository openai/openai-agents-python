"""
Helpers for approval handling within the run loop. Keep only execution-time utilities that
coordinate approval placeholders, rewinds, and normalization; public APIs should stay in
run.py or peer modules.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from openai.types.responses import ResponseFunctionToolCall

from ..agent import Agent
from ..items import ItemHelpers, RunItem, ToolApprovalItem, ToolCallOutputItem, TResponseInputItem
from .run_steps import NextStepInterruption

# --------------------------
# Public helpers
# --------------------------


def collect_approvals_and_rewind(
    step: NextStepInterruption | None, generated_items: Sequence[RunItem]
) -> tuple[list[ToolApprovalItem], int]:
    """Gather pending approvals and compute how many items to rewind to drop duplicates."""
    pending_approval_items = _collect_tool_approvals(step)
    if not pending_approval_items:
        return [], 0
    rewind_count = _calculate_approval_rewind_count(pending_approval_items, generated_items)
    return pending_approval_items, rewind_count


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


def apply_rewind_offset(current_count: int, rewind_count: int) -> int:
    """Adjust persisted count when pending approvals require a rewind."""
    if rewind_count <= 0:
        return current_count
    return max(0, current_count - rewind_count)


def filter_tool_approvals(interruptions: Sequence[Any]) -> list[ToolApprovalItem]:
    """Keep only approval items from a mixed interruption payload."""
    return [item for item in interruptions if isinstance(item, ToolApprovalItem)]


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


def _extract_approval_identity(raw_item: Any) -> tuple[str | None, str | None]:
    """Return the call identifier and type used for approval deduplication."""
    if isinstance(raw_item, dict):
        call_id = raw_item.get("callId") or raw_item.get("call_id") or raw_item.get("id")
        raw_type = raw_item.get("type") or "unknown"
        return call_id, raw_type
    if isinstance(raw_item, ResponseFunctionToolCall):
        return raw_item.call_id, "function_call"
    return None, None


def _approval_identity(approval: ToolApprovalItem) -> str | None:
    """Unique identifier for approvals so we can dedupe repeated requests."""
    raw_item = approval.raw_item
    call_id, raw_type = _extract_approval_identity(raw_item)
    if call_id is None:
        return None
    return f"{raw_type or 'unknown'}:{call_id}"


def _calculate_approval_rewind_count(
    approvals: Sequence[ToolApprovalItem], generated_items: Sequence[RunItem]
) -> int:
    """Work out how many approval placeholders were already emitted so we can rewind safely."""
    pending_identities = {
        identity for approval in approvals if (identity := _approval_identity(approval)) is not None
    }
    if not pending_identities:
        return 0

    rewind_count = 0
    for item in reversed(generated_items):
        if not isinstance(item, ToolApprovalItem):
            continue
        identity = _approval_identity(item)
        if not identity or identity not in pending_identities:
            continue
        rewind_count += 1
        pending_identities.discard(identity)
        if not pending_identities:
            break
    return rewind_count


def _collect_tool_approvals(step: NextStepInterruption | None) -> list[ToolApprovalItem]:
    """Extract only approval items from an interruption step."""
    if not isinstance(step, NextStepInterruption):
        return []
    return [item for item in step.interruptions if isinstance(item, ToolApprovalItem)]

"""Internal output types attesting to work observed directly by the Codex adapter."""

import json
from pathlib import Path
from typing import Any, cast

from openai.types.responses import (
    ResponseCustomToolCall,
    ResponseCustomToolCallOutputItem,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallOutputItem,
)
from pydantic import Field

from ..exceptions import UserError


class CodexFunctionToolCallOutput(ResponseFunctionToolCallOutputItem):
    """A matching native function call has already completed in Codex."""


class CodexCustomToolCallOutput(ResponseCustomToolCallOutputItem):
    """A matching native custom call has already completed in Codex."""


class CodexPendingFunctionCall(ResponseFunctionToolCall):
    """A native function call has paused at Codex's own approval boundary."""

    checkpoint: dict[str, Any] = Field(exclude=True, repr=False)


class CodexPendingCustomCall(ResponseCustomToolCall):
    """A native custom call has paused at Codex's own approval boundary."""

    checkpoint: dict[str, Any] = Field(exclude=True, repr=False)


def native_approval_checkpoint(raw_item: Any) -> dict[str, Any] | None:
    """Validate an adapter-owned receipt against the call shown for approval."""
    # An unrelated approval may define __getattribute__("__class__"). Don't run it
    # while taking a detached copy of a pending call or a session checkpoint.
    raw_type = type(raw_item)
    if raw_type is not CodexPendingFunctionCall and raw_type is not CodexPendingCustomCall:
        return None
    checkpoint = raw_item.checkpoint
    required = {"version", "cwd", "call", "request", "model"}
    if (
        not isinstance(checkpoint, dict)
        or not required <= checkpoint.keys()
        or set(checkpoint) - required - {"preconditions"}
        or type(checkpoint["version"]) is not int
        or checkpoint["version"] != 1
        or not isinstance(checkpoint["cwd"], str)
        or not Path(checkpoint["cwd"]).is_absolute()
        or not isinstance(checkpoint["model"], str)
        or not checkpoint["model"]
        or not isinstance(checkpoint["request"], dict)
        or not isinstance(checkpoint["call"], dict)
        or ("preconditions" in checkpoint and not isinstance(checkpoint["preconditions"], dict))
    ):
        raise UserError("Invalid Codex native approval checkpoint.")
    model = (
        ResponseFunctionToolCall
        if isinstance(raw_item, CodexPendingFunctionCall)
        else ResponseCustomToolCall
    )
    display_metadata = {"codex_reasoning_summary"}
    displayed = model.model_validate(raw_item.model_dump()).model_dump(exclude=display_metadata)
    stored = model.model_validate(checkpoint["call"]).model_dump(exclude=display_metadata)
    if displayed != stored or raw_item.name not in {"exec_command", "apply_patch"}:
        raise UserError("Codex native approval no longer matches its original call.")
    return cast(dict[str, Any], json.loads(json.dumps(checkpoint, allow_nan=False)))


def restore_native_approval_call(raw_item: Any, checkpoint: Any) -> Any:
    """Restore only explicit SDK checkpoint metadata, never provider output fields."""
    if not isinstance(raw_item, dict) or not isinstance(checkpoint, dict):
        raise UserError("Invalid Codex native approval checkpoint.")
    item: CodexPendingFunctionCall | CodexPendingCustomCall
    item_type = raw_item.get("type")
    if item_type == "function_call":
        item = CodexPendingFunctionCall(**raw_item, checkpoint=checkpoint)
    elif item_type == "custom_tool_call":
        item = CodexPendingCustomCall(**raw_item, checkpoint=checkpoint)
    else:
        raise UserError("Invalid Codex native approval call type.")
    native_approval_checkpoint(item)
    return item

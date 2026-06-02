from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from ...items import ItemHelpers, RunItem, ToolApprovalItem, TResponseInputItem
from ...result import RunResultBase, RunResultStreaming
from ...run_internal.items import run_items_to_input_items
from ...util._json import _to_dump_compatible
from ..errors import WorkspaceReadNotFoundError
from ..session.base_sandbox_session import BaseSandboxSession

_EXCLUDED_MEMORY_ITEM_TYPES = frozenset(
    {
        "compaction",
        "image_generation_call",
        "reasoning",
    }
)
_INCLUDED_MEMORY_ITEM_TYPES = frozenset(
    {
        "apply_patch_call",
        "apply_patch_call_output",
        "computer_call",
        "computer_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "function_call",
        "function_call_output",
        "local_shell_call",
        "local_shell_call_output",
        "mcp_approval_request",
        "mcp_approval_response",
        "mcp_call",
        "shell_call",
        "shell_call_output",
        "tool_search_call",
        "tool_search_output",
        "web_search_call",
    }
)


def _validate_relative_path(*, name: str, path: Path) -> None:
    if path.is_absolute():
        raise ValueError(f"{name} must be relative to the sandbox workspace root, got: {path}")
    if ".." in path.parts:
        raise ValueError(f"{name} must not escape root, got: {path}")
    if path.parts in [(), (".",)]:
        raise ValueError(f"{name} must be non-empty")


class RolloutTerminalMetadata(BaseModel):
    terminal_state: Literal[
        "completed",
        "interrupted",
        "cancelled",
        "failed",
        "max_turns_exceeded",
        "guardrail_tripped",
    ]
    exception_type: str | None = None
    exception_message: str | None = None
    has_final_output: bool = False


def dump_rollout_json(result: Any) -> str:
    return json.dumps(result, separators=(",", ":")) + "\n"


def _normalize_jsonl_line(*, rollout_contents: str) -> bytes:
    try:
        obj = json.loads(rollout_contents)
    except Exception as exc:
        raise ValueError("rollout_contents must be valid JSON text") from exc
    line = json.dumps(obj, separators=(",", ":"))
    return (line + "\n").encode("utf-8")


def _should_include_memory_item(item: TResponseInputItem) -> bool:
    role = item.get("role")
    if role in {"developer", "system"}:
        return False
    if role in {"assistant", "tool", "user"}:
        return True

    item_type = item.get("type")
    if item_type in _EXCLUDED_MEMORY_ITEM_TYPES:
        return False
    return item_type in _INCLUDED_MEMORY_ITEM_TYPES


def _sanitize_memory_items(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    return [item for item in items if _should_include_memory_item(item)]


async def write_rollout(
    *,
    session: BaseSandboxSession,
    rollout_contents: str,
    rollouts_path: str = "sessions",
    file_name: str | None = None,
) -> Path:
    rollouts_dir_rel = Path(rollouts_path)
    _validate_relative_path(name="rollouts_path", path=rollouts_dir_rel)
    line_bytes = _normalize_jsonl_line(rollout_contents=rollout_contents)

    if file_name is not None:
        requested_file_rel = Path(file_name.strip())
        if not requested_file_rel.name.endswith(".jsonl") or len(requested_file_rel.parts) != 1:
            raise ValueError("file_name must be a simple .jsonl filename")
        dest_file_path_rel = rollouts_dir_rel / requested_file_rel
    else:
        dest_file_path_rel = None
        for _ in range(10):
            rollout_id = str(uuid.uuid4())
            candidate_rel = rollouts_dir_rel / f"{rollout_id}.jsonl"
            prior_bytes = await _read_existing_bytes(session=session, path=candidate_rel)
            if prior_bytes is None:
                dest_file_path_rel = candidate_rel
                break
    if dest_file_path_rel is None:
        raise ValueError(f"failed to allocate a unique rollout id under: {rollouts_dir_rel}")

    await session.mkdir(dest_file_path_rel.parent, parents=True)
    prior_bytes = await _read_existing_bytes(session=session, path=dest_file_path_rel)
    if prior_bytes is None:
        await session.write(dest_file_path_rel, io.BytesIO(line_bytes))
    else:
        await session.write(dest_file_path_rel, io.BytesIO(prior_bytes + line_bytes))
    return dest_file_path_rel


async def _read_existing_bytes(*, session: BaseSandboxSession, path: Path) -> bytes | None:
    try:
        handle = await session.read(path)
    except WorkspaceReadNotFoundError:
        return None

    try:
        payload = handle.read()
    finally:
        handle.close()
    return payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)


def terminal_metadata_for_result(
    result: RunResultBase,
    *,
    exception: BaseException | None = None,
) -> RolloutTerminalMetadata:
    if result.final_output is not None:
        return RolloutTerminalMetadata(terminal_state="completed", has_final_output=True)
    if getattr(result, "interruptions", None):
        return RolloutTerminalMetadata(terminal_state="interrupted", has_final_output=False)

    exc = exception
    if exc is None and isinstance(result, RunResultStreaming):
        exc = getattr(result, "_stored_exception", None)
        if exc is None and result._cancel_mode == "immediate":
            return RolloutTerminalMetadata(terminal_state="cancelled", has_final_output=False)

    if exc is None:
        return RolloutTerminalMetadata(terminal_state="failed", has_final_output=False)

    return terminal_metadata_for_exception(exc)


def terminal_metadata_for_exception(exc: BaseException) -> RolloutTerminalMetadata:
    exc_name = type(exc).__name__
    terminal_state: Literal[
        "max_turns_exceeded",
        "guardrail_tripped",
        "cancelled",
        "failed",
    ]
    if exc_name == "MaxTurnsExceeded":
        terminal_state = "max_turns_exceeded"
    elif "Guardrail" in exc_name:
        terminal_state = "guardrail_tripped"
    elif exc_name == "CancelledError":
        terminal_state = "cancelled"
    else:
        terminal_state = "failed"
    return RolloutTerminalMetadata(
        terminal_state=terminal_state,
        exception_type=exc_name,
        exception_message=str(exc) or None,
        has_final_output=False,
    )


def build_rollout_payload(
    *,
    input: str | list[TResponseInputItem],
    new_items: list[RunItem],
    final_output: Any,
    interruptions: list[ToolApprovalItem],
    terminal_metadata: RolloutTerminalMetadata,
) -> dict[str, Any]:
    input_items = _sanitize_memory_items(ItemHelpers.input_to_new_input_list(input))
    generated_items = _to_dump_compatible(
        _sanitize_memory_items(run_items_to_input_items(new_items))
    )

    serialized_interruptions = [
        _to_dump_compatible(interruption.raw_item)
        if not isinstance(interruption.raw_item, dict)
        else dict(interruption.raw_item)
        for interruption in interruptions
    ]

    payload: dict[str, Any] = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        "input": _to_dump_compatible(input_items),
        "generated_items": generated_items,
    }
    if serialized_interruptions:
        payload["interruptions"] = serialized_interruptions
    payload["terminal_metadata"] = terminal_metadata.model_dump(mode="json")
    if final_output is not None:
        payload["final_output"] = _to_dump_compatible(final_output)
    return payload


def build_rollout_payload_from_result(
    result: RunResultBase,
    *,
    exception: BaseException | None = None,
    input_override: str | list[TResponseInputItem] | None = None,
) -> dict[str, Any]:
    interruptions = list(getattr(result, "interruptions", []))
    return build_rollout_payload(
        input=input_override if input_override is not None else result.input,
        new_items=result.new_items,
        final_output=result.final_output,
        interruptions=interruptions,
        terminal_metadata=terminal_metadata_for_result(result, exception=exception),
    )

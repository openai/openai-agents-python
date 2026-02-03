"""
Tool execution helpers for the run pipeline. This module hosts execution-time helpers,
approval plumbing, and payload coercion. Action classes live in tool_actions.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_input_item_param import (
    ComputerCallOutputAcknowledgedSafetyCheck,
)
from openai.types.responses.response_input_param import McpApprovalResponse
from openai.types.responses.response_output_item import McpApprovalRequest

from ..agent import Agent
from ..agent_tool_state import consume_agent_tool_run_result, peek_agent_tool_run_result
from ..editor import ApplyPatchOperation, ApplyPatchResult
from ..exceptions import (
    AgentsException,
    ModelBehaviorError,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
    UserError,
)
from ..items import (
    ItemHelpers,
    MCPApprovalResponseItem,
    RunItem,
    RunItemBase,
    ToolApprovalItem,
    ToolCallOutputItem,
)
from ..logger import logger
from ..model_settings import ModelSettings
from ..run_config import RunConfig, ToolErrorFormatterArgs
from ..run_context import RunContextWrapper
from ..tool import (
    ApplyPatchTool,
    ComputerTool,
    ComputerToolSafetyCheckData,
    FunctionTool,
    FunctionToolResult,
    ShellActionRequest,
    ShellCallData,
    ShellCallOutcome,
    ShellCommandOutput,
    Tool,
    resolve_computer,
)
from ..tool_context import ToolContext
from ..tool_guardrails import (
    ToolInputGuardrailData,
    ToolInputGuardrailResult,
    ToolOutputGuardrailData,
    ToolOutputGuardrailResult,
)
from ..tracing import SpanError, function_span
from ..util import _coro, _error_tracing
from ..util._approvals import evaluate_needs_approval_setting
from .approvals import append_approval_error_output
from .items import (
    REJECTION_MESSAGE,
    extract_mcp_request_id,
    extract_mcp_request_id_from_run,
    function_rejection_item,
)
from .run_steps import ToolRunFunction
from .tool_use_tracker import AgentToolUseTracker

if TYPE_CHECKING:
    from ..lifecycle import RunHooks
    from .run_steps import (
        ToolRunApplyPatchCall,
        ToolRunComputerAction,
        ToolRunFunction,
        ToolRunLocalShellCall,
        ToolRunShellCall,
    )

__all__ = [
    "maybe_reset_tool_choice",
    "initialize_computer_tools",
    "extract_tool_call_id",
    "coerce_shell_call",
    "parse_apply_patch_custom_input",
    "parse_apply_patch_function_args",
    "extract_apply_patch_call_id",
    "coerce_apply_patch_operation",
    "normalize_apply_patch_result",
    "is_apply_patch_name",
    "normalize_shell_output",
    "serialize_shell_output",
    "resolve_exit_code",
    "render_shell_outputs",
    "truncate_shell_outputs",
    "normalize_max_output_length",
    "normalize_shell_output_entries",
    "format_shell_error",
    "build_litellm_json_tool_call",
    "process_hosted_mcp_approvals",
    "collect_manual_mcp_approvals",
    "index_approval_items_by_call_id",
    "should_keep_hosted_mcp_item",
    "resolve_approval_status",
    "resolve_approval_interruption",
    "resolve_approval_rejection_message",
    "function_needs_approval",
    "execute_function_tool_calls",
    "execute_local_shell_calls",
    "execute_shell_calls",
    "execute_apply_patch_calls",
    "execute_computer_actions",
    "execute_approved_tools",
]


# --------------------------
# Public helpers
# --------------------------


def maybe_reset_tool_choice(
    agent: Agent[Any],
    tool_use_tracker: AgentToolUseTracker,
    model_settings: ModelSettings,
) -> ModelSettings:
    """Reset tool_choice if the agent was forced to pick a tool previously and should be reset."""
    if agent.reset_tool_choice is True and tool_use_tracker.has_used_tools(agent):
        return dataclasses.replace(model_settings, tool_choice=None)
    return model_settings


async def initialize_computer_tools(
    *,
    tools: list[Tool],
    context_wrapper: RunContextWrapper[Any],
) -> None:
    """Resolve computer tools ahead of model invocation so each run gets its own instance."""
    computer_tools = [tool for tool in tools if isinstance(tool, ComputerTool)]
    if not computer_tools:
        return

    await asyncio.gather(
        *(resolve_computer(tool=tool, run_context=context_wrapper) for tool in computer_tools)
    )


def get_mapping_or_attr(target: Any, key: str) -> Any:
    """Allow mapping-or-attribute access so tool payloads can be dicts or objects."""
    if isinstance(target, Mapping):
        return target.get(key)
    return getattr(target, key, None)


def extract_tool_call_id(raw: Any) -> str | None:
    """Return a call ID from tool call payloads or approval items."""
    # OpenAI tool call payloads are documented to include a call_id/id so outputs can be matched.
    # See https://platform.openai.com/docs/guides/function-calling
    # We still guard against missing IDs to avoid hard failures on malformed or non-OpenAI inputs.
    if isinstance(raw, Mapping):
        candidate = raw.get("call_id") or raw.get("id")
        return candidate if isinstance(candidate, str) else None
    candidate = get_mapping_or_attr(raw, "call_id") or get_mapping_or_attr(raw, "id")
    return candidate if isinstance(candidate, str) else None


def extract_shell_call_id(tool_call: Any) -> str:
    """Ensure shell calls include a call_id before executing them."""
    value = extract_tool_call_id(tool_call)
    if not value:
        raise ModelBehaviorError("Shell call is missing call_id.")
    return str(value)


def coerce_shell_call(tool_call: Any) -> ShellCallData:
    """Normalize a shell call payload into ShellCallData for consistent execution."""
    call_id = extract_shell_call_id(tool_call)
    action_payload = get_mapping_or_attr(tool_call, "action")
    if action_payload is None:
        raise ModelBehaviorError("Shell call is missing an action payload.")

    commands_value = get_mapping_or_attr(action_payload, "commands")
    if not isinstance(commands_value, Sequence):
        raise ModelBehaviorError("Shell call action is missing commands.")
    commands: list[str] = []
    for entry in commands_value:
        if entry is None:
            continue
        commands.append(str(entry))
    if not commands:
        raise ModelBehaviorError("Shell call action must include at least one command.")

    timeout_value = (
        get_mapping_or_attr(action_payload, "timeout_ms")
        or get_mapping_or_attr(action_payload, "timeoutMs")
        or get_mapping_or_attr(action_payload, "timeout")
    )
    timeout_ms = int(timeout_value) if isinstance(timeout_value, (int, float)) else None

    max_length_value = get_mapping_or_attr(action_payload, "max_output_length")
    if max_length_value is None:
        max_length_value = get_mapping_or_attr(action_payload, "maxOutputLength")
    max_output_length = (
        int(max_length_value) if isinstance(max_length_value, (int, float)) else None
    )

    action = ShellActionRequest(
        commands=commands,
        timeout_ms=timeout_ms,
        max_output_length=max_output_length,
    )

    status_value = get_mapping_or_attr(tool_call, "status")
    status_literal: Literal["in_progress", "completed"] | None = None
    if isinstance(status_value, str):
        lowered = status_value.lower()
        if lowered in {"in_progress", "completed"}:
            status_literal = cast(Literal["in_progress", "completed"], lowered)

    return ShellCallData(call_id=call_id, action=action, status=status_literal, raw=tool_call)


def _parse_apply_patch_json(payload: str, *, label: str) -> dict[str, Any]:
    """Parse apply_patch JSON payloads with consistent error messages."""
    try:
        parsed = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise ModelBehaviorError(f"Invalid apply_patch {label} JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ModelBehaviorError(f"Apply patch {label} must be a JSON object.")
    return dict(parsed)


def parse_apply_patch_custom_input(input_json: str) -> dict[str, Any]:
    """Parse custom apply_patch tool input used when a tool passes raw JSON strings."""
    return _parse_apply_patch_json(input_json, label="input")


def parse_apply_patch_function_args(arguments: str) -> dict[str, Any]:
    """Parse apply_patch function tool arguments from the model."""
    return _parse_apply_patch_json(arguments, label="arguments")


def extract_apply_patch_call_id(tool_call: Any) -> str:
    """Ensure apply_patch calls include a call_id for approvals and tracing."""
    value = extract_tool_call_id(tool_call)
    if not value:
        raise ModelBehaviorError("Apply patch call is missing call_id.")
    return str(value)


def coerce_apply_patch_operation(
    tool_call: Any, *, context_wrapper: RunContextWrapper[Any]
) -> ApplyPatchOperation:
    """Normalize the tool payload into an ApplyPatchOperation the editor can consume."""
    raw_operation = get_mapping_or_attr(tool_call, "operation")
    if raw_operation is None:
        raise ModelBehaviorError("Apply patch call is missing an operation payload.")

    op_type_value = str(get_mapping_or_attr(raw_operation, "type"))
    if op_type_value not in {"create_file", "update_file", "delete_file"}:
        raise ModelBehaviorError(f"Unknown apply_patch operation: {op_type_value}")
    op_type_literal = cast(Literal["create_file", "update_file", "delete_file"], op_type_value)

    path = get_mapping_or_attr(raw_operation, "path")
    if not isinstance(path, str) or not path:
        raise ModelBehaviorError("Apply patch operation is missing a valid path.")

    diff_value = get_mapping_or_attr(raw_operation, "diff")
    if op_type_literal in {"create_file", "update_file"}:
        if not isinstance(diff_value, str) or not diff_value:
            raise ModelBehaviorError(
                f"Apply patch operation {op_type_literal} is missing the required diff payload."
            )
        diff: str | None = diff_value
    else:
        diff = None

    return ApplyPatchOperation(
        type=op_type_literal,
        path=str(path),
        diff=diff,
        ctx_wrapper=context_wrapper,
    )


def normalize_apply_patch_result(
    result: ApplyPatchResult | Mapping[str, Any] | str | None,
) -> ApplyPatchResult | None:
    """Coerce editor return values into ApplyPatchResult for consistent handling."""
    if result is None:
        return None
    if isinstance(result, ApplyPatchResult):
        return result
    if isinstance(result, Mapping):
        status = result.get("status")
        output = result.get("output")
        normalized_status = status if status in {"completed", "failed"} else None
        normalized_output = str(output) if output is not None else None
        return ApplyPatchResult(status=normalized_status, output=normalized_output)
    if isinstance(result, str):
        return ApplyPatchResult(output=result)
    return ApplyPatchResult(output=str(result))


def is_apply_patch_name(name: str | None, tool: ApplyPatchTool | None) -> bool:
    """Allow flexible matching for apply_patch so existing names keep working."""
    if not name:
        return False
    candidate = name.strip().lower()
    if candidate.startswith("apply_patch"):
        return True
    if tool and candidate == tool.name.strip().lower():
        return True
    return False


def normalize_shell_output(entry: ShellCommandOutput | Mapping[str, Any]) -> ShellCommandOutput:
    """Normalize shell output into ShellCommandOutput so downstream code sees a stable shape."""
    if isinstance(entry, ShellCommandOutput):
        return entry

    stdout = str(entry.get("stdout", "") or "")
    stderr = str(entry.get("stderr", "") or "")
    command_value = entry.get("command")
    provider_data_value = entry.get("provider_data")
    outcome_value = entry.get("outcome")

    outcome_type: Literal["exit", "timeout"] = "exit"
    exit_code_value: Any | None = None

    if isinstance(outcome_value, Mapping):
        type_value = outcome_value.get("type")
        if type_value == "timeout":
            outcome_type = "timeout"
        elif isinstance(type_value, str):
            outcome_type = "exit"
        exit_code_value = outcome_value.get("exit_code")
    else:
        status_str = str(entry.get("status", "completed") or "completed").lower()
        if status_str == "timeout":
            outcome_type = "timeout"
        if isinstance(outcome_value, str):
            if outcome_value == "failure":
                exit_code_value = 1
            elif outcome_value == "success":
                exit_code_value = 0
        if exit_code_value is None and "exit_code" in entry:
            exit_code_value = entry.get("exit_code")

    outcome = ShellCallOutcome(
        type=outcome_type,
        exit_code=_normalize_exit_code(exit_code_value),
    )

    return ShellCommandOutput(
        stdout=stdout,
        stderr=stderr,
        outcome=outcome,
        command=str(command_value) if command_value is not None else None,
        provider_data=cast(dict[str, Any], provider_data_value)
        if isinstance(provider_data_value, Mapping)
        else provider_data_value,
    )


def serialize_shell_output(output: ShellCommandOutput) -> dict[str, Any]:
    """Serialize ShellCommandOutput for persistence or cross-run transmission."""
    payload: dict[str, Any] = {
        "stdout": output.stdout,
        "stderr": output.stderr,
        "status": output.status,
        "outcome": {"type": output.outcome.type},
    }
    if output.outcome.type == "exit":
        payload["outcome"]["exit_code"] = output.outcome.exit_code
        if output.outcome.exit_code is not None:
            payload["exit_code"] = output.outcome.exit_code
    if output.command is not None:
        payload["command"] = output.command
    if output.provider_data:
        payload["provider_data"] = output.provider_data
    return payload


def resolve_exit_code(raw_exit_code: Any, outcome_status: str | None) -> int:
    """Fallback logic to produce an exit code when providers omit one."""
    normalized = _normalize_exit_code(raw_exit_code)
    if normalized is not None:
        return normalized

    normalized_status = (outcome_status or "").lower()
    if normalized_status == "success":
        return 0
    if normalized_status == "failure":
        return 1
    return 0


def render_shell_outputs(outputs: Sequence[ShellCommandOutput]) -> str:
    """Render shell outputs into human-readable text for tool responses."""
    if not outputs:
        return "(no output)"

    rendered_chunks: list[str] = []
    for result in outputs:
        chunk_lines: list[str] = []
        if result.command:
            chunk_lines.append(f"$ {result.command}")

        stdout = result.stdout.rstrip("\n")
        stderr = result.stderr.rstrip("\n")

        if stdout:
            chunk_lines.append(stdout)
        if stderr:
            if stdout:
                chunk_lines.append("")
            chunk_lines.append("stderr:")
            chunk_lines.append(stderr)

        if result.exit_code not in (None, 0):
            chunk_lines.append(f"exit code: {result.exit_code}")
        if result.status == "timeout":
            chunk_lines.append("status: timeout")

        chunk = "\n".join(chunk_lines).strip()
        rendered_chunks.append(chunk if chunk else "(no output)")

    return "\n\n".join(rendered_chunks)


def truncate_shell_outputs(
    outputs: Sequence[ShellCommandOutput], max_length: int
) -> list[ShellCommandOutput]:
    """Truncate shell output streams to a maximum combined length."""
    if max_length <= 0:
        return [
            ShellCommandOutput(
                stdout="",
                stderr="",
                outcome=output.outcome,
                command=output.command,
                provider_data=output.provider_data,
            )
            for output in outputs
        ]

    remaining = max_length
    truncated: list[ShellCommandOutput] = []
    for output in outputs:
        stdout = ""
        stderr = ""
        if remaining > 0 and output.stdout:
            stdout = output.stdout[:remaining]
            remaining -= len(stdout)
        if remaining > 0 and output.stderr:
            stderr = output.stderr[:remaining]
            remaining -= len(stderr)
        truncated.append(
            ShellCommandOutput(
                stdout=stdout,
                stderr=stderr,
                outcome=output.outcome,
                command=output.command,
                provider_data=output.provider_data,
            )
        )

    return truncated


def normalize_shell_output_entries(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize raw shell output entries into the model-facing payload."""
    structured_output: list[dict[str, Any]] = []
    for entry in entries:
        sanitized = dict(entry)
        status_value = sanitized.pop("status", None)
        sanitized.pop("provider_data", None)
        raw_exit_code = sanitized.pop("exit_code", None)
        sanitized.pop("command", None)
        outcome_value = sanitized.get("outcome")
        if isinstance(outcome_value, str):
            resolved_type = "exit"
            if status_value == "timeout":
                resolved_type = "timeout"
            outcome_payload: dict[str, Any] = {"type": resolved_type}
            if resolved_type == "exit":
                outcome_payload["exit_code"] = resolve_exit_code(raw_exit_code, outcome_value)
            sanitized["outcome"] = outcome_payload
        elif isinstance(outcome_value, dict):
            outcome_payload = dict(outcome_value)
            outcome_status = outcome_payload.pop("status", None)
            outcome_type = outcome_payload.get("type")
            if outcome_type != "timeout":
                status_str = outcome_status if isinstance(outcome_status, str) else None
                outcome_payload.setdefault(
                    "exit_code",
                    resolve_exit_code(raw_exit_code, status_str),
                )
            sanitized["outcome"] = outcome_payload
        structured_output.append(sanitized)
    return structured_output


def normalize_max_output_length(value: int | None) -> int | None:
    """Clamp negative max output lengths to zero while preserving None."""
    if value is None:
        return None
    return max(0, value)


def format_shell_error(error: Exception | BaseException | Any) -> str:
    """Best-effort stringify of shell errors to keep tool failures readable."""
    if isinstance(error, Exception):
        message = str(error)
        return message or error.__class__.__name__
    try:
        return str(error)
    except Exception:  # pragma: no cover - fallback only
        return repr(error)


def build_litellm_json_tool_call(output: ResponseFunctionToolCall) -> FunctionTool:
    """Wrap a JSON string result in a FunctionTool so LiteLLM can stream it."""

    async def on_invoke_tool(_ctx: ToolContext[Any], value: Any) -> Any:
        """Deserialize JSON strings so LiteLLM callers receive structured data."""
        if isinstance(value, str):
            return json.loads(value)
        return value

    return FunctionTool(
        name=output.name,
        description=output.name,
        params_json_schema={},
        on_invoke_tool=on_invoke_tool,
        strict_json_schema=True,
        is_enabled=True,
    )


async def resolve_approval_status(
    *,
    tool_name: str,
    call_id: str,
    raw_item: Any,
    agent: Agent[Any],
    context_wrapper: RunContextWrapper[Any],
    on_approval: Callable[[RunContextWrapper[Any], ToolApprovalItem], Any] | None = None,
) -> tuple[bool | None, ToolApprovalItem]:
    """Build approval item, run on_approval hook if needed, and return latest approval status."""
    approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name=tool_name)
    approval_status = context_wrapper.get_approval_status(
        tool_name,
        call_id,
        existing_pending=approval_item,
    )
    if approval_status is None and on_approval:
        decision_result = on_approval(context_wrapper, approval_item)
        if inspect.isawaitable(decision_result):
            decision_result = await decision_result
        if isinstance(decision_result, Mapping):
            if decision_result.get("approve") is True:
                context_wrapper.approve_tool(approval_item)
            elif decision_result.get("approve") is False:
                context_wrapper.reject_tool(approval_item)
        approval_status = context_wrapper.get_approval_status(
            tool_name,
            call_id,
            existing_pending=approval_item,
        )
    return approval_status, approval_item


def resolve_approval_interruption(
    approval_status: bool | None,
    approval_item: ToolApprovalItem,
    *,
    rejection_factory: Callable[[], RunItem],
) -> RunItem | ToolApprovalItem | None:
    """Return a rejection or pending approval item when approval is required."""
    if approval_status is False:
        return rejection_factory()
    if approval_status is not True:
        return approval_item
    return None


async def resolve_approval_rejection_message(
    *,
    context_wrapper: RunContextWrapper[Any],
    run_config: RunConfig,
    tool_type: Literal["function", "computer", "shell", "apply_patch"],
    tool_name: str,
    call_id: str,
) -> str:
    """Resolve model-visible output text for approval rejections."""
    formatter = run_config.tool_error_formatter
    if formatter is None:
        return REJECTION_MESSAGE

    try:
        maybe_message = formatter(
            ToolErrorFormatterArgs(
                kind="approval_rejected",
                tool_type=tool_type,
                tool_name=tool_name,
                call_id=call_id,
                default_message=REJECTION_MESSAGE,
                run_context=context_wrapper,
            )
        )
        message = await maybe_message if inspect.isawaitable(maybe_message) else maybe_message
    except Exception as exc:
        logger.error("Tool error formatter failed for %s: %s", tool_name, exc)
        return REJECTION_MESSAGE

    if message is None:
        return REJECTION_MESSAGE

    if not isinstance(message, str):
        logger.error(
            "Tool error formatter returned non-string for %s: %s",
            tool_name,
            type(message).__name__,
        )
        return REJECTION_MESSAGE

    return message


async def function_needs_approval(
    function_tool: FunctionTool,
    context_wrapper: RunContextWrapper[Any],
    tool_call: ResponseFunctionToolCall,
) -> bool:
    """Evaluate a function tool's needs_approval setting with parsed args."""
    parsed_args: dict[str, Any] = {}
    if callable(function_tool.needs_approval):
        try:
            parsed_args = json.loads(tool_call.arguments or "{}")
        except json.JSONDecodeError:
            parsed_args = {}
    needs_approval = await evaluate_needs_approval_setting(
        function_tool.needs_approval,
        context_wrapper,
        parsed_args,
        tool_call.call_id,
    )
    return bool(needs_approval)


def process_hosted_mcp_approvals(
    *,
    original_pre_step_items: Sequence[RunItem],
    mcp_approval_requests: Sequence[Any],
    context_wrapper: RunContextWrapper[Any],
    agent: Agent[Any],
    append_item: Callable[[RunItem], None],
) -> tuple[list[ToolApprovalItem], set[str]]:
    """Filter hosted MCP outputs and merge manual approvals so only coherent items remain."""
    hosted_mcp_approvals_by_id: dict[str, ToolApprovalItem] = {}
    for item in original_pre_step_items:
        if not isinstance(item, ToolApprovalItem):
            continue
        raw = item.raw_item
        if not _is_hosted_mcp_approval_request(raw):
            continue
        request_id = extract_mcp_request_id(raw)
        if request_id:
            hosted_mcp_approvals_by_id[request_id] = item

    pending_hosted_mcp_approvals: list[ToolApprovalItem] = []
    pending_hosted_mcp_approval_ids: set[str] = set()

    for mcp_run in mcp_approval_requests:
        request_id = extract_mcp_request_id_from_run(mcp_run)
        # MCP approval requests are documented to include an id used as approval_request_id.
        # See https://platform.openai.com/docs/guides/tools-connectors-mcp#approvals
        approval_item = hosted_mcp_approvals_by_id.get(request_id) if request_id else None
        if not approval_item or not request_id:
            continue

        tool_name = RunContextWrapper._resolve_tool_name(approval_item)
        approved = context_wrapper.get_approval_status(
            tool_name=tool_name,
            call_id=request_id,
            existing_pending=approval_item,
        )

        if approved is not None:
            raw_item: McpApprovalResponse = {
                "type": "mcp_approval_response",
                "approval_request_id": request_id,
                "approve": approved,
            }
            response_item = MCPApprovalResponseItem(raw_item=raw_item, agent=agent)
            append_item(response_item)
            continue

        if approval_item not in pending_hosted_mcp_approvals:
            pending_hosted_mcp_approvals.append(approval_item)
        pending_hosted_mcp_approval_ids.add(request_id)
        append_item(approval_item)

    return pending_hosted_mcp_approvals, pending_hosted_mcp_approval_ids


def collect_manual_mcp_approvals(
    *,
    agent: Agent[Any],
    requests: Sequence[Any],
    context_wrapper: RunContextWrapper[Any],
    existing_pending_by_call_id: Mapping[str, ToolApprovalItem] | None = None,
) -> tuple[list[MCPApprovalResponseItem], list[ToolApprovalItem]]:
    """Bridge hosted MCP approval requests with manual approvals to keep state consistent."""
    pending_lookup = existing_pending_by_call_id or {}
    approved: list[MCPApprovalResponseItem] = []
    pending: list[ToolApprovalItem] = []
    seen_request_ids: set[str] = set()

    for request in requests:
        request_item = get_mapping_or_attr(request, "request_item")
        request_id = extract_mcp_request_id_from_run(request)
        # The Responses API returns mcp_approval_request items with an id to correlate approvals.
        # See https://platform.openai.com/docs/guides/tools-connectors-mcp#approvals
        if request_id and request_id in seen_request_ids:
            continue
        if request_id:
            seen_request_ids.add(request_id)

        tool_name = RunContextWrapper._to_str_or_none(getattr(request_item, "name", None))
        tool_name = tool_name or get_mapping_or_attr(request, "mcp_tool").name

        existing_pending = pending_lookup.get(request_id or "")
        approval_status = context_wrapper.get_approval_status(
            tool_name, request_id or "", existing_pending=existing_pending
        )

        if approval_status is not None and request_id:
            approval_response_raw: McpApprovalResponse = {
                "type": "mcp_approval_response",
                "approval_request_id": request_id,
                "approve": approval_status,
            }
            approved.append(MCPApprovalResponseItem(raw_item=approval_response_raw, agent=agent))
            continue

        if approval_status is not None:
            continue

        pending.append(
            existing_pending
            or ToolApprovalItem(
                agent=agent,
                raw_item=request_item,
                tool_name=tool_name,
            )
        )

    return approved, pending


def index_approval_items_by_call_id(items: Sequence[RunItem]) -> dict[str, ToolApprovalItem]:
    """Build a mapping of tool call IDs to pending approval items."""
    approvals: dict[str, ToolApprovalItem] = {}
    for item in items:
        if not isinstance(item, ToolApprovalItem):
            continue
        call_id = extract_tool_call_id(item.raw_item)
        if call_id:
            approvals[call_id] = item
    return approvals


def should_keep_hosted_mcp_item(
    item: RunItem,
    *,
    pending_hosted_mcp_approvals: Sequence[ToolApprovalItem],
    pending_hosted_mcp_approval_ids: set[str],
) -> bool:
    """Keep only hosted MCP approvals that match pending requests from the provider."""
    if not isinstance(item, ToolApprovalItem):
        return True
    if not _is_hosted_mcp_approval_request(item.raw_item):
        return False
    request_id = extract_mcp_request_id(item.raw_item)
    return item in pending_hosted_mcp_approvals or (
        request_id is not None and request_id in pending_hosted_mcp_approval_ids
    )


async def execute_function_tool_calls(
    *,
    agent: Agent[Any],
    tool_runs: list[ToolRunFunction],
    hooks: RunHooks[Any],
    context_wrapper: RunContextWrapper[Any],
    config: RunConfig,
) -> tuple[
    list[FunctionToolResult], list[ToolInputGuardrailResult], list[ToolOutputGuardrailResult]
]:
    """Execute function tool calls with approvals, guardrails, and hooks."""
    tool_input_guardrail_results: list[ToolInputGuardrailResult] = []
    tool_output_guardrail_results: list[ToolOutputGuardrailResult] = []

    async def run_single_tool(func_tool: FunctionTool, tool_call: ResponseFunctionToolCall) -> Any:
        with function_span(func_tool.name) as span_fn:
            tool_context = ToolContext.from_agent_context(
                context_wrapper,
                tool_call.call_id,
                tool_call=tool_call,
            )
            agent_hooks = agent.hooks
            if config.trace_include_sensitive_data:
                span_fn.span_data.input = tool_call.arguments
            try:
                needs_approval_result = await function_needs_approval(
                    func_tool,
                    context_wrapper,
                    tool_call,
                )

                if needs_approval_result:
                    approval_status = context_wrapper.get_approval_status(
                        func_tool.name,
                        tool_call.call_id,
                    )

                    if approval_status is None:
                        approval_item = ToolApprovalItem(
                            agent=agent, raw_item=tool_call, tool_name=func_tool.name
                        )
                        return FunctionToolResult(
                            tool=func_tool, output=None, run_item=approval_item
                        )

                    if approval_status is False:
                        rejection_message = await resolve_approval_rejection_message(
                            context_wrapper=context_wrapper,
                            run_config=config,
                            tool_type="function",
                            tool_name=func_tool.name,
                            call_id=tool_call.call_id,
                        )
                        span_fn.set_error(
                            SpanError(
                                message=rejection_message,
                                data={
                                    "tool_name": func_tool.name,
                                    "error": (
                                        f"Tool execution for {tool_call.call_id} "
                                        "was manually rejected by user."
                                    ),
                                },
                            )
                        )
                        result = rejection_message
                        span_fn.span_data.output = result
                        return FunctionToolResult(
                            tool=func_tool,
                            output=result,
                            run_item=function_rejection_item(
                                agent,
                                tool_call,
                                rejection_message=rejection_message,
                            ),
                        )

                rejected_message = await _execute_tool_input_guardrails(
                    func_tool=func_tool,
                    tool_context=tool_context,
                    agent=agent,
                    tool_input_guardrail_results=tool_input_guardrail_results,
                )

                if rejected_message is not None:
                    final_result = rejected_message
                else:
                    await asyncio.gather(
                        hooks.on_tool_start(tool_context, agent, func_tool),
                        (
                            agent_hooks.on_tool_start(tool_context, agent, func_tool)
                            if agent_hooks
                            else _coro.noop_coroutine()
                        ),
                    )
                    real_result = await func_tool.on_invoke_tool(tool_context, tool_call.arguments)

                    final_result = await _execute_tool_output_guardrails(
                        func_tool=func_tool,
                        tool_context=tool_context,
                        agent=agent,
                        real_result=real_result,
                        tool_output_guardrail_results=tool_output_guardrail_results,
                    )

                    await asyncio.gather(
                        hooks.on_tool_end(tool_context, agent, func_tool, final_result),
                        (
                            agent_hooks.on_tool_end(tool_context, agent, func_tool, final_result)
                            if agent_hooks
                            else _coro.noop_coroutine()
                        ),
                    )
                result = final_result
            except Exception as e:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Error running tool",
                        data={"tool_name": func_tool.name, "error": str(e)},
                    )
                )
                if isinstance(e, AgentsException):
                    raise e
                raise UserError(f"Error running tool {func_tool.name}: {e}") from e

            if config.trace_include_sensitive_data:
                span_fn.span_data.output = result
        return result

    tasks = []
    for tool_run in tool_runs:
        function_tool = tool_run.function_tool
        tasks.append(run_single_tool(function_tool, tool_run.tool_call))

    results = await asyncio.gather(*tasks)

    function_tool_results = []
    for tool_run, result in zip(tool_runs, results):
        if isinstance(result, FunctionToolResult):
            nested_run_result = consume_agent_tool_run_result(tool_run.tool_call)
            if nested_run_result:
                result.agent_run_result = nested_run_result
                nested_interruptions_from_result: list[ToolApprovalItem] = (
                    nested_run_result.interruptions
                    if hasattr(nested_run_result, "interruptions")
                    else []
                )
                if nested_interruptions_from_result:
                    result.interruptions = nested_interruptions_from_result

            function_tool_results.append(result)
        else:
            nested_run_result = peek_agent_tool_run_result(tool_run.tool_call)
            nested_interruptions: list[ToolApprovalItem] = []
            if nested_run_result:
                nested_interruptions = (
                    nested_run_result.interruptions
                    if hasattr(nested_run_result, "interruptions")
                    else []
                )
            if nested_run_result and not nested_interruptions:
                nested_run_result = consume_agent_tool_run_result(tool_run.tool_call)
            elif nested_run_result is None:
                nested_run_result = consume_agent_tool_run_result(tool_run.tool_call)
                if nested_run_result:
                    nested_interruptions = (
                        nested_run_result.interruptions
                        if hasattr(nested_run_result, "interruptions")
                        else []
                    )

            run_item: RunItem | None = None
            if not nested_interruptions:
                run_item = ToolCallOutputItem(
                    output=result,
                    raw_item=ItemHelpers.tool_call_output_item(tool_run.tool_call, result),
                    agent=agent,
                )
            else:
                # Skip tool output until nested interruptions are resolved.
                run_item = None

            function_tool_results.append(
                FunctionToolResult(
                    tool=tool_run.function_tool,
                    output=result,
                    run_item=run_item,
                    interruptions=nested_interruptions,
                    agent_run_result=nested_run_result,
                )
            )

    return function_tool_results, tool_input_guardrail_results, tool_output_guardrail_results


async def execute_local_shell_calls(
    *,
    agent: Agent[Any],
    calls: list[ToolRunLocalShellCall],
    context_wrapper: RunContextWrapper[Any],
    hooks: RunHooks[Any],
    config: RunConfig,
) -> list[RunItem]:
    """Run local shell tool calls serially and wrap outputs."""
    from .tool_actions import LocalShellAction

    results: list[RunItem] = []
    for call in calls:
        results.append(
            await LocalShellAction.execute(
                agent=agent,
                call=call,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=config,
            )
        )
    return results


async def execute_shell_calls(
    *,
    agent: Agent[Any],
    calls: list[ToolRunShellCall],
    context_wrapper: RunContextWrapper[Any],
    hooks: RunHooks[Any],
    config: RunConfig,
) -> list[RunItem]:
    """Run shell tool calls serially and wrap outputs."""
    from .tool_actions import ShellAction

    results: list[RunItem] = []
    for call in calls:
        results.append(
            await ShellAction.execute(
                agent=agent,
                call=call,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=config,
            )
        )
    return results


async def execute_apply_patch_calls(
    *,
    agent: Agent[Any],
    calls: list[ToolRunApplyPatchCall],
    context_wrapper: RunContextWrapper[Any],
    hooks: RunHooks[Any],
    config: RunConfig,
) -> list[RunItem]:
    """Run apply_patch tool calls serially and normalize outputs."""
    from .tool_actions import ApplyPatchAction

    results: list[RunItem] = []
    for call in calls:
        results.append(
            await ApplyPatchAction.execute(
                agent=agent,
                call=call,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=config,
            )
        )
    return results


async def execute_computer_actions(
    *,
    agent: Agent[Any],
    actions: list[ToolRunComputerAction],
    hooks: RunHooks[Any],
    context_wrapper: RunContextWrapper[Any],
    config: RunConfig,
) -> list[RunItem]:
    """Run computer actions serially and emit screenshot outputs."""
    from .tool_actions import ComputerAction

    results: list[RunItem] = []
    for action in actions:
        acknowledged: list[ComputerCallOutputAcknowledgedSafetyCheck] | None = None
        if action.tool_call.pending_safety_checks and action.computer_tool.on_safety_check:
            acknowledged = []
            for check in action.tool_call.pending_safety_checks:
                data = ComputerToolSafetyCheckData(
                    ctx_wrapper=context_wrapper,
                    agent=agent,
                    tool_call=action.tool_call,
                    safety_check=check,
                )
                maybe = action.computer_tool.on_safety_check(data)
                ack = await maybe if inspect.isawaitable(maybe) else maybe
                if ack:
                    acknowledged.append(
                        ComputerCallOutputAcknowledgedSafetyCheck(
                            id=check.id,
                            code=check.code,
                            message=check.message,
                        )
                    )
                else:
                    raise UserError("Computer tool safety check was not acknowledged")

        results.append(
            await ComputerAction.execute(
                agent=agent,
                action=action,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=config,
                acknowledged_safety_checks=acknowledged,
            )
        )

    return results


async def execute_approved_tools(
    *,
    agent: Agent[Any],
    interruptions: list[Any],
    context_wrapper: RunContextWrapper[Any],
    generated_items: list[RunItem],
    run_config: RunConfig,
    hooks: RunHooks[Any],
    all_tools: list[Tool] | None = None,
) -> None:
    """Execute tools that have been approved after an interruption (HITL resume path)."""
    tool_runs: list[ToolRunFunction] = []
    tool_map: dict[str, Tool] = {tool.name: tool for tool in all_tools or []}

    def _append_error(message: str, *, tool_call: Any, tool_name: str, call_id: str) -> None:
        append_approval_error_output(
            message=message,
            tool_call=tool_call,
            tool_name=tool_name,
            call_id=call_id,
            generated_items=generated_items,
            agent=agent,
        )

    async def _resolve_tool_run(
        interruption: Any,
    ) -> tuple[ResponseFunctionToolCall, FunctionTool, str, str] | None:
        tool_call = interruption.raw_item
        tool_name = interruption.name or RunContextWrapper._resolve_tool_name(interruption)
        if not tool_name:
            _append_error(
                message="Tool approval item missing tool name.",
                tool_call=tool_call,
                tool_name="unknown",
                call_id="unknown",
            )
            return None

        call_id = extract_tool_call_id(tool_call)
        if not call_id:
            _append_error(
                message="Tool approval item missing call ID.",
                tool_call=tool_call,
                tool_name=tool_name,
                call_id="unknown",
            )
            return None

        approval_status = context_wrapper.get_approval_status(
            tool_name, call_id, existing_pending=interruption
        )
        if approval_status is False:
            resolved_tool = tool_map.get(tool_name)
            message = REJECTION_MESSAGE
            if isinstance(resolved_tool, FunctionTool):
                message = await resolve_approval_rejection_message(
                    context_wrapper=context_wrapper,
                    run_config=run_config,
                    tool_type="function",
                    tool_name=tool_name,
                    call_id=call_id,
                )
            _append_error(
                message=message,
                tool_call=tool_call,
                tool_name=tool_name,
                call_id=call_id,
            )
            return None

        if approval_status is not True:
            _append_error(
                message="Tool approval status unclear.",
                tool_call=tool_call,
                tool_name=tool_name,
                call_id=call_id,
            )
            return None

        tool = tool_map.get(tool_name)
        if tool is None:
            _append_error(
                message=f"Tool '{tool_name}' not found.",
                tool_call=tool_call,
                tool_name=tool_name,
                call_id=call_id,
            )
            return None

        if not isinstance(tool, FunctionTool):
            _append_error(
                message=f"Tool '{tool_name}' is not a function tool.",
                tool_call=tool_call,
                tool_name=tool_name,
                call_id=call_id,
            )
            return None

        if not isinstance(tool_call, ResponseFunctionToolCall):
            _append_error(
                message=(
                    f"Tool '{tool_name}' approval item has invalid raw_item type for execution."
                ),
                tool_call=tool_call,
                tool_name=tool_name,
                call_id=call_id,
            )
            return None

        return tool_call, tool, tool_name, call_id

    for interruption in interruptions:
        resolved = await _resolve_tool_run(interruption)
        if resolved is None:
            continue
        tool_call, tool, tool_name, _ = resolved
        tool_runs.append(ToolRunFunction(function_tool=tool, tool_call=tool_call))

    if tool_runs:
        function_results, _, _ = await execute_function_tool_calls(
            agent=agent,
            tool_runs=tool_runs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )
        for result in function_results:
            if isinstance(result.run_item, RunItemBase):
                generated_items.append(result.run_item)


# --------------------------
# Private helpers
# --------------------------


async def _execute_tool_input_guardrails(
    *,
    func_tool: FunctionTool,
    tool_context: ToolContext[Any],
    agent: Agent[Any],
    tool_input_guardrail_results: list[ToolInputGuardrailResult],
) -> str | None:
    """Execute input guardrails for a tool call and return a rejection message if any."""
    if not func_tool.tool_input_guardrails:
        return None

    for guardrail in func_tool.tool_input_guardrails:
        gr_out = await guardrail.run(
            ToolInputGuardrailData(
                context=tool_context,
                agent=agent,
            )
        )

        tool_input_guardrail_results.append(
            ToolInputGuardrailResult(
                guardrail=guardrail,
                output=gr_out,
            )
        )

        if gr_out.behavior["type"] == "raise_exception":
            raise ToolInputGuardrailTripwireTriggered(guardrail=guardrail, output=gr_out)
        elif gr_out.behavior["type"] == "reject_content":
            return gr_out.behavior["message"]

    return None


async def _execute_tool_output_guardrails(
    *,
    func_tool: FunctionTool,
    tool_context: ToolContext[Any],
    agent: Agent[Any],
    real_result: Any,
    tool_output_guardrail_results: list[ToolOutputGuardrailResult],
) -> Any:
    """Execute output guardrails for a tool call and return the final result."""
    if not func_tool.tool_output_guardrails:
        return real_result

    final_result = real_result
    for output_guardrail in func_tool.tool_output_guardrails:
        gr_out = await output_guardrail.run(
            ToolOutputGuardrailData(
                context=tool_context,
                agent=agent,
                output=real_result,
            )
        )

        tool_output_guardrail_results.append(
            ToolOutputGuardrailResult(
                guardrail=output_guardrail,
                output=gr_out,
            )
        )

        if gr_out.behavior["type"] == "raise_exception":
            raise ToolOutputGuardrailTripwireTriggered(guardrail=output_guardrail, output=gr_out)
        elif gr_out.behavior["type"] == "reject_content":
            final_result = gr_out.behavior["message"]
            break

    return final_result


def _normalize_exit_code(value: Any) -> int | None:
    """Convert arbitrary exit code types into an int if possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_hosted_mcp_approval_request(raw_item: Any) -> bool:
    """Detect hosted MCP approval request payloads emitted by the provider."""
    if isinstance(raw_item, McpApprovalRequest):
        return True
    if not isinstance(raw_item, dict):
        return False
    provider_data = raw_item.get("provider_data", {})
    return (
        raw_item.get("type") == "hosted_tool_call"
        and provider_data.get("type") == "mcp_approval_request"
    )

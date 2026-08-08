from __future__ import annotations

import asyncio
import dataclasses as _dc
import inspect
import json
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from typing import Any, TypeVar, cast

from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_input_param import McpApprovalResponse

from .._tool_identity import (
    FunctionToolLookupKey,
    get_function_tool_lookup_key_for_call,
    get_function_tool_lookup_key_for_tool,
    get_tool_call_namespace,
)
from .._tool_invocation import (
    tool_invocation_call_id,
    tool_invocation_identity,
    tool_output_identity,
)
from ..agent import Agent
from ..exceptions import ModelBehaviorError, UserError
from ..items import (
    HandoffCallItem,
    HandoffOutputItem,
    ItemHelpers,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    ReasoningItem,
    RunItem,
    RunItemBase,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from ..run_context import RunContextWrapper
from ..tool import FunctionTool, MCPToolApprovalRequest, get_function_tool_origin
from ..tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from ..util._asyncio_tasks import gather_with_cancel
from .agent_bindings import AgentBindings
from .run_steps import (
    ProcessedResponse,
    ToolRunApplyPatchCall,
    ToolRunComputerAction,
    ToolRunCustom,
    ToolRunFunction,
    ToolRunLocalShellCall,
    ToolRunMCPApprovalRequest,
    ToolRunShellCall,
)
from .tool_execution import (
    collect_manual_mcp_approvals,
    execute_apply_patch_calls,
    execute_computer_actions,
    execute_custom_tool_calls,
    execute_function_tool_calls,
    execute_local_shell_calls,
    execute_shell_calls,
    get_mapping_or_attr,
)

T = TypeVar("T")

__all__ = [
    "execute_mcp_approval_requests",
    "_build_tool_output_index",
    "_dedupe_tool_call_items",
    "_dedupe_processed_response_invocations",
    "_register_tool_call_items",
    "_validate_unresolved_function_calls",
    "ToolExecutionPlan",
    "_build_plan_for_fresh_turn",
    "_build_plan_for_resume_turn",
    "_collect_mcp_approval_plan",
    "_collect_tool_interruptions",
    "_build_tool_result_items",
    "_make_unique_item_appender",
    "_collect_runs_by_approval",
    "_apply_manual_mcp_approvals",
    "_append_mcp_callback_results",
    "_select_function_tool_runs_for_resume",
    "_execute_tool_plan",
]


def _hashable_identity_value(value: Any) -> Hashable | None:
    """Convert a tool call field into a stable, hashable representation."""
    if value is None:
        return None
    if isinstance(value, dict | list | tuple):
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except Exception:
            return repr(value)
    if isinstance(value, Hashable):
        return value
    return str(value)


def _tool_call_identity(raw: Any) -> tuple[str | None, str | None, Hashable | None]:
    """Return a tuple that identifies a tool call when call_id/id may be missing."""
    call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None)
    name = getattr(raw, "name", None)
    args = getattr(raw, "arguments", None)
    if args is None:
        args = getattr(raw, "input", None)
    if isinstance(raw, dict):
        call_id = raw.get("call_id") or raw.get("id") or call_id
        name = raw.get("name", name)
        args = raw.get("arguments", args)
        if args is None:
            args = raw.get("input")
    return call_id, name, _hashable_identity_value(args)


async def execute_mcp_approval_requests(
    *,
    agent: Agent[Any],
    approval_requests: list[ToolRunMCPApprovalRequest],
    context_wrapper: RunContextWrapper[Any],
) -> list[RunItem]:
    """Run hosted MCP approval callbacks and return approval response items."""

    approval_requests, _ = _preflight_mcp_approval_requests(approval_requests)

    async def run_single_approval(approval_request: ToolRunMCPApprovalRequest) -> RunItem:
        request_item = approval_request.request_item
        request_id = (
            request_item.id
            if hasattr(request_item, "id")
            else cast(dict[str, Any], request_item).get("id", "")
        )
        approval_item = ToolApprovalItem(
            agent=agent,
            raw_item=request_item,
            tool_name=get_mapping_or_attr(request_item, "name"),
        )
        approval_status = context_wrapper.get_approval_status(
            approval_item.tool_name or "",
            request_id,
            existing_pending=approval_item,
            current_invocation=approval_item,
        )
        reason = context_wrapper.get_rejection_message(
            approval_item.tool_name or "",
            request_id,
            existing_pending=approval_item,
        )
        if approval_status is None:
            invocation_status = context_wrapper._tool_invocation_status(request_item)
            if invocation_status is None:
                raise ModelBehaviorError(
                    "Hosted MCP approval requests require a canonical invocation identity."
                )
            if invocation_status[2]:
                raise ModelBehaviorError(
                    "A Hosted MCP approval callback already ran, but its response was not "
                    "committed. Start a new request instead of retrying the invocation."
                )
            context_wrapper._mark_tool_invocation_executed(request_item)
            callback = approval_request.mcp_tool.on_approval_request
            assert callback is not None, "Callback is required for MCP approval requests"
            maybe_awaitable_result = callback(
                MCPToolApprovalRequest(context_wrapper, approval_request.request_item)
            )
            if inspect.isawaitable(maybe_awaitable_result):
                result = await maybe_awaitable_result
            else:
                result = maybe_awaitable_result
            approval_status = result["approve"]
            reason = result.get("reason", None)
            if approval_status:
                context_wrapper.approve_tool(approval_item)
            else:
                context_wrapper.reject_tool(
                    approval_item,
                    rejection_message=reason if isinstance(reason, str) else None,
                )
        raw_item: McpApprovalResponse = {
            "approval_request_id": request_id,
            "approve": approval_status,
            "type": "mcp_approval_response",
        }
        if not approval_status and reason:
            raw_item["reason"] = reason
        ItemHelpers.copy_tool_call_caller(request_item, raw_item)
        return MCPApprovalResponseItem(
            raw_item=raw_item,
            agent=agent,
        )

    tasks = [run_single_approval(approval_request) for approval_request in approval_requests]
    return list(await gather_with_cancel(*tasks))


def _preflight_mcp_approval_requests(
    approval_requests: Sequence[ToolRunMCPApprovalRequest],
) -> tuple[list[ToolRunMCPApprovalRequest], set[int]]:
    """Reject changed same-ID MCP siblings and coalesce exact duplicates."""
    seen_by_call: dict[tuple[str, str], tuple[str, str, str]] = {}
    deduped: list[ToolRunMCPApprovalRequest] = []
    skipped_raw_item_ids: set[int] = set()
    for approval_request in approval_requests:
        raw_item = approval_request.request_item
        identity = tool_invocation_identity(raw_item)
        if identity is None:
            deduped.append(approval_request)
            continue
        call_key = identity[:2]
        existing_identity = seen_by_call.get(call_key)
        if existing_identity is None:
            seen_by_call[call_key] = identity
            deduped.append(approval_request)
            continue
        if existing_identity != identity:
            raise ModelBehaviorError(
                "Model reused an approval-gated tool call ID for a different invocation. "
                "Use a unique call ID for each approval-gated invocation."
            )
        skipped_raw_item_ids.add(id(raw_item))
    return deduped, skipped_raw_item_ids


def _build_tool_output_index(items: Sequence[RunItem]) -> set[tuple[str, str]]:
    """Index tool call output items by (type, call_id) for fast lookups."""
    index: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, ToolCallOutputItem):
            continue
        raw_item = item.raw_item
        if isinstance(raw_item, dict):
            raw_type = raw_item.get("type")
            call_id = raw_item.get("call_id") or raw_item.get("id")
        else:
            raw_type = getattr(raw_item, "type", None)
            call_id = getattr(raw_item, "call_id", None) or getattr(raw_item, "id", None)
        if isinstance(raw_type, str) and isinstance(call_id, str):
            index.add((raw_type, call_id))
    return index


def _dedupe_tool_call_items(
    *,
    existing_items: Sequence[RunItem],
    new_items: Sequence[RunItem],
    skipped_raw_item_ids: set[int],
) -> list[RunItem]:
    """Return new items while skipping tool call duplicates already seen by identity."""
    existing_call_keys: set[tuple[str | None, str | None, Hashable | None]] = set()
    for item in existing_items:
        if isinstance(item, ToolCallItem):
            existing_call_keys.add(_tool_call_identity(item.raw_item))
    deduped: list[RunItem] = []
    for item in new_items:
        if isinstance(item, ToolCallItem | HandoffCallItem | MCPApprovalRequestItem):
            if id(item.raw_item) in skipped_raw_item_ids:
                continue
            identity = _tool_call_identity(item.raw_item)
            if identity in existing_call_keys:
                continue
            existing_call_keys.add(identity)
        deduped.append(item)
    return deduped


def _register_tool_call_items(
    context_wrapper: RunContextWrapper[Any],
    items: Sequence[RunItem],
    *,
    validate_invocations: bool = True,
) -> None:
    """Validate approval-bound calls and record their committed outputs."""
    call_item_types = (ToolCallItem, HandoffCallItem, MCPApprovalRequestItem, ToolApprovalItem)
    for item in items:
        if isinstance(item, ToolApprovalItem):
            context_wrapper._restore_pending_approval_binding(item)
    for item in items:
        if not isinstance(item, call_item_types):
            continue
        if isinstance(item, ToolApprovalItem):
            continue
        if not validate_invocations and isinstance(item, ToolCallItem | MCPApprovalRequestItem):
            raw_type = get_mapping_or_attr(item.raw_item, "type")
            tool_name = get_mapping_or_attr(item.raw_item, "name")
            if not isinstance(tool_name, str):
                tool_name = {
                    "apply_patch_call": "apply_patch",
                    "computer_call": "computer",
                    "local_shell_call": "local_shell",
                    "shell_call": "shell",
                }.get(raw_type)
            if isinstance(tool_name, str):
                context_wrapper._restore_pending_approval_binding(
                    ToolApprovalItem(
                        agent=item.agent,
                        raw_item=cast(Any, item.raw_item),
                        tool_name=tool_name,
                        tool_namespace=get_tool_call_namespace(item.raw_item),
                        tool_lookup_key=(
                            get_function_tool_lookup_key_for_call(item.raw_item)
                            if raw_type == "function_call"
                            else None
                        ),
                    )
                )
        if not validate_invocations:
            continue
        if (
            isinstance(item, ToolCallItem)
            and get_mapping_or_attr(item.raw_item, "type") == "function_call"
        ):
            # Resolved function calls are validated from the plan, where canonical routing identity
            # is available. Raw calls can omit deferred-loading routing metadata.
            continue
        context_wrapper._tool_invocation_status(
            item.raw_item,
            tool_name=(item.tool_name if isinstance(item, ToolCallItem) else None),
            invocation_role=("handoff" if isinstance(item, HandoffCallItem) else None),
        )
    for item in items:
        if isinstance(item, call_item_types):
            continue
        if isinstance(item, ToolCallOutputItem | HandoffOutputItem | MCPApprovalResponseItem):
            context_wrapper._mark_tool_call_completed(item.raw_item)


def _validate_unresolved_function_calls(
    context_wrapper: RunContextWrapper[Any],
    runs: Sequence[Any],
) -> None:
    """Validate unresolved function calls before any sibling tool starts."""
    for run in runs:
        context_wrapper._tool_invocation_status(get_mapping_or_attr(run, "tool_call"))


def _dedupe_processed_response_invocations(
    processed_response: ProcessedResponse,
    *,
    context_wrapper: RunContextWrapper[Any],
    existing_items: Sequence[RunItem],
    deferred_binding_validation_raw_item_ids: set[int] | None = None,
    filter_completed: bool = True,
) -> set[int]:
    """Validate and coalesce one response's tool invocations before user callbacks run."""
    deferred_binding_validation_raw_item_ids = deferred_binding_validation_raw_item_ids or set()
    completed_output_keys = {
        output_identity
        for item in existing_items
        if (output_identity := tool_output_identity(getattr(item, "raw_item", None))) is not None
    }
    completed_historical_invocations: dict[str, tuple[str, str, str]] = {}
    for item in existing_items:
        raw_item = getattr(item, "raw_item", None)
        identity = tool_invocation_identity(
            raw_item,
            tool_lookup_key=(item.tool_lookup_key if isinstance(item, ToolApprovalItem) else None),
            tool_name=(
                item.tool_name if isinstance(item, ToolCallItem | ToolApprovalItem) else None
            ),
            invocation_role=("handoff" if isinstance(item, HandoffCallItem) else None),
        )
        if (
            context_wrapper._allow_legacy_approval_binding_reconstruction
            and isinstance(item, ToolCallItem)
            and item.tool_name is None
        ):
            call_identity = tool_invocation_call_id(raw_item)
            if call_identity is not None and call_identity[1] is not None:
                legacy_record = context_wrapper._tool_invocations.get(call_identity[1])
                if (
                    legacy_record is not None
                    and legacy_record.completed
                    and legacy_record.invocation_type == call_identity[0]
                ):
                    identity = (
                        legacy_record.invocation_type,
                        call_identity[1],
                        legacy_record.fingerprint,
                    )
        if identity is None or identity[:2] not in completed_output_keys:
            continue
        previous_identity = completed_historical_invocations.get(identity[1])
        if previous_identity is not None and previous_identity != identity:
            raise ModelBehaviorError(
                "Run history reused a tool call ID for different completed invocations. "
                "Use a unique call ID for each tool invocation."
            )
        completed_historical_invocations[identity[1]] = identity
    current_response_invocations: dict[str, tuple[str, str, str]] = {}
    (
        processed_response.mcp_approval_requests,
        skipped_raw_item_ids,
    ) = _preflight_mcp_approval_requests(processed_response.mcp_approval_requests)
    uncanonical_response_call_ids: set[str] = set()

    def should_keep(
        raw_item: Any,
        tool_lookup_key: FunctionToolLookupKey | None = None,
        tool_name: str | None = None,
        invocation_role: str | None = None,
    ) -> bool:
        call_identity = tool_invocation_call_id(raw_item)
        if call_identity is not None and call_identity[1] is None:
            raise ModelBehaviorError(
                "Tool invocations require a non-empty string call ID before execution."
            )
        identity = tool_invocation_identity(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            invocation_role=invocation_role,
        )
        if identity is None:
            context_wrapper._tool_invocation_status(
                raw_item,
                tool_lookup_key=tool_lookup_key,
                tool_name=tool_name,
                invocation_role=invocation_role,
            )
            if call_identity is not None and call_identity[1] is not None:
                call_id = call_identity[1]
                if (
                    call_id in current_response_invocations
                    or call_id in uncanonical_response_call_ids
                ):
                    raise ModelBehaviorError(
                        "Model reused a tool call ID for a different invocation in one response. "
                        "Use a unique call ID for each tool invocation."
                    )
                uncanonical_response_call_ids.add(call_id)
            return True

        if identity[1] in uncanonical_response_call_ids:
            raise ModelBehaviorError(
                "Model reused a tool call ID for a different invocation in one response. "
                "Use a unique call ID for each tool invocation."
            )

        historical_identity = completed_historical_invocations.get(identity[1])
        if historical_identity is not None:
            if historical_identity != identity:
                raise ModelBehaviorError(
                    "Model reused a completed tool call ID for a different invocation. "
                    "Use a unique call ID for each tool invocation."
                )
            if filter_completed:
                skipped_raw_item_ids.add(id(raw_item))
                return False
        previous_identity = current_response_invocations.get(identity[1])
        if previous_identity is not None:
            if previous_identity != identity:
                raise ModelBehaviorError(
                    "Model reused a tool call ID for a different invocation in one response. "
                    "Use a unique call ID for each tool invocation."
                )
            skipped_raw_item_ids.add(id(raw_item))
            return False
        current_response_invocations[identity[1]] = identity

        if id(raw_item) not in deferred_binding_validation_raw_item_ids:
            try:
                binding_status = context_wrapper._tool_invocation_status(
                    raw_item,
                    tool_lookup_key=tool_lookup_key,
                    tool_name=tool_name,
                    invocation_role=invocation_role,
                )
            except ModelBehaviorError:
                # A completed exact sibling with the same provider ID can predate approval
                # binding, so preserve that released cross-kind resume behavior. Changed content
                # has no exact historical identity and still fails closed.
                if historical_identity == identity:
                    return True
                raise
            if filter_completed and binding_status is not None and binding_status[1]:
                skipped_raw_item_ids.add(id(raw_item))
                return False
            if binding_status is not None and binding_status[2] and not binding_status[1]:
                raise ModelBehaviorError(
                    "A tool call already executed, but its output was not committed. "
                    "Start a new run instead of retrying the invocation."
                )
        return True

    processed_response.functions = [
        run
        for run in processed_response.functions
        if should_keep(
            run.tool_call,
            get_function_tool_lookup_key_for_tool(run.function_tool),
        )
    ]
    processed_response.handoffs = [
        run
        for run in processed_response.handoffs
        if should_keep(run.tool_call, invocation_role="handoff")
    ]
    processed_response.function_tools_not_found = [
        run for run in processed_response.function_tools_not_found if should_keep(run.tool_call)
    ]
    processed_response.computer_actions = [
        run
        for run in processed_response.computer_actions
        if should_keep(run.tool_call, tool_name=run.computer_tool.name)
    ]
    processed_response.custom_tool_calls = [
        run
        for run in processed_response.custom_tool_calls
        if should_keep(run.tool_call, tool_name=run.custom_tool.name)
    ]
    processed_response.local_shell_calls = [
        run
        for run in processed_response.local_shell_calls
        if should_keep(run.tool_call, tool_name=run.local_shell_tool.name)
    ]
    processed_response.shell_calls = [
        run
        for run in processed_response.shell_calls
        if should_keep(run.tool_call, tool_name=run.shell_tool.name)
    ]
    processed_response.apply_patch_calls = [
        run
        for run in processed_response.apply_patch_calls
        if should_keep(run.tool_call, tool_name=run.apply_patch_tool.name)
    ]
    processed_response.mcp_approval_requests = [
        run for run in processed_response.mcp_approval_requests if should_keep(run.request_item)
    ]
    dropped_item_indexes = {
        index
        for index, item in enumerate(processed_response.new_items)
        if isinstance(item, ToolCallItem | HandoffCallItem | MCPApprovalRequestItem)
        and id(item.raw_item) in skipped_raw_item_ids
    }
    dropped_reasoning_indexes: set[int] = set()
    for index in range(len(processed_response.new_items) - 1, -1, -1):
        if not isinstance(processed_response.new_items[index], ReasoningItem):
            continue
        for next_index in range(index + 1, len(processed_response.new_items)):
            if isinstance(processed_response.new_items[next_index], ReasoningItem):
                continue
            if next_index in dropped_item_indexes:
                dropped_reasoning_indexes.add(index)
            break
    excluded_item_indexes = dropped_item_indexes | dropped_reasoning_indexes
    processed_response.new_items = [
        item
        for index, item in enumerate(processed_response.new_items)
        if index not in excluded_item_indexes
    ]
    return skipped_raw_item_ids


@_dc.dataclass
class ToolExecutionPlan:
    """Represents tool execution work to perform in a single turn."""

    function_runs: list[ToolRunFunction] = _dc.field(default_factory=list)
    computer_actions: list[ToolRunComputerAction] = _dc.field(default_factory=list)
    custom_tool_calls: list[ToolRunCustom] = _dc.field(default_factory=list)
    shell_calls: list[ToolRunShellCall] = _dc.field(default_factory=list)
    apply_patch_calls: list[ToolRunApplyPatchCall] = _dc.field(default_factory=list)
    local_shell_calls: list[ToolRunLocalShellCall] = _dc.field(default_factory=list)
    pending_interruptions: list[ToolApprovalItem] = _dc.field(default_factory=list)
    approved_mcp_responses: list[RunItem] = _dc.field(default_factory=list)
    mcp_requests_with_callback: list[ToolRunMCPApprovalRequest] = _dc.field(default_factory=list)

    @property
    def has_interruptions(self) -> bool:
        return bool(self.pending_interruptions)


def _partition_mcp_approval_requests(
    requests: Sequence[ToolRunMCPApprovalRequest],
) -> tuple[list[ToolRunMCPApprovalRequest], list[ToolRunMCPApprovalRequest]]:
    """Split MCP approval requests into callback-handled and manual buckets."""
    with_callback: list[ToolRunMCPApprovalRequest] = []
    manual: list[ToolRunMCPApprovalRequest] = []
    for request in requests:
        if (
            request.mcp_tool.on_approval_request is not None
            and tool_invocation_identity(request.request_item) is not None
        ):
            with_callback.append(request)
        else:
            manual.append(request)
    return with_callback, manual


def _collect_mcp_approval_plan(
    *,
    processed_response,
    agent: Agent[Any],
    context_wrapper: RunContextWrapper[Any],
    approval_items_by_call_id: Mapping[str, ToolApprovalItem],
    pending_interruption_adder: Callable[[ToolApprovalItem], None],
) -> tuple[list[ToolRunMCPApprovalRequest], list[RunItem]]:
    """Return MCP approval callback requests and approved responses."""
    approved_mcp_responses: list[RunItem] = []
    (
        mcp_requests_with_callback,
        mcp_requests_requiring_manual_approval,
    ) = _partition_mcp_approval_requests(processed_response.mcp_approval_requests)
    if mcp_requests_requiring_manual_approval:
        approved_mcp_responses, _ = _apply_manual_mcp_approvals(
            agent=agent,
            requests=mcp_requests_requiring_manual_approval,
            context_wrapper=context_wrapper,
            approval_items_by_call_id=approval_items_by_call_id,
            pending_interruption_adder=pending_interruption_adder,
        )

    return list(mcp_requests_with_callback), approved_mcp_responses


def _build_plan_for_fresh_turn(
    *,
    processed_response,
    agent: Agent[Any],
    context_wrapper: RunContextWrapper[Any],
    approval_items_by_call_id: Mapping[str, ToolApprovalItem],
) -> ToolExecutionPlan:
    """Build a ToolExecutionPlan for a fresh turn."""
    pending_interruptions: list[ToolApprovalItem] = []
    mcp_requests_with_callback, approved_mcp_responses = _collect_mcp_approval_plan(
        processed_response=processed_response,
        agent=agent,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
        pending_interruption_adder=pending_interruptions.append,
    )

    return ToolExecutionPlan(
        function_runs=processed_response.functions,
        computer_actions=processed_response.computer_actions,
        custom_tool_calls=processed_response.custom_tool_calls,
        shell_calls=processed_response.shell_calls,
        apply_patch_calls=processed_response.apply_patch_calls,
        local_shell_calls=processed_response.local_shell_calls,
        pending_interruptions=pending_interruptions,
        approved_mcp_responses=approved_mcp_responses,
        mcp_requests_with_callback=list(mcp_requests_with_callback),
    )


def _build_plan_for_resume_turn(
    *,
    processed_response,
    agent: Agent[Any],
    context_wrapper: RunContextWrapper[Any],
    approval_items_by_call_id: Mapping[str, ToolApprovalItem],
    pending_interruptions: list[ToolApprovalItem],
    pending_interruption_adder: Callable[[ToolApprovalItem], None],
    function_runs: list[ToolRunFunction],
    computer_actions: list[ToolRunComputerAction],
    shell_calls: list[ToolRunShellCall],
    custom_tool_calls: list[ToolRunCustom],
    apply_patch_calls: list[ToolRunApplyPatchCall],
) -> ToolExecutionPlan:
    """Build a ToolExecutionPlan for a resumed turn."""
    mcp_requests_with_callback, approved_mcp_responses = _collect_mcp_approval_plan(
        processed_response=processed_response,
        agent=agent,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
        pending_interruption_adder=pending_interruption_adder,
    )

    return ToolExecutionPlan(
        function_runs=function_runs,
        computer_actions=computer_actions,
        custom_tool_calls=custom_tool_calls,
        shell_calls=shell_calls,
        apply_patch_calls=apply_patch_calls,
        local_shell_calls=[],
        pending_interruptions=pending_interruptions,
        approved_mcp_responses=approved_mcp_responses,
        mcp_requests_with_callback=list(mcp_requests_with_callback),
    )


def _collect_tool_interruptions(
    *,
    function_results: Sequence[Any],
    custom_tool_results: Sequence[RunItem],
    shell_results: Sequence[RunItem],
    apply_patch_results: Sequence[RunItem],
) -> list[ToolApprovalItem]:
    """Collect tool approval interruptions from tool results."""
    interruptions: list[ToolApprovalItem] = []
    for result in function_results:
        if isinstance(result.run_item, ToolApprovalItem):
            interruptions.append(result.run_item)
        if getattr(result, "interruptions", None):
            interruptions.extend(result.interruptions)
        elif getattr(result, "agent_run_result", None) and hasattr(
            result.agent_run_result, "interruptions"
        ):
            nested_interruptions = result.agent_run_result.interruptions
            if nested_interruptions:
                interruptions.extend(nested_interruptions)
    for custom_tool_result in custom_tool_results:
        if isinstance(custom_tool_result, ToolApprovalItem):
            interruptions.append(custom_tool_result)
    for shell_result in shell_results:
        if isinstance(shell_result, ToolApprovalItem):
            interruptions.append(shell_result)
    for apply_patch_result in apply_patch_results:
        if isinstance(apply_patch_result, ToolApprovalItem):
            interruptions.append(apply_patch_result)
    return interruptions


def _build_tool_result_items(
    *,
    function_results: Sequence[Any],
    computer_results: Sequence[RunItem],
    custom_tool_results: Sequence[RunItem],
    shell_results: Sequence[RunItem],
    apply_patch_results: Sequence[RunItem],
    local_shell_results: Sequence[RunItem] | None = None,
) -> list[RunItem]:
    """Build ordered tool result items for inclusion in new step items."""
    results: list[RunItem] = []
    for result in function_results:
        run_item = getattr(result, "run_item", None)
        if isinstance(run_item, RunItemBase):
            results.append(cast(RunItem, run_item))
    results.extend(computer_results)
    results.extend(custom_tool_results)
    results.extend(shell_results)
    results.extend(apply_patch_results)
    if local_shell_results:
        results.extend(local_shell_results)
    return results


def _make_unique_item_appender(
    existing_items: Sequence[RunItem],
) -> tuple[list[RunItem], Callable[[RunItem], None]]:
    """Return (items, append_fn) that skips duplicates by object identity."""
    existing_ids = {id(item) for item in existing_items}
    new_items: list[RunItem] = []
    new_item_ids: set[int] = set()

    def append_if_new(item: RunItem) -> None:
        item_id = id(item)
        if item_id in existing_ids or item_id in new_item_ids:
            return
        new_items.append(item)
        new_item_ids.add(item_id)

    return new_items, append_if_new


async def _collect_runs_by_approval(
    runs: Sequence[T],
    *,
    call_id_extractor: Callable[[T], str],
    tool_name_resolver: Callable[[T], str],
    rejection_builder: Callable[[T, str], Awaitable[RunItem] | RunItem],
    context_wrapper: RunContextWrapper[Any],
    approval_items_by_call_id: Mapping[str, ToolApprovalItem],
    agent: Agent[Any],
    pending_interruption_adder: Callable[[ToolApprovalItem], None],
    needs_approval_checker: Callable[[T], Awaitable[bool]] | None = None,
    output_exists_checker: Callable[[str], bool] | None = None,
) -> tuple[list[T], list[RunItem]]:
    """Return approved runs and rejection items, adding pending approvals via callback."""
    approved_runs: list[T] = []
    rejection_items: list[RunItem] = []
    for run in runs:
        call_id = call_id_extractor(run)
        if output_exists_checker is not None and output_exists_checker(call_id):
            continue
        tool_name = tool_name_resolver(run)
        existing_pending = approval_items_by_call_id.get(call_id)
        function_tool = get_mapping_or_attr(run, "function_tool")
        current_item = ToolApprovalItem(
            agent=agent,
            raw_item=get_mapping_or_attr(run, "tool_call"),
            tool_name=tool_name,
            tool_namespace=get_tool_call_namespace(get_mapping_or_attr(run, "tool_call")),
            tool_origin=(
                get_function_tool_origin(function_tool)
                if isinstance(function_tool, FunctionTool)
                else None
            ),
            tool_lookup_key=(
                get_function_tool_lookup_key_for_tool(function_tool)
                if isinstance(function_tool, FunctionTool)
                else None
            ),
        )
        approval_status = context_wrapper.get_approval_status(
            tool_name,
            call_id,
            existing_pending=existing_pending,
            current_invocation=current_item,
        )

        needs_approval = True
        if approval_status is None and needs_approval_checker is not None:
            try:
                needs_approval = await needs_approval_checker(run)
            except UserError:
                raise
            except Exception:
                needs_approval = True
            approval_status = context_wrapper.get_approval_status(
                tool_name,
                call_id,
                existing_pending=existing_pending,
                current_invocation=current_item,
            )

        if approval_status is False:
            rejection = rejection_builder(run, call_id)
            if inspect.isawaitable(rejection):
                rejection_item = await cast(Awaitable[RunItem], rejection)
            else:
                rejection_item = rejection
            rejection_items.append(rejection_item)
            continue

        if approval_status is True:
            approved_runs.append(run)
            continue

        if not needs_approval:
            approved_runs.append(run)
            continue

        pending_item = existing_pending if existing_pending is not None else current_item
        pending_interruption_adder(pending_item)

    return approved_runs, rejection_items


def _apply_manual_mcp_approvals(
    *,
    agent: Agent[Any],
    requests: Sequence[ToolRunMCPApprovalRequest],
    context_wrapper: RunContextWrapper[Any],
    approval_items_by_call_id: Mapping[str, ToolApprovalItem],
    pending_interruption_adder: Callable[[ToolApprovalItem], None],
) -> tuple[list[RunItem], list[ToolApprovalItem]]:
    """Collect manual MCP approvals and record pending interruptions via callback."""
    approved_responses, pending_items = collect_manual_mcp_approvals(
        agent=agent,
        requests=requests,
        context_wrapper=context_wrapper,
        existing_pending_by_call_id=approval_items_by_call_id,
    )
    approved_items: list[RunItem] = list(approved_responses)
    for approval_item in pending_items:
        pending_interruption_adder(approval_item)
    return approved_items, pending_items


async def _append_mcp_callback_results(
    *,
    agent: Agent[Any],
    requests: Sequence[ToolRunMCPApprovalRequest],
    context_wrapper: RunContextWrapper[Any],
    append_item: Callable[[RunItem], None],
) -> None:
    """Execute MCP approval callbacks and append results when present."""
    if not requests:
        return
    approval_results = await execute_mcp_approval_requests(
        agent=agent,
        approval_requests=list(requests),
        context_wrapper=context_wrapper,
    )
    for result in approval_results:
        append_item(result)


async def _select_function_tool_runs_for_resume(
    runs: Sequence[ToolRunFunction],
    *,
    approval_items_by_call_id: Mapping[str, ToolApprovalItem],
    context_wrapper: RunContextWrapper[Any],
    needs_approval_checker: Callable[[ToolRunFunction], Awaitable[bool]],
    output_exists_checker: Callable[[ToolRunFunction], bool],
    record_rejection: Callable[
        [str | None, ResponseFunctionToolCall, FunctionTool], Awaitable[None]
    ],
    pending_interruption_adder: Callable[[ToolApprovalItem], None],
    pending_item_builder: Callable[[ToolRunFunction], ToolApprovalItem],
) -> list[ToolRunFunction]:
    """Filter function tool runs during resume, honoring approvals and outputs."""
    selected: list[ToolRunFunction] = []
    for run in runs:
        call_id = run.tool_call.call_id
        if output_exists_checker(run):
            continue

        current_item = pending_item_builder(run)
        existing_pending = approval_items_by_call_id.get(call_id)
        approval_status = context_wrapper.get_approval_status(
            run.function_tool.name,
            call_id,
            tool_namespace=get_tool_call_namespace(run.tool_call),
            existing_pending=existing_pending,
            tool_lookup_key=current_item.tool_lookup_key,
            current_invocation=current_item,
        )

        requires_approval = True
        if approval_status is None:
            requires_approval = await needs_approval_checker(run)
            approval_status = context_wrapper.get_approval_status(
                run.function_tool.name,
                call_id,
                tool_namespace=get_tool_call_namespace(run.tool_call),
                existing_pending=existing_pending,
                tool_lookup_key=current_item.tool_lookup_key,
                current_invocation=current_item,
            )

        if approval_status is False:
            await record_rejection(call_id, run.tool_call, run.function_tool)
            continue

        if approval_status is True:
            selected.append(run)
            continue

        if not requires_approval:
            selected.append(run)
            continue

        pending_item = existing_pending if existing_pending is not None else current_item
        pending_interruption_adder(pending_item)

    return selected


async def _execute_tool_plan(
    *,
    plan: ToolExecutionPlan,
    bindings: AgentBindings[Any],
    hooks,
    context_wrapper: RunContextWrapper[Any],
    run_config,
    parallel: bool = True,
    tool_output_committer: Callable[[RunItem], None] | None = None,
) -> tuple[
    list[Any],
    list[ToolInputGuardrailResult],
    list[ToolOutputGuardrailResult],
    list[RunItem],
    list[RunItem],
    list[RunItem],
    list[RunItem],
    list[RunItem],
]:
    """Execute tool runs captured in a ToolExecutionPlan."""
    public_agent = bindings.public_agent
    isolate_function_tool_failures = len(plan.function_runs) > 1 or (
        parallel
        and (
            bool(plan.computer_actions)
            or bool(plan.custom_tool_calls)
            or bool(plan.shell_calls)
            or bool(plan.apply_patch_calls)
            or bool(plan.local_shell_calls)
        )
    )
    if parallel:
        sibling_category_failure = asyncio.Event()
        (
            (function_results, tool_input_guardrail_results, tool_output_guardrail_results),
            computer_results,
            custom_tool_results,
            shell_results,
            apply_patch_results,
            local_shell_results,
        ) = await gather_with_cancel(
            execute_function_tool_calls(
                bindings=bindings,
                tool_runs=plan.function_runs,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
                isolate_parallel_failures=isolate_function_tool_failures,
                sibling_category_failure=sibling_category_failure,
                tool_output_committer=tool_output_committer,
            ),
            execute_computer_actions(
                public_agent=public_agent,
                actions=plan.computer_actions,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
                tool_output_committer=tool_output_committer,
            ),
            execute_custom_tool_calls(
                public_agent=public_agent,
                calls=plan.custom_tool_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
                tool_output_committer=tool_output_committer,
            ),
            execute_shell_calls(
                public_agent=public_agent,
                calls=plan.shell_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
                tool_output_committer=tool_output_committer,
            ),
            execute_apply_patch_calls(
                public_agent=public_agent,
                calls=plan.apply_patch_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
                tool_output_committer=tool_output_committer,
            ),
            execute_local_shell_calls(
                public_agent=public_agent,
                calls=plan.local_shell_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
                tool_output_committer=tool_output_committer,
            ),
            on_child_failure=sibling_category_failure.set,
        )
    else:
        (
            function_results,
            tool_input_guardrail_results,
            tool_output_guardrail_results,
        ) = await execute_function_tool_calls(
            bindings=bindings,
            tool_runs=plan.function_runs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
            isolate_parallel_failures=isolate_function_tool_failures,
            tool_output_committer=tool_output_committer,
        )
        computer_results = await execute_computer_actions(
            public_agent=public_agent,
            actions=plan.computer_actions,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
            tool_output_committer=tool_output_committer,
        )
        custom_tool_results = await execute_custom_tool_calls(
            public_agent=public_agent,
            calls=plan.custom_tool_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
            tool_output_committer=tool_output_committer,
        )
        shell_results = await execute_shell_calls(
            public_agent=public_agent,
            calls=plan.shell_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
            tool_output_committer=tool_output_committer,
        )
        apply_patch_results = await execute_apply_patch_calls(
            public_agent=public_agent,
            calls=plan.apply_patch_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
            tool_output_committer=tool_output_committer,
        )
        local_shell_results = await execute_local_shell_calls(
            public_agent=public_agent,
            calls=plan.local_shell_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
            tool_output_committer=tool_output_committer,
        )

    return (
        function_results,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        computer_results,
        custom_tool_results,
        shell_results,
        apply_patch_results,
        local_shell_results,
    )

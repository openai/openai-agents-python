from __future__ import annotations

import asyncio
import dataclasses as _dc
import inspect
import json
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from typing import Any, TypeVar, cast

from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_input_param import McpApprovalResponse

from ..agent import Agent
from ..exceptions import UserError
from ..items import (
    MCPApprovalResponseItem,
    RunItem,
    RunItemBase,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from ..run_context import RunContextWrapper
from ..tool import FunctionTool, MCPToolApprovalRequest
from ..tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from .run_steps import (
    ToolRunApplyPatchCall,
    ToolRunComputerAction,
    ToolRunFunction,
    ToolRunLocalShellCall,
    ToolRunMCPApprovalRequest,
    ToolRunShellCall,
)
from .tool_execution import (
    collect_manual_mcp_approvals,
    execute_apply_patch_calls,
    execute_computer_actions,
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
    if isinstance(value, (dict, list, tuple)):
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
    if isinstance(raw, dict):
        call_id = raw.get("call_id") or raw.get("id") or call_id
        name = raw.get("name", name)
        args = raw.get("arguments", args)
    return call_id, name, _hashable_identity_value(args)


async def execute_mcp_approval_requests(
    *,
    agent: Agent[Any],
    approval_requests: list[ToolRunMCPApprovalRequest],
    context_wrapper: RunContextWrapper[Any],
) -> list[RunItem]:
    """Run hosted MCP approval callbacks and return approval response items."""

    async def run_single_approval(approval_request: ToolRunMCPApprovalRequest) -> RunItem:
        callback = approval_request.mcp_tool.on_approval_request
        assert callback is not None, "Callback is required for MCP approval requests"
        maybe_awaitable_result = callback(
            MCPToolApprovalRequest(context_wrapper, approval_request.request_item)
        )
        if inspect.isawaitable(maybe_awaitable_result):
            result = await maybe_awaitable_result
        else:
            result = maybe_awaitable_result
        reason = result.get("reason", None)
        request_item = approval_request.request_item
        request_id = (
            request_item.id
            if hasattr(request_item, "id")
            else cast(dict[str, Any], request_item).get("id", "")
        )
        raw_item: McpApprovalResponse = {
            "approval_request_id": request_id,
            "approve": result["approve"],
            "type": "mcp_approval_response",
        }
        if not result["approve"] and reason:
            raw_item["reason"] = reason
        return MCPApprovalResponseItem(
            raw_item=raw_item,
            agent=agent,
        )

    tasks = [run_single_approval(approval_request) for approval_request in approval_requests]
    return await asyncio.gather(*tasks)


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
    *, existing_items: Sequence[RunItem], new_items: Sequence[RunItem]
) -> list[RunItem]:
    """Return new items while skipping tool call duplicates already seen by identity."""
    existing_call_keys: set[tuple[str | None, str | None, Hashable | None]] = set()
    for item in existing_items:
        if isinstance(item, ToolCallItem):
            existing_call_keys.add(_tool_call_identity(item.raw_item))
    deduped: list[RunItem] = []
    for item in new_items:
        if isinstance(item, ToolCallItem):
            identity = _tool_call_identity(item.raw_item)
            if identity in existing_call_keys:
                continue
            existing_call_keys.add(identity)
        deduped.append(item)
    return deduped


@_dc.dataclass
class ToolExecutionPlan:
    """Represents tool execution work to perform in a single turn."""

    function_runs: list[ToolRunFunction] = _dc.field(default_factory=list)
    computer_actions: list[ToolRunComputerAction] = _dc.field(default_factory=list)
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
        if request.mcp_tool.on_approval_request:
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
        tool_name = tool_name_resolver(run)
        existing_pending = approval_items_by_call_id.get(call_id)
        approval_status = context_wrapper.get_approval_status(
            tool_name,
            call_id,
            existing_pending=existing_pending,
        )

        if output_exists_checker and output_exists_checker(call_id):
            continue

        if approval_status is False:
            rejection = rejection_builder(run, call_id)
            if inspect.isawaitable(rejection):
                rejection_item = await cast(Awaitable[RunItem], rejection)
            else:
                rejection_item = rejection
            rejection_items.append(rejection_item)
            continue

        needs_approval = True
        if needs_approval_checker:
            try:
                needs_approval = await needs_approval_checker(run)
            except UserError:
                raise
            except Exception:
                needs_approval = True

        if not needs_approval:
            approved_runs.append(run)
            continue

        if approval_status is True:
            approved_runs.append(run)
        else:
            pending_item = existing_pending or ToolApprovalItem(
                agent=agent,
                raw_item=get_mapping_or_attr(run, "tool_call"),
                tool_name=tool_name,
            )
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

        approval_status = context_wrapper.get_approval_status(
            run.function_tool.name,
            call_id,
            existing_pending=approval_items_by_call_id.get(call_id),
        )

        requires_approval = await needs_approval_checker(run)

        if approval_status is False:
            await record_rejection(call_id, run.tool_call, run.function_tool)
            continue

        if approval_status is True:
            selected.append(run)
            continue

        if not requires_approval:
            selected.append(run)
            continue

        if approval_status is None:
            pending_interruption_adder(
                approval_items_by_call_id.get(run.tool_call.call_id) or pending_item_builder(run)
            )
            continue
        selected.append(run)

    return selected


async def _execute_tool_plan(
    *,
    plan: ToolExecutionPlan,
    agent: Agent[Any],
    hooks,
    context_wrapper: RunContextWrapper[Any],
    run_config,
    parallel: bool = True,
) -> tuple[
    list[Any],
    list[ToolInputGuardrailResult],
    list[ToolOutputGuardrailResult],
    list[RunItem],
    list[RunItem],
    list[RunItem],
    list[RunItem],
]:
    """Execute tool runs captured in a ToolExecutionPlan."""
    if parallel:
        (
            (function_results, tool_input_guardrail_results, tool_output_guardrail_results),
            computer_results,
            shell_results,
            apply_patch_results,
            local_shell_results,
        ) = await asyncio.gather(
            execute_function_tool_calls(
                agent=agent,
                tool_runs=plan.function_runs,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            execute_computer_actions(
                agent=agent,
                actions=plan.computer_actions,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            execute_shell_calls(
                agent=agent,
                calls=plan.shell_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            execute_apply_patch_calls(
                agent=agent,
                calls=plan.apply_patch_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            execute_local_shell_calls(
                agent=agent,
                calls=plan.local_shell_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
        )
    else:
        (
            function_results,
            tool_input_guardrail_results,
            tool_output_guardrail_results,
        ) = await execute_function_tool_calls(
            agent=agent,
            tool_runs=plan.function_runs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )
        computer_results = await execute_computer_actions(
            agent=agent,
            actions=plan.computer_actions,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )
        shell_results = await execute_shell_calls(
            agent=agent,
            calls=plan.shell_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )
        apply_patch_results = await execute_apply_patch_calls(
            agent=agent,
            calls=plan.apply_patch_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )
        local_shell_results = await execute_local_shell_calls(
            agent=agent,
            calls=plan.local_shell_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )

    return (
        function_results,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        computer_results,
        shell_results,
        apply_patch_results,
        local_shell_results,
    )

"""
Run-loop orchestration helpers used by the Agent runner. This module coordinates tool execution,
approvals, and turn processing; all symbols here are internal and not part of the public SDK.
"""

from __future__ import annotations

import asyncio
import dataclasses as _dc
import inspect
import json
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from typing import Any, Literal, TypeVar, cast

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseComputerToolCall,
    ResponseCustomToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
)
from openai.types.responses.response_code_interpreter_tool_call import (
    ResponseCodeInterpreterToolCall,
)
from openai.types.responses.response_input_param import McpApprovalResponse
from openai.types.responses.response_output_item import (
    ImageGenerationCall,
    LocalShellCall,
    McpApprovalRequest,
    McpCall,
    McpListTools,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from ..agent import Agent, ToolsToFinalOutputResult
from ..agent_output import AgentOutputSchema, AgentOutputSchemaBase
from ..exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    ModelBehaviorError,
    OutputGuardrailTripwireTriggered,
    RunErrorDetails,
    UserError,
)
from ..guardrail import InputGuardrail, InputGuardrailResult, OutputGuardrail, OutputGuardrailResult
from ..handoffs import Handoff, HandoffInputData, handoff, nest_handoff_history
from ..items import (
    HandoffCallItem,
    HandoffOutputItem,
    ItemHelpers,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MCPListToolsItem,
    MessageOutputItem,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallItemTypes,
    ToolCallOutputItem,
    TResponseInputItem,
)
from ..lifecycle import AgentHooksBase, RunHooks, RunHooksBase
from ..logger import logger
from ..memory import Session
from ..models.interface import Model
from ..result import RunResultStreaming
from ..run_config import CallModelData, ModelInputData, RunConfig
from ..run_context import AgentHookContext, RunContextWrapper, TContext
from ..run_state import RunState
from ..stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
    StreamEvent,
)
from ..tool import (
    ApplyPatchTool,
    ComputerTool,
    FunctionTool,
    FunctionToolResult,
    HostedMCPTool,
    LocalShellTool,
    MCPToolApprovalRequest,
    ShellTool,
    Tool,
    dispose_resolved_computers,
)
from ..tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from ..tracing import Span, SpanError, agent_span, guardrail_span, handoff_span
from ..tracing.model_tracing import get_model_tracing_impl
from ..tracing.span_data import AgentSpanData
from ..usage import Usage
from ..util import _coro, _error_tracing
from .approvals import (
    append_input_items_excluding_approvals,
    apply_rewind_offset,
    collect_approvals_and_rewind,
    filter_tool_approvals,
)
from .items import (
    REJECTION_MESSAGE,
    apply_patch_rejection_item,
    copy_input_items,
    deduplicate_input_items,
    drop_orphan_function_calls,
    ensure_input_item_format,
    function_rejection_item,
    normalize_input_items_for_api,
    shell_rejection_item,
)
from .oai_conversation import OpenAIServerConversationTracker
from .run_steps import (
    NOT_FINAL_OUTPUT,
    NextStepFinalOutput,
    NextStepHandoff,
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
    QueueCompleteSentinel,
    SingleStepResult,
    ToolRunApplyPatchCall,
    ToolRunComputerAction,
    ToolRunFunction,
    ToolRunHandoff,
    ToolRunLocalShellCall,
    ToolRunMCPApprovalRequest,
    ToolRunShellCall,
)
from .session_persistence import (
    prepare_input_with_session,
    rewind_session_items,
    save_result_to_session,
)
from .tool_actions import ApplyPatchAction, ComputerAction, LocalShellAction, ShellAction
from .tool_execution import (
    build_litellm_json_tool_call,
    coerce_apply_patch_operation,
    coerce_shell_call,
    collect_manual_mcp_approvals,
    evaluate_needs_approval_setting,
    execute_apply_patch_calls,
    execute_computer_actions,
    execute_function_tool_calls,
    execute_local_shell_calls,
    execute_shell_calls,
    extract_apply_patch_call_id,
    extract_shell_call_id,
    extract_tool_call_id,
    function_needs_approval,
    get_mapping_or_attr,
    index_approval_items_by_call_id,
    initialize_computer_tools,
    is_apply_patch_name,
    maybe_reset_tool_choice,
    normalize_shell_output,
    parse_apply_patch_custom_input,
    parse_apply_patch_function_args,
    process_hosted_mcp_approvals,
    serialize_shell_output,
    should_keep_hosted_mcp_item,
)
from .tool_use_tracker import (
    TOOL_CALL_TYPES,
    AgentToolUseTracker,
    hydrate_tool_use_tracker,
    serialize_tool_use_tracker,
)

__all__ = [
    "extract_tool_call_id",
    "coerce_shell_call",
    "normalize_shell_output",
    "serialize_shell_output",
    "ComputerAction",
    "LocalShellAction",
    "ShellAction",
    "ApplyPatchAction",
    "REJECTION_MESSAGE",
    "AgentToolUseTracker",
    "ToolRunHandoff",
    "ToolRunFunction",
    "ToolRunComputerAction",
    "ToolRunMCPApprovalRequest",
    "ToolRunLocalShellCall",
    "ToolRunShellCall",
    "ToolRunApplyPatchCall",
    "ProcessedResponse",
    "NextStepHandoff",
    "NextStepFinalOutput",
    "NextStepRunAgain",
    "NextStepInterruption",
    "SingleStepResult",
    "QueueCompleteSentinel",
    "execute_tools_and_side_effects",
    "resolve_interrupted_turn",
    "execute_function_tool_calls",
    "execute_local_shell_calls",
    "execute_shell_calls",
    "execute_apply_patch_calls",
    "execute_computer_actions",
    "execute_handoffs",
    "execute_mcp_approval_requests",
    "execute_final_output",
    "run_final_output_hooks",
    "run_single_input_guardrail",
    "run_single_output_guardrail",
    "maybe_reset_tool_choice",
    "initialize_computer_tools",
    "process_model_response",
    "stream_step_items_to_queue",
    "stream_step_result_to_queue",
    "check_for_final_output_from_tools",
    "get_model_tracing_impl",
    "validate_run_hooks",
    "maybe_filter_model_input",
    "run_input_guardrails_with_queue",
    "start_streaming",
    "run_single_turn_streamed",
    "run_single_turn",
    "get_single_step_result_from_response",
    "run_input_guardrails",
    "run_output_guardrails",
    "get_new_response",
    "get_output_schema",
    "get_handoffs",
    "get_all_tools",
    "get_model",
    "input_guardrail_tripwire_triggered_for_stream",
]


T = TypeVar("T")


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


async def execute_final_output_step(
    *,
    agent: Agent[Any],
    original_input: str | list[TResponseInputItem],
    new_response: ModelResponse,
    pre_step_items: list[RunItem],
    new_step_items: list[RunItem],
    final_output: Any,
    hooks: RunHooks[Any],
    context_wrapper: RunContextWrapper[Any],
    tool_input_guardrail_results: list[ToolInputGuardrailResult],
    tool_output_guardrail_results: list[ToolOutputGuardrailResult],
) -> SingleStepResult:
    """Finalize a turn once final output is known and run end hooks."""
    await run_final_output_hooks(
        agent=agent,
        hooks=hooks,
        context_wrapper=context_wrapper,
        final_output=final_output,
    )

    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        next_step=NextStepFinalOutput(final_output),
        tool_input_guardrail_results=tool_input_guardrail_results,
        tool_output_guardrail_results=tool_output_guardrail_results,
        output_guardrail_results=[],
    )


async def execute_final_output(
    *,
    agent: Agent[Any],
    original_input: str | list[TResponseInputItem],
    new_response: ModelResponse,
    pre_step_items: list[RunItem],
    new_step_items: list[RunItem],
    final_output: Any,
    hooks: RunHooks[Any],
    context_wrapper: RunContextWrapper[Any],
    tool_input_guardrail_results: list[ToolInputGuardrailResult],
    tool_output_guardrail_results: list[ToolOutputGuardrailResult],
) -> SingleStepResult:
    """Convenience wrapper to finalize a turn and run end hooks."""
    return await execute_final_output_step(
        agent=agent,
        original_input=original_input,
        new_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        final_output=final_output,
        hooks=hooks,
        context_wrapper=context_wrapper,
        tool_input_guardrail_results=tool_input_guardrail_results,
        tool_output_guardrail_results=tool_output_guardrail_results,
    )


async def execute_tools_and_side_effects(
    *,
    agent: Agent[TContext],
    # The original input to the Runner
    original_input: str | list[TResponseInputItem],
    # Everything generated by Runner since the original input, but before the current step
    pre_step_items: list[RunItem],
    new_response: ModelResponse,
    processed_response: ProcessedResponse,
    output_schema: AgentOutputSchemaBase | None,
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
) -> SingleStepResult:
    """Run one turn of the loop, coordinating tools, approvals, guardrails, and handoffs."""
    # Make a copy of the generated items
    pre_step_items = list(pre_step_items)

    def _hashable_identity_value(value: Any) -> Hashable | None:
        if value is None:
            return None
        if isinstance(value, (dict, list, tuple)):
            try:
                return json.dumps(value, sort_keys=True, default=str)
            except Exception:
                return repr(value)
        try:
            hash(value)
        except Exception:
            return str(value)
        return cast(Hashable, value)

    def _tool_call_identity(raw: Any) -> tuple[str | None, str | None, Hashable | None]:
        """Return a tuple that uniquely identifies a tool call for deduplication."""
        call_id = extract_tool_call_id(raw)
        name = None
        args = None
        if isinstance(raw, Mapping):
            name = raw.get("name")
            args = raw.get("arguments")
        else:
            name = getattr(raw, "name", None)
            args = getattr(raw, "arguments", None)
        return call_id, name, _hashable_identity_value(args)

    existing_call_keys: set[tuple[str | None, str | None, Hashable | None]] = set()
    for item in pre_step_items:
        if isinstance(item, ToolCallItem):
            identity = _tool_call_identity(item.raw_item)
            existing_call_keys.add(identity)
    approval_items_by_call_id = index_approval_items_by_call_id(pre_step_items)

    new_step_items: list[RunItem] = []
    (
        mcp_requests_with_callback,
        mcp_requests_requiring_manual_approval,
    ) = _partition_mcp_approval_requests(processed_response.mcp_approval_requests)
    for item in processed_response.new_items:
        if isinstance(item, ToolCallItem):
            identity = _tool_call_identity(item.raw_item)
            if identity in existing_call_keys:
                continue
            existing_call_keys.add(identity)
        new_step_items.append(item)

    # First, run function tools, computer actions, shell calls, apply_patch calls,
    # and legacy local shell calls.
    (
        (function_results, tool_input_guardrail_results, tool_output_guardrail_results),
        computer_results,
        shell_results,
        apply_patch_results,
        local_shell_results,
    ) = await asyncio.gather(
        execute_function_tool_calls(
            agent=agent,
            tool_runs=processed_response.functions,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        ),
        execute_computer_actions(
            agent=agent,
            actions=processed_response.computer_actions,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        ),
        execute_shell_calls(
            agent=agent,
            calls=processed_response.shell_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        ),
        execute_apply_patch_calls(
            agent=agent,
            calls=processed_response.apply_patch_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        ),
        execute_local_shell_calls(
            agent=agent,
            calls=processed_response.local_shell_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        ),
    )
    for result in function_results:
        new_step_items.append(result.run_item)

    new_step_items.extend(computer_results)
    for shell_result in shell_results:
        new_step_items.append(shell_result)
    for apply_patch_result in apply_patch_results:
        new_step_items.append(apply_patch_result)
    new_step_items.extend(local_shell_results)

    # Collect approval interruptions so they can be serialized and resumed.
    interruptions: list[ToolApprovalItem] = []
    for result in function_results:
        if isinstance(result.run_item, ToolApprovalItem):
            interruptions.append(result.run_item)
        else:
            if result.interruptions:
                interruptions.extend(result.interruptions)
            elif result.agent_run_result and hasattr(result.agent_run_result, "interruptions"):
                nested_interruptions = result.agent_run_result.interruptions
                if nested_interruptions:
                    interruptions.extend(nested_interruptions)
    for shell_result in shell_results:
        if isinstance(shell_result, ToolApprovalItem):
            interruptions.append(shell_result)
    for apply_patch_result in apply_patch_results:
        if isinstance(apply_patch_result, ToolApprovalItem):
            interruptions.append(apply_patch_result)
    if mcp_requests_requiring_manual_approval:
        approved_mcp_responses, pending_mcp_approvals = collect_manual_mcp_approvals(
            agent=agent,
            requests=mcp_requests_requiring_manual_approval,
            context_wrapper=context_wrapper,
            existing_pending_by_call_id=approval_items_by_call_id,
        )
        interruptions.extend(pending_mcp_approvals)
        new_step_items.extend(approved_mcp_responses)
        new_step_items.extend(pending_mcp_approvals)

    processed_response.interruptions = interruptions

    if interruptions:
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            next_step=NextStepInterruption(interruptions=interruptions),
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
            processed_response=processed_response,
        )
    # Next, run the MCP approval requests
    if mcp_requests_with_callback:
        approval_results = await execute_mcp_approval_requests(
            agent=agent,
            approval_requests=mcp_requests_with_callback,
            context_wrapper=context_wrapper,
        )
        new_step_items.extend(approval_results)

    # Next, check if there are any handoffs
    if run_handoffs := processed_response.handoffs:
        return await execute_handoffs(
            agent=agent,
            original_input=original_input,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            new_response=new_response,
            run_handoffs=run_handoffs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
        )

    # Next, we'll check if the tool use should result in a final output
    check_tool_use = await check_for_final_output_from_tools(
        agent=agent,
        tool_results=function_results,
        context_wrapper=context_wrapper,
        config=run_config,
    )

    if check_tool_use.is_final_output:
        # If the output type is str, then let's just stringify it
        if not agent.output_type or agent.output_type is str:
            check_tool_use.final_output = str(check_tool_use.final_output)

        if check_tool_use.final_output is None:
            logger.error(
                "Model returned a final output of None. Not raising an error because we assume"
                "you know what you're doing."
            )

        return await execute_final_output(
            agent=agent,
            original_input=original_input,
            new_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            final_output=check_tool_use.final_output,
            hooks=hooks,
            context_wrapper=context_wrapper,
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )

    # Now we can check if the model also produced a final output
    message_items = [item for item in new_step_items if isinstance(item, MessageOutputItem)]

    # We'll use the last content output as the final output
    potential_final_output_text = (
        ItemHelpers.extract_last_text(message_items[-1].raw_item) if message_items else None
    )

    # Generate final output only when there are no pending tool calls or approval requests.
    if not processed_response.has_tools_or_approvals_to_run():
        if output_schema and not output_schema.is_plain_text() and potential_final_output_text:
            final_output = output_schema.validate_json(potential_final_output_text)
            return await execute_final_output(
                agent=agent,
                original_input=original_input,
                new_response=new_response,
                pre_step_items=pre_step_items,
                new_step_items=new_step_items,
                final_output=final_output,
                hooks=hooks,
                context_wrapper=context_wrapper,
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
            )
        elif not output_schema or output_schema.is_plain_text():
            return await execute_final_output(
                agent=agent,
                original_input=original_input,
                new_response=new_response,
                pre_step_items=pre_step_items,
                new_step_items=new_step_items,
                final_output=potential_final_output_text or "",
                hooks=hooks,
                context_wrapper=context_wrapper,
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
            )

    # If there's no final output, we can just run again
    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        next_step=NextStepRunAgain(),
        tool_input_guardrail_results=tool_input_guardrail_results,
        tool_output_guardrail_results=tool_output_guardrail_results,
    )


async def resolve_interrupted_turn(
    *,
    agent: Agent[TContext],
    original_input: str | list[TResponseInputItem],
    original_pre_step_items: list[RunItem],
    new_response: ModelResponse,
    processed_response: ProcessedResponse,
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    run_state: RunState | None = None,
) -> SingleStepResult:
    """Continues a turn that was previously interrupted waiting for tool approval.

    Executes the now approved tools and returns the resulting step transition.
    """

    def _pending_approvals_from_state() -> list[ToolApprovalItem]:
        """Return pending approval items from state or previous step history."""
        if (
            run_state is not None
            and hasattr(run_state, "_current_step")
            and isinstance(run_state._current_step, NextStepInterruption)
        ):
            return [
                item
                for item in run_state._current_step.interruptions
                if isinstance(item, ToolApprovalItem)
            ]
        return [item for item in original_pre_step_items if isinstance(item, ToolApprovalItem)]

    def _record_function_rejection(
        call_id: str | None, tool_call: ResponseFunctionToolCall
    ) -> None:
        rejected_function_outputs.append(function_rejection_item(agent, tool_call))
        if isinstance(call_id, str):
            rejected_function_call_ids.add(call_id)

    async def _function_requires_approval(run: ToolRunFunction) -> bool:
        call_id = run.tool_call.call_id
        if call_id and call_id in approval_items_by_call_id:
            return True

        try:
            return await function_needs_approval(
                run.function_tool,
                context_wrapper,
                run.tool_call,
            )
        except UserError:
            raise
        except Exception:
            return True

    try:
        context_wrapper.turn_input = ItemHelpers.input_to_new_input_list(original_input)
    except Exception:
        context_wrapper.turn_input = []

    # Pending approval items come from persisted state; the run loop handles rewinds
    # and we use them to rebuild missing function tool runs if needed.
    pending_approval_items = _pending_approvals_from_state()

    approval_items_by_call_id = index_approval_items_by_call_id(pending_approval_items)

    rejected_function_outputs: list[RunItem] = []
    rejected_function_call_ids: set[str] = set()
    pending_interruptions: list[ToolApprovalItem] = []
    pending_interruption_keys: set[str] = set()

    (
        mcp_requests_with_callback,
        mcp_requests_requiring_manual_approval,
    ) = _partition_mcp_approval_requests(processed_response.mcp_approval_requests)

    def _has_output_item(call_id: str, expected_type: str) -> bool:
        for item in original_pre_step_items:
            if not isinstance(item, ToolCallOutputItem):
                continue
            raw_item = item.raw_item
            raw_type = None
            raw_call_id = None
            if isinstance(raw_item, Mapping):
                raw_type = raw_item.get("type")
                raw_call_id = raw_item.get("call_id")
            else:
                raw_type = getattr(raw_item, "type", None)
                raw_call_id = getattr(raw_item, "call_id", None)
            if raw_type == expected_type and raw_call_id == call_id:
                return True
        return False

    async def _collect_runs_by_approval(
        runs: Sequence[T],
        *,
        call_id_extractor: Callable[[T], str],
        tool_name_resolver: Callable[[T], str],
        rejection_builder: Callable[[str], RunItem],
        needs_approval_checker: Callable[[T], Awaitable[bool]] | None = None,
        output_exists_checker: Callable[[str], bool] | None = None,
    ) -> tuple[list[T], list[RunItem]]:
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
                rejection_items.append(rejection_builder(call_id))
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
                _add_pending_interruption(pending_item)
        return approved_runs, rejection_items

    def _shell_call_id_from_run(run: ToolRunShellCall) -> str:
        return extract_shell_call_id(run.tool_call)

    def _apply_patch_call_id_from_run(run: ToolRunApplyPatchCall) -> str:
        return extract_apply_patch_call_id(run.tool_call)

    def _computer_call_id_from_run(run: ToolRunComputerAction) -> str:
        call_id = extract_tool_call_id(run.tool_call)
        if not call_id:
            raise ModelBehaviorError("Computer action is missing call_id.")
        return call_id

    def _shell_tool_name(run: ToolRunShellCall) -> str:
        return run.shell_tool.name

    def _apply_patch_tool_name(run: ToolRunApplyPatchCall) -> str:
        return run.apply_patch_tool.name

    def _build_shell_rejection(call_id: str) -> RunItem:
        return shell_rejection_item(agent, call_id)

    def _build_apply_patch_rejection(call_id: str) -> RunItem:
        return apply_patch_rejection_item(agent, call_id)

    async def _shell_needs_approval(run: ToolRunShellCall) -> bool:
        shell_call = coerce_shell_call(run.tool_call)
        return await evaluate_needs_approval_setting(
            run.shell_tool.needs_approval,
            context_wrapper,
            shell_call.action,
            shell_call.call_id,
        )

    async def _apply_patch_needs_approval(run: ToolRunApplyPatchCall) -> bool:
        operation = coerce_apply_patch_operation(
            run.tool_call,
            context_wrapper=context_wrapper,
        )
        call_id = extract_apply_patch_call_id(run.tool_call)
        return await evaluate_needs_approval_setting(
            run.apply_patch_tool.needs_approval, context_wrapper, operation, call_id
        )

    def _shell_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "shell_call_output")

    def _apply_patch_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "apply_patch_call_output")

    def _computer_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "computer_call_output")

    def _function_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "function_call_output")

    def _add_pending_interruption(item: ToolApprovalItem | None) -> None:
        if item is None:
            return
        call_id = extract_tool_call_id(item.raw_item)
        key = call_id or f"raw:{id(item.raw_item)}"
        if key in pending_interruption_keys:
            return
        pending_interruption_keys.add(key)
        pending_interruptions.append(item)

    approved_mcp_responses: list[RunItem] = []

    approved_manual_mcp, pending_manual_mcp = collect_manual_mcp_approvals(
        agent=agent,
        requests=mcp_requests_requiring_manual_approval,
        context_wrapper=context_wrapper,
        existing_pending_by_call_id=approval_items_by_call_id,
    )
    approved_mcp_responses.extend(approved_manual_mcp)
    for approval_item in pending_manual_mcp:
        _add_pending_interruption(approval_item)

    async def _rebuild_function_runs_from_approvals() -> list[ToolRunFunction]:
        """Recreate function runs from pending approvals when runs are missing."""
        if not pending_approval_items:
            return []
        all_tools = await agent.get_all_tools(context_wrapper)
        tool_map: dict[str, FunctionTool] = {
            tool.name: tool for tool in all_tools if isinstance(tool, FunctionTool)
        }
        existing_pending_call_ids: set[str] = set()
        for existing_pending in pending_interruptions:
            if isinstance(existing_pending, ToolApprovalItem):
                existing_call_id = extract_tool_call_id(existing_pending.raw_item)
                if existing_call_id:
                    existing_pending_call_ids.add(existing_call_id)
        rebuilt_runs: list[ToolRunFunction] = []
        for approval in pending_approval_items:
            if not isinstance(approval, ToolApprovalItem):
                continue
            raw = approval.raw_item
            raw_type = get_mapping_or_attr(raw, "type")
            if raw_type != "function_call":
                continue
            name = get_mapping_or_attr(raw, "name")
            if not (isinstance(name, str) and name in tool_map):
                continue

            rebuilt_call_id: str | None
            arguments: str | None
            tool_call: ResponseFunctionToolCall
            if isinstance(raw, ResponseFunctionToolCall):
                rebuilt_call_id = raw.call_id
                arguments = raw.arguments
                tool_call = raw
            else:
                rebuilt_call_id = extract_tool_call_id(raw)
                arguments = get_mapping_or_attr(raw, "arguments") or "{}"
                status = get_mapping_or_attr(raw, "status")
                if not (isinstance(rebuilt_call_id, str) and isinstance(arguments, str)):
                    continue
                # Validate status is a valid Literal type
                valid_status: Literal["in_progress", "completed", "incomplete"] | None = None
                if isinstance(status, str) and status in (
                    "in_progress",
                    "completed",
                    "incomplete",
                ):
                    valid_status = status  # type: ignore[assignment]
                tool_call = ResponseFunctionToolCall(
                    type="function_call",
                    name=name,
                    call_id=rebuilt_call_id,
                    arguments=arguments,
                    status=valid_status,
                )

            if not (isinstance(rebuilt_call_id, str) and isinstance(arguments, str)):
                continue

            approval_status = context_wrapper.get_approval_status(
                name, rebuilt_call_id, existing_pending=approval
            )
            if approval_status is False:
                _record_function_rejection(rebuilt_call_id, tool_call)
                continue
            if approval_status is None:
                if rebuilt_call_id not in existing_pending_call_ids:
                    _add_pending_interruption(approval)
                    existing_pending_call_ids.add(rebuilt_call_id)
                continue
            rebuilt_runs.append(ToolRunFunction(function_tool=tool_map[name], tool_call=tool_call))
        return rebuilt_runs

    # Run only the approved function calls for this turn; emit rejections for denied ones.
    function_tool_runs: list[ToolRunFunction] = []
    for run in processed_response.functions:
        call_id = run.tool_call.call_id
        if call_id and _function_output_exists(call_id):
            continue
        approval_status = context_wrapper.get_approval_status(
            run.function_tool.name,
            call_id,
            existing_pending=approval_items_by_call_id.get(call_id),
        )

        requires_approval = await _function_requires_approval(run)

        if approval_status is False:
            _record_function_rejection(call_id, run.tool_call)
            continue

        # If the user has already approved this call, run it even if the original tool did
        # not require approval. This avoids skipping execution when we are resuming from a
        # purely HITL-driven interruption.
        if approval_status is True:
            function_tool_runs.append(run)
            continue

        # If approval is not required and no explicit rejection is present, skip running again.
        # The original turn already executed this tool, so resuming after an unrelated approval
        # should not invoke it a second time.
        if not requires_approval:
            continue

        if approval_status is None:
            _add_pending_interruption(
                approval_items_by_call_id.get(run.tool_call.call_id)
                or ToolApprovalItem(agent=agent, raw_item=run.tool_call)
            )
            continue
        function_tool_runs.append(run)

    # If state lacks function runs, rebuild them from pending approvals.
    # This covers resume-from-serialization cases where only ToolApprovalItems were persisted,
    # so we reconstruct minimal tool calls to apply the user's decision.
    if not function_tool_runs:
        function_tool_runs = await _rebuild_function_runs_from_approvals()

    (
        function_results,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
    ) = await execute_function_tool_calls(
        agent=agent,
        tool_runs=function_tool_runs,
        hooks=hooks,
        context_wrapper=context_wrapper,
        config=run_config,
    )

    # Surface nested interruptions from function tool results (e.g., agent-as-tool HITL).
    for result in function_results:
        if result.interruptions:
            for interruption in result.interruptions:
                _add_pending_interruption(interruption)

    pending_computer_actions: list[ToolRunComputerAction] = []
    for action in processed_response.computer_actions:
        call_id = _computer_call_id_from_run(action)
        if _computer_output_exists(call_id):
            continue
        pending_computer_actions.append(action)

    computer_results: list[RunItem] = []
    if pending_computer_actions:
        computer_results = await execute_computer_actions(
            agent=agent,
            actions=pending_computer_actions,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )

    # Execute shell/apply_patch only when approved; emit rejections otherwise.
    approved_shell_calls, rejected_shell_results = await _collect_runs_by_approval(
        processed_response.shell_calls,
        call_id_extractor=_shell_call_id_from_run,
        tool_name_resolver=_shell_tool_name,
        rejection_builder=_build_shell_rejection,
        needs_approval_checker=_shell_needs_approval,
        output_exists_checker=_shell_output_exists,
    )

    approved_apply_patch_calls, rejected_apply_patch_results = await _collect_runs_by_approval(
        processed_response.apply_patch_calls,
        call_id_extractor=_apply_patch_call_id_from_run,
        tool_name_resolver=_apply_patch_tool_name,
        rejection_builder=_build_apply_patch_rejection,
        needs_approval_checker=_apply_patch_needs_approval,
        output_exists_checker=_apply_patch_output_exists,
    )

    shell_results = await execute_shell_calls(
        agent=agent,
        calls=approved_shell_calls,
        hooks=hooks,
        context_wrapper=context_wrapper,
        config=run_config,
    )

    apply_patch_results = await execute_apply_patch_calls(
        agent=agent,
        calls=approved_apply_patch_calls,
        hooks=hooks,
        context_wrapper=context_wrapper,
        config=run_config,
    )

    # Resuming reuses the same RunItem objects; skip duplicates by identity.
    original_pre_step_item_ids = {id(item) for item in original_pre_step_items}
    new_items: list[RunItem] = []
    new_items_ids: set[int] = set()

    def append_if_new(item: RunItem) -> None:
        item_id = id(item)
        if item_id in original_pre_step_item_ids or item_id in new_items_ids:
            return
        new_items.append(item)
        new_items_ids.add(item_id)

    for function_result in function_results:
        append_if_new(function_result.run_item)
    for computer_result in computer_results:
        append_if_new(computer_result)
    for rejection_item in rejected_function_outputs:
        append_if_new(rejection_item)
    for pending_item in pending_interruptions:
        if pending_item:
            append_if_new(pending_item)
    for shell_result in shell_results:
        append_if_new(shell_result)
    for shell_rejection in rejected_shell_results:
        append_if_new(shell_rejection)
    for apply_patch_result in apply_patch_results:
        append_if_new(apply_patch_result)
    for apply_patch_rejection in rejected_apply_patch_results:
        append_if_new(apply_patch_rejection)
    for approved_response in approved_mcp_responses:
        append_if_new(approved_response)

    processed_response.interruptions = pending_interruptions
    if pending_interruptions:
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=original_pre_step_items,
            new_step_items=new_items,
            next_step=NextStepInterruption(
                interruptions=[item for item in pending_interruptions if item]
            ),
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
            processed_response=processed_response,
        )

    if mcp_requests_with_callback:
        approval_results = await execute_mcp_approval_requests(
            agent=agent,
            approval_requests=mcp_requests_with_callback,
            context_wrapper=context_wrapper,
        )
        for approval_result in approval_results:
            append_if_new(approval_result)

    (
        pending_hosted_mcp_approvals,
        pending_hosted_mcp_approval_ids,
    ) = process_hosted_mcp_approvals(
        original_pre_step_items=original_pre_step_items,
        mcp_approval_requests=processed_response.mcp_approval_requests,
        context_wrapper=context_wrapper,
        agent=agent,
        append_item=append_if_new,
    )

    # Keep only unresolved hosted MCP approvals so server-managed conversations
    # can surface them on the next turn; drop resolved placeholders.
    pre_step_items = [
        item
        for item in original_pre_step_items
        if should_keep_hosted_mcp_item(
            item,
            pending_hosted_mcp_approvals=pending_hosted_mcp_approvals,
            pending_hosted_mcp_approval_ids=pending_hosted_mcp_approval_ids,
        )
    ]

    if rejected_function_call_ids:
        pre_step_items = [
            item
            for item in pre_step_items
            if not (
                item.type == "tool_call_output_item"
                and (
                    extract_tool_call_id(getattr(item, "raw_item", None))
                    in rejected_function_call_ids
                )
            )
        ]

    # Avoid re-running handoffs that already executed before the interruption.
    executed_handoff_call_ids: set[str] = set()
    for item in original_pre_step_items:
        if isinstance(item, HandoffCallItem):
            handoff_call_id = extract_tool_call_id(item.raw_item)
            if handoff_call_id:
                executed_handoff_call_ids.add(handoff_call_id)

    pending_handoffs = [
        handoff
        for handoff in processed_response.handoffs
        if not handoff.tool_call.call_id
        or handoff.tool_call.call_id not in executed_handoff_call_ids
    ]

    # If there are pending handoffs that haven't been executed yet, execute them now.
    if pending_handoffs:
        return await execute_handoffs(
            agent=agent,
            original_input=original_input,
            pre_step_items=pre_step_items,
            new_step_items=new_items,
            new_response=new_response,
            run_handoffs=pending_handoffs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
        )

    # Check if tool use should result in a final output
    check_tool_use = await check_for_final_output_from_tools(
        agent=agent,
        tool_results=function_results,
        context_wrapper=context_wrapper,
        config=run_config,
    )

    if check_tool_use.is_final_output:
        if not agent.output_type or agent.output_type is str:
            check_tool_use.final_output = str(check_tool_use.final_output)

        if check_tool_use.final_output is None:
            logger.error(
                "Model returned a final output of None. Not raising an error because we assume"
                "you know what you're doing."
            )

        return await execute_final_output(
            agent=agent,
            original_input=original_input,
            new_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_items,
            final_output=check_tool_use.final_output,
            hooks=hooks,
            context_wrapper=context_wrapper,
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )

    # We only ran new tools and side effects. We need to run the rest of the agent
    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_items,
        next_step=NextStepRunAgain(),
        tool_input_guardrail_results=tool_input_guardrail_results,
        tool_output_guardrail_results=tool_output_guardrail_results,
    )


def process_model_response(
    *,
    agent: Agent[Any],
    all_tools: list[Tool],
    response: ModelResponse,
    output_schema: AgentOutputSchemaBase | None,
    handoffs: list[Handoff],
) -> ProcessedResponse:
    items: list[RunItem] = []

    run_handoffs = []
    functions = []
    computer_actions = []
    local_shell_calls = []
    shell_calls = []
    apply_patch_calls = []
    mcp_approval_requests = []
    tools_used: list[str] = []
    handoff_map = {handoff.tool_name: handoff for handoff in handoffs}
    function_map = {tool.name: tool for tool in all_tools if isinstance(tool, FunctionTool)}
    computer_tool = next((tool for tool in all_tools if isinstance(tool, ComputerTool)), None)
    local_shell_tool = next((tool for tool in all_tools if isinstance(tool, LocalShellTool)), None)
    shell_tool = next((tool for tool in all_tools if isinstance(tool, ShellTool)), None)
    apply_patch_tool = next((tool for tool in all_tools if isinstance(tool, ApplyPatchTool)), None)
    hosted_mcp_server_map = {
        tool.tool_config["server_label"]: tool
        for tool in all_tools
        if isinstance(tool, HostedMCPTool)
    }

    for output in response.output:
        output_type = get_mapping_or_attr(output, "type")
        logger.debug(
            "Processing output item type=%s class=%s",
            output_type,
            output.__class__.__name__ if hasattr(output, "__class__") else type(output),
        )
        if output_type == "shell_call":
            items.append(ToolCallItem(raw_item=cast(Any, output), agent=agent))
            if not shell_tool:
                tools_used.append("shell")
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Shell tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError("Model produced shell call without a shell tool.")
            tools_used.append(shell_tool.name)
            call_identifier = get_mapping_or_attr(output, "call_id")
            logger.debug("Queuing shell_call %s", call_identifier)
            shell_calls.append(ToolRunShellCall(tool_call=output, shell_tool=shell_tool))
            continue
        if output_type == "apply_patch_call":
            items.append(ToolCallItem(raw_item=cast(Any, output), agent=agent))
            if apply_patch_tool:
                tools_used.append(apply_patch_tool.name)
                call_identifier = get_mapping_or_attr(output, "call_id")
                logger.debug("Queuing apply_patch_call %s", call_identifier)
                apply_patch_calls.append(
                    ToolRunApplyPatchCall(
                        tool_call=output,
                        apply_patch_tool=apply_patch_tool,
                    )
                )
            else:
                tools_used.append("apply_patch")
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Apply patch tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError(
                    "Model produced apply_patch call without an apply_patch tool."
                )
            continue
        if isinstance(output, ResponseOutputMessage):
            items.append(MessageOutputItem(raw_item=output, agent=agent))
        elif isinstance(output, ResponseFileSearchToolCall):
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("file_search")
        elif isinstance(output, ResponseFunctionWebSearch):
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("web_search")
        elif isinstance(output, ResponseReasoningItem):
            items.append(ReasoningItem(raw_item=output, agent=agent))
        elif isinstance(output, ResponseComputerToolCall):
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("computer_use")
            if not computer_tool:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Computer tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError("Model produced computer action without a computer tool.")
            computer_actions.append(
                ToolRunComputerAction(tool_call=output, computer_tool=computer_tool)
            )
        elif isinstance(output, McpApprovalRequest):
            items.append(MCPApprovalRequestItem(raw_item=output, agent=agent))
            if output.server_label not in hosted_mcp_server_map:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="MCP server label not found",
                        data={"server_label": output.server_label},
                    )
                )
                raise ModelBehaviorError(f"MCP server label {output.server_label} not found")
            server = hosted_mcp_server_map[output.server_label]
            mcp_approval_requests.append(
                ToolRunMCPApprovalRequest(
                    request_item=output,
                    mcp_tool=server,
                )
            )
            if not server.on_approval_request:
                logger.debug(
                    "Hosted MCP server %s has no on_approval_request hook; approvals will be "
                    "surfaced as interruptions for the caller to handle.",
                    output.server_label,
                )
        elif isinstance(output, McpListTools):
            items.append(MCPListToolsItem(raw_item=output, agent=agent))
        elif isinstance(output, McpCall):
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("mcp")
        elif isinstance(output, ImageGenerationCall):
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("image_generation")
        elif isinstance(output, ResponseCodeInterpreterToolCall):
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("code_interpreter")
        elif isinstance(output, LocalShellCall):
            items.append(ToolCallItem(raw_item=output, agent=agent))
            if local_shell_tool:
                tools_used.append("local_shell")
                local_shell_calls.append(
                    ToolRunLocalShellCall(tool_call=output, local_shell_tool=local_shell_tool)
                )
            elif shell_tool:
                tools_used.append(shell_tool.name)
                shell_calls.append(ToolRunShellCall(tool_call=output, shell_tool=shell_tool))
            else:
                tools_used.append("local_shell")
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Local shell tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError(
                    "Model produced local shell call without a local shell tool."
                )
        elif isinstance(output, ResponseCustomToolCall) and is_apply_patch_name(
            output.name, apply_patch_tool
        ):
            parsed_operation = parse_apply_patch_custom_input(output.input)
            pseudo_call = {
                "type": "apply_patch_call",
                "call_id": output.call_id,
                "operation": parsed_operation,
            }
            items.append(ToolCallItem(raw_item=cast(Any, pseudo_call), agent=agent))
            if apply_patch_tool:
                tools_used.append(apply_patch_tool.name)
                apply_patch_calls.append(
                    ToolRunApplyPatchCall(
                        tool_call=pseudo_call,
                        apply_patch_tool=apply_patch_tool,
                    )
                )
            else:
                tools_used.append("apply_patch")
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Apply patch tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError(
                    "Model produced apply_patch call without an apply_patch tool."
                )
        elif (
            isinstance(output, ResponseFunctionToolCall)
            and is_apply_patch_name(output.name, apply_patch_tool)
            and output.name not in function_map
        ):
            parsed_operation = parse_apply_patch_function_args(output.arguments)
            pseudo_call = {
                "type": "apply_patch_call",
                "call_id": output.call_id,
                "operation": parsed_operation,
            }
            items.append(ToolCallItem(raw_item=cast(Any, pseudo_call), agent=agent))
            if apply_patch_tool:
                tools_used.append(apply_patch_tool.name)
                apply_patch_calls.append(
                    ToolRunApplyPatchCall(tool_call=pseudo_call, apply_patch_tool=apply_patch_tool)
                )
            else:
                tools_used.append("apply_patch")
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Apply patch tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError(
                    "Model produced apply_patch call without an apply_patch tool."
                )
            continue

        elif not isinstance(output, ResponseFunctionToolCall):
            logger.warning(f"Unexpected output type, ignoring: {type(output)}")
            continue

        # At this point we know it's a function tool call
        if not isinstance(output, ResponseFunctionToolCall):
            continue

        tools_used.append(output.name)

        # Handoffs
        if output.name in handoff_map:
            items.append(HandoffCallItem(raw_item=output, agent=agent))
            handoff = ToolRunHandoff(
                tool_call=output,
                handoff=handoff_map[output.name],
            )
            run_handoffs.append(handoff)
        # Regular function tool call
        else:
            if output.name not in function_map:
                if output_schema is not None and output.name == "json_tool_call":
                    # LiteLLM could generate non-existent tool calls for structured outputs
                    items.append(ToolCallItem(raw_item=output, agent=agent))
                    functions.append(
                        ToolRunFunction(
                            tool_call=output,
                            # this tool does not exist in function_map, so generate ad-hoc one,
                            # which just parses the input if it's a string, and returns the
                            # value otherwise
                            function_tool=build_litellm_json_tool_call(output),
                        )
                    )
                    continue
                else:
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Tool not found",
                            data={"tool_name": output.name},
                        )
                    )
                    error = f"Tool {output.name} not found in agent {agent.name}"
                    raise ModelBehaviorError(error)

            items.append(ToolCallItem(raw_item=output, agent=agent))
            functions.append(
                ToolRunFunction(
                    tool_call=output,
                    function_tool=function_map[output.name],
                )
            )

    return ProcessedResponse(
        new_items=items,
        handoffs=run_handoffs,
        functions=functions,
        computer_actions=computer_actions,
        local_shell_calls=local_shell_calls,
        shell_calls=shell_calls,
        apply_patch_calls=apply_patch_calls,
        tools_used=tools_used,
        mcp_approval_requests=mcp_approval_requests,
        interruptions=[],  # Will be populated after tool execution
    )


async def execute_handoffs(
    *,
    agent: Agent[TContext],
    original_input: str | list[TResponseInputItem],
    pre_step_items: list[RunItem],
    new_step_items: list[RunItem],
    new_response: ModelResponse,
    run_handoffs: list[ToolRunHandoff],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
) -> SingleStepResult:
    # If there is more than one handoff, add tool responses that reject those handoffs
    multiple_handoffs = len(run_handoffs) > 1
    if multiple_handoffs:
        output_message = "Multiple handoffs detected, ignoring this one."
        new_step_items.extend(
            [
                ToolCallOutputItem(
                    output=output_message,
                    raw_item=ItemHelpers.tool_call_output_item(handoff.tool_call, output_message),
                    agent=agent,
                )
                for handoff in run_handoffs[1:]
            ]
        )

    actual_handoff = run_handoffs[0]
    with handoff_span(from_agent=agent.name) as span_handoff:
        handoff = actual_handoff.handoff
        new_agent: Agent[Any] = await handoff.on_invoke_handoff(
            context_wrapper, actual_handoff.tool_call.arguments
        )
        span_handoff.span_data.to_agent = new_agent.name
        if multiple_handoffs:
            requested_agents = [handoff.handoff.agent_name for handoff in run_handoffs]
            span_handoff.set_error(
                SpanError(
                    message="Multiple handoffs requested",
                    data={
                        "requested_agents": requested_agents,
                    },
                )
            )

        # Append a tool output item for the handoff
        new_step_items.append(
            HandoffOutputItem(
                agent=agent,
                raw_item=ItemHelpers.tool_call_output_item(
                    actual_handoff.tool_call,
                    handoff.get_transfer_message(new_agent),
                ),
                source_agent=agent,
                target_agent=new_agent,
            )
        )

        # Execute handoff hooks
        await asyncio.gather(
            hooks.on_handoff(
                context=context_wrapper,
                from_agent=agent,
                to_agent=new_agent,
            ),
            (
                agent.hooks.on_handoff(
                    context_wrapper,
                    agent=new_agent,
                    source=agent,
                )
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        # If there's an input filter, filter the input for the next agent
        input_filter = handoff.input_filter or (
            run_config.handoff_input_filter if run_config else None
        )
        handoff_nest_setting = handoff.nest_handoff_history
        should_nest_history = (
            handoff_nest_setting
            if handoff_nest_setting is not None
            else run_config.nest_handoff_history
        )
        handoff_input_data: HandoffInputData | None = None
        if input_filter or should_nest_history:
            handoff_input_data = HandoffInputData(
                input_history=tuple(original_input)
                if isinstance(original_input, list)
                else original_input,
                pre_handoff_items=tuple(pre_step_items),
                new_items=tuple(new_step_items),
                run_context=context_wrapper,
            )

        if input_filter and handoff_input_data is not None:
            filter_name = getattr(input_filter, "__qualname__", repr(input_filter))
            from_agent = getattr(agent, "name", agent.__class__.__name__)
            to_agent = getattr(new_agent, "name", new_agent.__class__.__name__)
            logger.debug(
                "Filtering handoff inputs with %s for %s -> %s",
                filter_name,
                from_agent,
                to_agent,
            )
            if not callable(input_filter):
                _error_tracing.attach_error_to_span(
                    span_handoff,
                    SpanError(
                        message="Invalid input filter",
                        data={"details": "not callable()"},
                    ),
                )
                raise UserError(f"Invalid input filter: {input_filter}")
            filtered = input_filter(handoff_input_data)
            if inspect.isawaitable(filtered):
                filtered = await filtered
            if not isinstance(filtered, HandoffInputData):
                _error_tracing.attach_error_to_span(
                    span_handoff,
                    SpanError(
                        message="Invalid input filter result",
                        data={"details": "not a HandoffInputData"},
                    ),
                )
                raise UserError(f"Invalid input filter result: {filtered}")

            original_input = (
                filtered.input_history
                if isinstance(filtered.input_history, str)
                else list(filtered.input_history)
            )
            pre_step_items = list(filtered.pre_handoff_items)
            new_step_items = list(filtered.new_items)
        elif should_nest_history and handoff_input_data is not None:
            nested = nest_handoff_history(
                handoff_input_data,
                history_mapper=run_config.handoff_history_mapper,
            )
            original_input = (
                nested.input_history
                if isinstance(nested.input_history, str)
                else list(nested.input_history)
            )
            pre_step_items = list(nested.pre_handoff_items)
            new_step_items = list(nested.new_items)

    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        next_step=NextStepHandoff(new_agent),
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
    )


async def run_final_output_hooks(
    agent: Agent[TContext],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    final_output: Any,
) -> None:
    agent_hook_context = AgentHookContext(
        context=context_wrapper.context,
        usage=context_wrapper.usage,
        _approvals=context_wrapper._approvals,
        turn_input=context_wrapper.turn_input,
    )

    await asyncio.gather(
        hooks.on_agent_end(agent_hook_context, agent, final_output),
        agent.hooks.on_end(agent_hook_context, agent, final_output)
        if agent.hooks
        else _coro.noop_coroutine(),
    )


async def run_single_input_guardrail(
    agent: Agent[Any],
    guardrail: InputGuardrail[TContext],
    input: str | list[TResponseInputItem],
    context: RunContextWrapper[TContext],
) -> InputGuardrailResult:
    with guardrail_span(guardrail.get_name()) as span_guardrail:
        result = await guardrail.run(agent, input, context)
        span_guardrail.span_data.triggered = result.output.tripwire_triggered
        return result


async def run_single_output_guardrail(
    guardrail: OutputGuardrail[TContext],
    agent: Agent[Any],
    agent_output: Any,
    context: RunContextWrapper[TContext],
) -> OutputGuardrailResult:
    with guardrail_span(guardrail.get_name()) as span_guardrail:
        result = await guardrail.run(agent=agent, agent_output=agent_output, context=context)
        span_guardrail.span_data.triggered = result.output.tripwire_triggered
        return result


def stream_step_items_to_queue(
    new_step_items: list[RunItem],
    queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel],
):
    for item in new_step_items:
        if isinstance(item, MessageOutputItem):
            event = RunItemStreamEvent(item=item, name="message_output_created")
        elif isinstance(item, HandoffCallItem):
            event = RunItemStreamEvent(item=item, name="handoff_requested")
        elif isinstance(item, HandoffOutputItem):
            event = RunItemStreamEvent(item=item, name="handoff_occured")
        elif isinstance(item, ToolCallItem):
            event = RunItemStreamEvent(item=item, name="tool_called")
        elif isinstance(item, ToolCallOutputItem):
            event = RunItemStreamEvent(item=item, name="tool_output")
        elif isinstance(item, ReasoningItem):
            event = RunItemStreamEvent(item=item, name="reasoning_item_created")
        elif isinstance(item, MCPApprovalRequestItem):
            event = RunItemStreamEvent(item=item, name="mcp_approval_requested")
        elif isinstance(item, MCPApprovalResponseItem):
            event = RunItemStreamEvent(item=item, name="mcp_approval_response")
        elif isinstance(item, MCPListToolsItem):
            event = RunItemStreamEvent(item=item, name="mcp_list_tools")
        elif isinstance(item, ToolApprovalItem):
            # Tool approval items should not be streamed - they represent interruptions
            event = None

        else:
            logger.warning(f"Unexpected item type: {type(item)}")
            event = None

        if event:
            queue.put_nowait(event)


def stream_step_result_to_queue(
    step_result: SingleStepResult,
    queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel],
):
    stream_step_items_to_queue(step_result.new_step_items, queue)


async def check_for_final_output_from_tools(
    *,
    agent: Agent[TContext],
    tool_results: list[FunctionToolResult],
    context_wrapper: RunContextWrapper[TContext],
    config: RunConfig,
) -> ToolsToFinalOutputResult:
    """Determine if tool results should produce a final output.
    Returns:
        ToolsToFinalOutputResult: Indicates whether final output is ready, and the output value.
    """
    if not tool_results:
        return NOT_FINAL_OUTPUT

    if agent.tool_use_behavior == "run_llm_again":
        return NOT_FINAL_OUTPUT
    elif agent.tool_use_behavior == "stop_on_first_tool":
        return ToolsToFinalOutputResult(is_final_output=True, final_output=tool_results[0].output)
    elif isinstance(agent.tool_use_behavior, dict):
        names = agent.tool_use_behavior.get("stop_at_tool_names", [])
        for tool_result in tool_results:
            if tool_result.tool.name in names:
                return ToolsToFinalOutputResult(
                    is_final_output=True, final_output=tool_result.output
                )
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
    elif callable(agent.tool_use_behavior):
        if inspect.iscoroutinefunction(agent.tool_use_behavior):
            return await cast(
                Awaitable[ToolsToFinalOutputResult],
                agent.tool_use_behavior(context_wrapper, tool_results),
            )
        else:
            return cast(
                ToolsToFinalOutputResult, agent.tool_use_behavior(context_wrapper, tool_results)
            )

    logger.error(f"Invalid tool_use_behavior: {agent.tool_use_behavior}")
    raise UserError(f"Invalid tool_use_behavior: {agent.tool_use_behavior}")


def validate_run_hooks(
    hooks: RunHooksBase[Any, Agent[Any]] | AgentHooksBase[Any, Agent[Any]] | Any | None,
) -> RunHooks[Any]:
    """Normalize hooks input and enforce RunHooks type."""
    if hooks is None:
        return RunHooks[Any]()
    input_hook_type = type(hooks).__name__
    if isinstance(hooks, AgentHooksBase):
        raise TypeError(
            "Run hooks must be instances of RunHooks. "
            f"Received agent-scoped hooks ({input_hook_type}). "
            "Attach AgentHooks to an Agent via Agent(..., hooks=...)."
        )
    if not isinstance(hooks, RunHooksBase):
        raise TypeError(f"Run hooks must be instances of RunHooks. Received {input_hook_type}.")
    return hooks


async def maybe_filter_model_input(
    *,
    agent: Agent[TContext],
    run_config: RunConfig,
    context_wrapper: RunContextWrapper[TContext],
    input_items: list[TResponseInputItem],
    system_instructions: str | None,
) -> ModelInputData:
    """Apply optional call_model_input_filter to modify model input."""
    effective_instructions = system_instructions
    effective_input: list[TResponseInputItem] = input_items

    def _sanitize_for_logging(value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, val in value.items():
                sanitized[key] = _sanitize_for_logging(val)
            return sanitized
        if isinstance(value, list):
            return [_sanitize_for_logging(v) for v in value]
        if isinstance(value, str) and len(value) > 200:
            return value[:200] + "...(truncated)"
        return value

    if run_config.call_model_input_filter is None:
        return ModelInputData(input=effective_input, instructions=effective_instructions)

    try:
        model_input = ModelInputData(
            input=effective_input.copy(),
            instructions=effective_instructions,
        )
        filter_payload: CallModelData[TContext] = CallModelData(
            model_data=model_input,
            agent=agent,
            context=context_wrapper.context,
        )
        maybe_updated = run_config.call_model_input_filter(filter_payload)
        updated = await maybe_updated if inspect.isawaitable(maybe_updated) else maybe_updated
        if not isinstance(updated, ModelInputData):
            raise UserError("call_model_input_filter must return a ModelInputData instance")
        return updated
    except Exception as e:
        _error_tracing.attach_error_to_current_span(
            SpanError(message="Error in call_model_input_filter", data={"error": str(e)})
        )
        raise


async def run_input_guardrails_with_queue(
    agent: Agent[Any],
    guardrails: list[InputGuardrail[TContext]],
    input: str | list[TResponseInputItem],
    context: RunContextWrapper[TContext],
    streamed_result: RunResultStreaming,
    parent_span: Span[Any],
):
    """Run guardrails concurrently and stream results into the queue."""
    queue = streamed_result._input_guardrail_queue

    guardrail_tasks = [
        asyncio.create_task(run_single_input_guardrail(agent, guardrail, input, context))
        for guardrail in guardrails
    ]
    guardrail_results = []
    try:
        for done in asyncio.as_completed(guardrail_tasks):
            result = await done
            if result.output.tripwire_triggered:
                for t in guardrail_tasks:
                    t.cancel()
                await asyncio.gather(*guardrail_tasks, return_exceptions=True)
                _error_tracing.attach_error_to_span(
                    parent_span,
                    SpanError(
                        message="Guardrail tripwire triggered",
                        data={
                            "guardrail": result.guardrail.get_name(),
                            "type": "input_guardrail",
                        },
                    ),
                )
                queue.put_nowait(result)
                guardrail_results.append(result)
                break
            queue.put_nowait(result)
            guardrail_results.append(result)
    except Exception:
        for t in guardrail_tasks:
            t.cancel()
        raise

    streamed_result.input_guardrail_results = (
        streamed_result.input_guardrail_results + guardrail_results
    )


async def start_streaming(
    starting_input: str | list[TResponseInputItem],
    streamed_result: RunResultStreaming,
    starting_agent: Agent[TContext],
    max_turns: int,
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    previous_response_id: str | None,
    auto_previous_response_id: bool,
    conversation_id: str | None,
    session: Session | None,
    run_state: RunState[TContext] | None = None,
    *,
    is_resumed_state: bool = False,
):
    """Run the streaming loop for a run result."""
    if streamed_result.trace:
        streamed_result.trace.start(mark_as_current=True)

    if is_resumed_state and run_state is not None:
        conversation_id = conversation_id or run_state._conversation_id
        previous_response_id = previous_response_id or run_state._previous_response_id
        if auto_previous_response_id is False and run_state._auto_previous_response_id:
            auto_previous_response_id = True

    if conversation_id is not None or previous_response_id is not None or auto_previous_response_id:
        server_conversation_tracker = OpenAIServerConversationTracker(
            conversation_id=conversation_id,
            previous_response_id=previous_response_id,
            auto_previous_response_id=auto_previous_response_id,
        )
    else:
        server_conversation_tracker = None

    def _sync_conversation_tracking_from_tracker() -> None:
        if server_conversation_tracker is None:
            return
        if run_state is not None:
            run_state._conversation_id = server_conversation_tracker.conversation_id
            run_state._previous_response_id = server_conversation_tracker.previous_response_id
            run_state._auto_previous_response_id = (
                server_conversation_tracker.auto_previous_response_id
            )
        streamed_result._conversation_id = server_conversation_tracker.conversation_id
        streamed_result._previous_response_id = server_conversation_tracker.previous_response_id
        streamed_result._auto_previous_response_id = (
            server_conversation_tracker.auto_previous_response_id
        )

    if run_state is None:
        run_state = RunState(
            context=context_wrapper,
            original_input=copy_input_items(starting_input),
            starting_agent=starting_agent,
            max_turns=max_turns,
            conversation_id=conversation_id,
            previous_response_id=previous_response_id,
            auto_previous_response_id=auto_previous_response_id,
        )
        streamed_result._state = run_state
    elif streamed_result._state is None:
        streamed_result._state = run_state

    if run_state is not None:
        run_state._conversation_id = conversation_id
        run_state._previous_response_id = previous_response_id
        run_state._auto_previous_response_id = auto_previous_response_id
    streamed_result._conversation_id = conversation_id
    streamed_result._previous_response_id = previous_response_id
    streamed_result._auto_previous_response_id = auto_previous_response_id

    current_span: Span[AgentSpanData] | None = None
    if run_state is not None and run_state._current_agent is not None:
        current_agent = run_state._current_agent
    else:
        current_agent = starting_agent
    if run_state is not None:
        current_turn = run_state._current_turn
    else:
        current_turn = 0
    should_run_agent_start_hooks = True
    tool_use_tracker = AgentToolUseTracker()
    if run_state is not None:
        hydrate_tool_use_tracker(tool_use_tracker, run_state, starting_agent)

    pending_server_items: list[RunItem] | None = None
    session_input_items_for_persistence: list[TResponseInputItem] | None = None

    if is_resumed_state and server_conversation_tracker is not None and run_state is not None:
        session_items: list[TResponseInputItem] | None = None
        if session is not None:
            try:
                session_items = await session.get_items()
            except Exception:
                session_items = None
        server_conversation_tracker.hydrate_from_state(
            original_input=run_state._original_input,
            generated_items=run_state._generated_items,
            model_responses=run_state._model_responses,
            session_items=session_items,
        )

    streamed_result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=current_agent))

    prepared_input: str | list[TResponseInputItem]
    if is_resumed_state and run_state is not None:
        if isinstance(starting_input, list):
            normalized_input = normalize_input_items_for_api(starting_input)
            filtered = drop_orphan_function_calls(normalized_input)
            prepared_input = filtered
        else:
            prepared_input = starting_input
        streamed_result.input = prepared_input
        streamed_result._original_input_for_persistence = []
        streamed_result._stream_input_persisted = True
    else:
        server_manages_conversation = server_conversation_tracker is not None
        prepared_input, session_items_snapshot = await prepare_input_with_session(
            starting_input,
            session,
            run_config.session_input_callback,
            include_history_in_prepared_input=not server_manages_conversation,
            preserve_dropped_new_items=True,
        )
        streamed_result.input = prepared_input
        streamed_result._original_input = copy_input_items(prepared_input)
        if server_manages_conversation:
            streamed_result._original_input_for_persistence = []
            streamed_result._stream_input_persisted = True
        else:
            session_input_items_for_persistence = session_items_snapshot
            streamed_result._original_input_for_persistence = session_items_snapshot

    try:
        while True:
            if is_resumed_state and run_state is not None and run_state._current_step is not None:
                if isinstance(run_state._current_step, NextStepInterruption):
                    if not run_state._model_responses or not run_state._last_processed_response:
                        raise UserError("No model response found in previous state")

                    last_model_response = run_state._model_responses[-1]

                    turn_result = await resolve_interrupted_turn(
                        agent=current_agent,
                        original_input=run_state._original_input,
                        original_pre_step_items=run_state._generated_items,
                        new_response=last_model_response,
                        processed_response=run_state._last_processed_response,
                        hooks=hooks,
                        context_wrapper=context_wrapper,
                        run_config=run_config,
                        run_state=run_state,
                    )

                    tool_use_tracker.add_tool_use(
                        current_agent, run_state._last_processed_response.tools_used
                    )
                    streamed_result._tool_use_tracker_snapshot = serialize_tool_use_tracker(
                        tool_use_tracker
                    )

                    pending_approval_items, rewind_count = collect_approvals_and_rewind(
                        run_state._current_step, run_state._generated_items
                    )

                    if rewind_count > 0:
                        streamed_result._current_turn_persisted_item_count = apply_rewind_offset(
                            streamed_result._current_turn_persisted_item_count, rewind_count
                        )

                    streamed_result.input = turn_result.original_input
                    streamed_result._original_input = copy_input_items(turn_result.original_input)
                    streamed_result.new_items = turn_result.generated_items
                    run_state._original_input = copy_input_items(turn_result.original_input)
                    run_state._generated_items = turn_result.generated_items
                    run_state._current_step = turn_result.next_step  # type: ignore[assignment]
                    run_state._current_turn_persisted_item_count = (
                        streamed_result._current_turn_persisted_item_count
                    )

                    stream_step_items_to_queue(
                        turn_result.new_step_items, streamed_result._event_queue
                    )

                    if isinstance(turn_result.next_step, NextStepInterruption):
                        if session is not None and server_conversation_tracker is None:
                            should_skip_session_save = (
                                await input_guardrail_tripwire_triggered_for_stream(streamed_result)
                            )
                            if should_skip_session_save is False:
                                await save_result_to_session(
                                    session,
                                    [],
                                    turn_result.new_step_items,
                                    streamed_result._state,
                                    response_id=turn_result.model_response.response_id,
                                )
                                streamed_result._current_turn_persisted_item_count = (
                                    streamed_result._state._current_turn_persisted_item_count
                                )
                        streamed_result.interruptions = filter_tool_approvals(
                            turn_result.next_step.interruptions
                        )
                        streamed_result._last_processed_response = (
                            run_state._last_processed_response
                        )
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        break

                    if isinstance(turn_result.next_step, NextStepHandoff):
                        current_agent = turn_result.next_step.new_agent
                        if current_span:
                            current_span.finish(reset_current=True)
                        current_span = None
                        should_run_agent_start_hooks = True
                        streamed_result._event_queue.put_nowait(
                            AgentUpdatedStreamEvent(new_agent=current_agent)
                        )
                        run_state._current_step = NextStepRunAgain()  # type: ignore[assignment]
                        continue

                    if isinstance(turn_result.next_step, NextStepFinalOutput):
                        streamed_result._output_guardrails_task = asyncio.create_task(
                            run_output_guardrails(
                                current_agent.output_guardrails
                                + (run_config.output_guardrails or []),
                                current_agent,
                                turn_result.next_step.output,
                                context_wrapper,
                            )
                        )

                        try:
                            output_guardrail_results = await streamed_result._output_guardrails_task
                        except Exception:
                            output_guardrail_results = []

                        streamed_result.output_guardrail_results = output_guardrail_results
                        streamed_result.final_output = turn_result.next_step.output
                        streamed_result.is_complete = True

                        if session is not None and server_conversation_tracker is None:
                            should_skip_session_save = (
                                await input_guardrail_tripwire_triggered_for_stream(streamed_result)
                            )
                            if should_skip_session_save is False:
                                await save_result_to_session(
                                    session,
                                    [],
                                    turn_result.new_step_items,
                                    streamed_result._state,
                                    response_id=turn_result.model_response.response_id,
                                )
                                streamed_result._current_turn_persisted_item_count = (
                                    streamed_result._state._current_turn_persisted_item_count
                                )

                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        break

                    if isinstance(turn_result.next_step, NextStepRunAgain):
                        run_state._current_step = NextStepRunAgain()  # type: ignore[assignment]
                        continue

                    run_state._current_step = None

            if streamed_result._cancel_mode == "after_turn":
                streamed_result.is_complete = True
                streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                break

            if streamed_result.is_complete:
                break

            all_tools = await get_all_tools(current_agent, context_wrapper)
            await initialize_computer_tools(tools=all_tools, context_wrapper=context_wrapper)

            if current_span is None:
                handoff_names = [
                    h.agent_name for h in await get_handoffs(current_agent, context_wrapper)
                ]
                if output_schema := get_output_schema(current_agent):
                    output_type_name = output_schema.name()
                else:
                    output_type_name = "str"

                current_span = agent_span(
                    name=current_agent.name,
                    handoffs=handoff_names,
                    output_type=output_type_name,
                )
                current_span.start(mark_as_current=True)
                tool_names = [t.name for t in all_tools]
                current_span.span_data.tools = tool_names

            last_model_response_check: ModelResponse | None = None
            if run_state is not None and run_state._model_responses:
                last_model_response_check = run_state._model_responses[-1]

            if run_state is None or last_model_response_check is None:
                current_turn += 1
                streamed_result.current_turn = current_turn
                streamed_result._current_turn_persisted_item_count = 0
                if run_state:
                    run_state._current_turn_persisted_item_count = 0

            if current_turn > max_turns:
                _error_tracing.attach_error_to_span(
                    current_span,
                    SpanError(
                        message="Max turns exceeded",
                        data={"max_turns": max_turns},
                    ),
                )
                streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                break

            if current_turn == 1:
                all_input_guardrails = starting_agent.input_guardrails + (
                    run_config.input_guardrails or []
                )
                sequential_guardrails = [g for g in all_input_guardrails if not g.run_in_parallel]
                parallel_guardrails = [g for g in all_input_guardrails if g.run_in_parallel]

                if sequential_guardrails:
                    await run_input_guardrails_with_queue(
                        starting_agent,
                        sequential_guardrails,
                        ItemHelpers.input_to_new_input_list(prepared_input),
                        context_wrapper,
                        streamed_result,
                        current_span,
                    )
                    for result in streamed_result.input_guardrail_results:
                        if result.output.tripwire_triggered:
                            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                            raise InputGuardrailTripwireTriggered(result)

                if parallel_guardrails:
                    streamed_result._input_guardrails_task = asyncio.create_task(
                        run_input_guardrails_with_queue(
                            starting_agent,
                            parallel_guardrails,
                            ItemHelpers.input_to_new_input_list(prepared_input),
                            context_wrapper,
                            streamed_result,
                            current_span,
                        )
                    )
            try:
                logger.debug(
                    "Starting turn %s, current_agent=%s",
                    current_turn,
                    current_agent.name,
                )
                if (
                    session is not None
                    and server_conversation_tracker is None
                    and not streamed_result._stream_input_persisted
                ):
                    streamed_result._original_input_for_persistence = (
                        session_input_items_for_persistence
                        if session_input_items_for_persistence is not None
                        else []
                    )
                turn_result = await run_single_turn_streamed(
                    streamed_result,
                    current_agent,
                    hooks,
                    context_wrapper,
                    run_config,
                    should_run_agent_start_hooks,
                    tool_use_tracker,
                    all_tools,
                    server_conversation_tracker,
                    pending_server_items=pending_server_items,
                    session=session,
                    session_items_to_rewind=(
                        streamed_result._original_input_for_persistence
                        if session is not None and server_conversation_tracker is None
                        else None
                    ),
                )
                logger.debug(
                    "Turn %s complete, next_step type=%s",
                    current_turn,
                    type(turn_result.next_step).__name__,
                )
                should_run_agent_start_hooks = False
                streamed_result._tool_use_tracker_snapshot = serialize_tool_use_tracker(
                    tool_use_tracker
                )

                streamed_result.raw_responses = streamed_result.raw_responses + [
                    turn_result.model_response
                ]
                streamed_result.input = turn_result.original_input
                streamed_result.new_items = turn_result.generated_items
                if server_conversation_tracker is not None:
                    pending_server_items = list(turn_result.new_step_items)

                if isinstance(turn_result.next_step, NextStepRunAgain):
                    streamed_result._current_turn_persisted_item_count = 0
                    if run_state:
                        run_state._current_turn_persisted_item_count = 0

                if server_conversation_tracker is not None:
                    server_conversation_tracker.track_server_items(turn_result.model_response)

                if isinstance(turn_result.next_step, NextStepHandoff):
                    current_agent = turn_result.next_step.new_agent
                    current_span.finish(reset_current=True)
                    current_span = None
                    should_run_agent_start_hooks = True
                    streamed_result._event_queue.put_nowait(
                        AgentUpdatedStreamEvent(new_agent=current_agent)
                    )
                    if streamed_result._state is not None:
                        streamed_result._state._current_step = NextStepRunAgain()

                    if streamed_result._cancel_mode == "after_turn":  # type: ignore[comparison-overlap]
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        break
                elif isinstance(turn_result.next_step, NextStepFinalOutput):
                    streamed_result._output_guardrails_task = asyncio.create_task(
                        run_output_guardrails(
                            current_agent.output_guardrails + (run_config.output_guardrails or []),
                            current_agent,
                            turn_result.next_step.output,
                            context_wrapper,
                        )
                    )

                    try:
                        output_guardrail_results = await streamed_result._output_guardrails_task
                    except Exception:
                        output_guardrail_results = []

                    streamed_result.output_guardrail_results = output_guardrail_results
                    streamed_result.final_output = turn_result.next_step.output
                    streamed_result.is_complete = True

                    if session is not None and server_conversation_tracker is None:
                        should_skip_session_save = (
                            await input_guardrail_tripwire_triggered_for_stream(streamed_result)
                        )
                        if should_skip_session_save is False:
                            await save_result_to_session(
                                session,
                                [],
                                turn_result.new_step_items,
                                streamed_result._state,
                                response_id=turn_result.model_response.response_id,
                            )
                            streamed_result._current_turn_persisted_item_count = (
                                streamed_result._state._current_turn_persisted_item_count
                            )

                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    break
                elif isinstance(turn_result.next_step, NextStepInterruption):
                    if session is not None and server_conversation_tracker is None:
                        should_skip_session_save = (
                            await input_guardrail_tripwire_triggered_for_stream(streamed_result)
                        )
                        if should_skip_session_save is False:
                            await save_result_to_session(
                                session,
                                [],
                                turn_result.new_step_items,
                                streamed_result._state,
                                response_id=turn_result.model_response.response_id,
                            )
                            streamed_result._current_turn_persisted_item_count = (
                                streamed_result._state._current_turn_persisted_item_count
                            )
                    streamed_result.interruptions = filter_tool_approvals(
                        turn_result.next_step.interruptions
                    )
                    streamed_result._last_processed_response = turn_result.processed_response
                    streamed_result.is_complete = True
                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    break
                elif isinstance(turn_result.next_step, NextStepRunAgain):
                    if streamed_result._state is not None:
                        streamed_result._state._current_step = NextStepRunAgain()

                    if streamed_result._cancel_mode == "after_turn":  # type: ignore[comparison-overlap]
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        break
            except Exception as e:
                if current_span and not isinstance(e, ModelBehaviorError):
                    _error_tracing.attach_error_to_span(
                        current_span,
                        SpanError(
                            message="Error in agent run",
                            data={"error": str(e)},
                        ),
                    )
                raise
    except AgentsException as exc:
        streamed_result.is_complete = True
        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
        exc.run_data = RunErrorDetails(
            input=streamed_result.input,
            new_items=streamed_result.new_items,
            raw_responses=streamed_result.raw_responses,
            last_agent=current_agent,
            context_wrapper=context_wrapper,
            input_guardrail_results=streamed_result.input_guardrail_results,
            output_guardrail_results=streamed_result.output_guardrail_results,
        )
        raise
    except Exception as e:
        if current_span and not isinstance(e, ModelBehaviorError):
            _error_tracing.attach_error_to_span(
                current_span,
                SpanError(
                    message="Error in agent run",
                    data={"error": str(e)},
                ),
            )
        streamed_result.is_complete = True
        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
        raise
    else:
        streamed_result.is_complete = True
    finally:
        _sync_conversation_tracking_from_tracker()
        if streamed_result._input_guardrails_task:
            try:
                triggered = await input_guardrail_tripwire_triggered_for_stream(streamed_result)
                if triggered:
                    first_trigger = next(
                        (
                            result
                            for result in streamed_result.input_guardrail_results
                            if result.output.tripwire_triggered
                        ),
                        None,
                    )
                    if first_trigger is not None:
                        raise InputGuardrailTripwireTriggered(first_trigger)
            except Exception as e:
                logger.debug(
                    f"Error in streamed_result finalize for agent {current_agent.name} - {e}"
                )
        try:
            await dispose_resolved_computers(run_context=context_wrapper)
        except Exception as error:
            logger.warning("Failed to dispose computers after streamed run: %s", error)
        if current_span:
            current_span.finish(reset_current=True)
        if streamed_result.trace:
            streamed_result.trace.finish(reset_current=True)

        if not streamed_result.is_complete:
            streamed_result.is_complete = True
            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())


async def run_single_turn_streamed(
    streamed_result: RunResultStreaming,
    agent: Agent[TContext],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    should_run_agent_start_hooks: bool,
    tool_use_tracker: AgentToolUseTracker,
    all_tools: list[Tool],
    server_conversation_tracker: OpenAIServerConversationTracker | None = None,
    session: Session | None = None,
    session_items_to_rewind: list[TResponseInputItem] | None = None,
    pending_server_items: list[RunItem] | None = None,
) -> SingleStepResult:
    """Run a single streamed turn and emit events as results arrive."""
    emitted_tool_call_ids: set[str] = set()
    emitted_reasoning_item_ids: set[str] = set()

    try:
        context_wrapper.turn_input = ItemHelpers.input_to_new_input_list(streamed_result.input)
    except Exception:
        context_wrapper.turn_input = []

    if should_run_agent_start_hooks:
        await asyncio.gather(
            hooks.on_agent_start(context_wrapper, agent),
            (
                agent.hooks.on_start(context_wrapper, agent)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

    output_schema = get_output_schema(agent)

    streamed_result.current_agent = agent
    streamed_result._current_agent_output_schema = output_schema

    system_prompt, prompt_config = await asyncio.gather(
        agent.get_system_prompt(context_wrapper),
        agent.get_prompt(context_wrapper),
    )

    handoffs = await get_handoffs(agent, context_wrapper)
    model = get_model(agent, run_config)
    model_settings = agent.model_settings.resolve(run_config.model_settings)
    model_settings = maybe_reset_tool_choice(agent, tool_use_tracker, model_settings)

    final_response: ModelResponse | None = None

    if server_conversation_tracker is not None:
        original_input_for_tracking = ItemHelpers.input_to_new_input_list(streamed_result.input)
        items_for_input = (
            pending_server_items if pending_server_items else streamed_result.new_items
        )
        for item in items_for_input:
            if item.type == "tool_approval_item":
                continue
            input_item = item.to_input_item()
            original_input_for_tracking.append(input_item)

        input = server_conversation_tracker.prepare_input(streamed_result.input, items_for_input)
        logger.debug(
            "prepare_input returned %s items; remaining_initial_input=%s",
            len(input),
            len(server_conversation_tracker.remaining_initial_input)
            if server_conversation_tracker.remaining_initial_input
            else 0,
        )
    else:
        input = ItemHelpers.input_to_new_input_list(streamed_result.input)
        append_input_items_excluding_approvals(input, streamed_result.new_items)

    if isinstance(input, list):
        input = normalize_input_items_for_api(input)
        input = deduplicate_input_items(input)

    filtered = await maybe_filter_model_input(
        agent=agent,
        run_config=run_config,
        context_wrapper=context_wrapper,
        input_items=input,
        system_instructions=system_prompt,
    )
    if isinstance(filtered.input, list):
        filtered.input = deduplicate_input_items(filtered.input)
    if server_conversation_tracker is not None:
        logger.debug(
            "filtered.input has %s items; ids=%s",
            len(filtered.input),
            [id(i) for i in filtered.input],
        )
        server_conversation_tracker.mark_input_as_sent(original_input_for_tracking)
    if not filtered.input and server_conversation_tracker is None:
        raise RuntimeError("Prepared model input is empty")

    await asyncio.gather(
        hooks.on_llm_start(context_wrapper, agent, filtered.instructions, filtered.input),
        (
            agent.hooks.on_llm_start(context_wrapper, agent, filtered.instructions, filtered.input)
            if agent.hooks
            else _coro.noop_coroutine()
        ),
    )

    if (
        not streamed_result._stream_input_persisted
        and session is not None
        and server_conversation_tracker is None
        and streamed_result._original_input_for_persistence
        and len(streamed_result._original_input_for_persistence) > 0
    ):
        streamed_result._stream_input_persisted = True
        input_items_to_save = [
            ensure_input_item_format(item)
            for item in ItemHelpers.input_to_new_input_list(
                streamed_result._original_input_for_persistence
            )
        ]
        if input_items_to_save:
            await save_result_to_session(session, input_items_to_save, [], streamed_result._state)

    previous_response_id = (
        server_conversation_tracker.previous_response_id
        if server_conversation_tracker
        and server_conversation_tracker.previous_response_id is not None
        else None
    )
    conversation_id = (
        server_conversation_tracker.conversation_id if server_conversation_tracker else None
    )
    if conversation_id:
        logger.debug("Using conversation_id=%s", conversation_id)
    else:
        logger.debug("No conversation_id available for request")

    async for event in model.stream_response(
        filtered.instructions,
        filtered.input,
        model_settings,
        all_tools,
        output_schema,
        handoffs,
        get_model_tracing_impl(
            run_config.tracing_disabled, run_config.trace_include_sensitive_data
        ),
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
        prompt=prompt_config,
    ):
        streamed_result._event_queue.put_nowait(RawResponsesStreamEvent(data=event))

        if isinstance(event, ResponseCompletedEvent):
            usage = (
                Usage(
                    requests=1,
                    input_tokens=event.response.usage.input_tokens,
                    output_tokens=event.response.usage.output_tokens,
                    total_tokens=event.response.usage.total_tokens,
                    input_tokens_details=event.response.usage.input_tokens_details,
                    output_tokens_details=event.response.usage.output_tokens_details,
                )
                if event.response.usage
                else Usage()
            )
            final_response = ModelResponse(
                output=event.response.output,
                usage=usage,
                response_id=event.response.id,
            )
            context_wrapper.usage.add(usage)

        if isinstance(event, ResponseOutputItemDoneEvent):
            output_item = event.item

            if isinstance(output_item, TOOL_CALL_TYPES):
                output_call_id: str | None = getattr(
                    output_item, "call_id", getattr(output_item, "id", None)
                )

                if (
                    output_call_id
                    and isinstance(output_call_id, str)
                    and output_call_id not in emitted_tool_call_ids
                ):
                    emitted_tool_call_ids.add(output_call_id)

                    tool_item = ToolCallItem(
                        raw_item=cast(ToolCallItemTypes, output_item),
                        agent=agent,
                    )
                    streamed_result._event_queue.put_nowait(
                        RunItemStreamEvent(item=tool_item, name="tool_called")
                    )

            elif isinstance(output_item, ResponseReasoningItem):
                reasoning_id: str | None = getattr(output_item, "id", None)

                if reasoning_id and reasoning_id not in emitted_reasoning_item_ids:
                    emitted_reasoning_item_ids.add(reasoning_id)

                    reasoning_item = ReasoningItem(raw_item=output_item, agent=agent)
                    streamed_result._event_queue.put_nowait(
                        RunItemStreamEvent(item=reasoning_item, name="reasoning_item_created")
                    )

    if final_response is not None:
        await asyncio.gather(
            (
                agent.hooks.on_llm_end(context_wrapper, agent, final_response)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
            hooks.on_llm_end(context_wrapper, agent, final_response),
        )

    if not final_response:
        raise ModelBehaviorError("Model did not produce a final response!")

    if server_conversation_tracker is not None:
        server_conversation_tracker.track_server_items(final_response)

    single_step_result = await get_single_step_result_from_response(
        agent=agent,
        original_input=streamed_result.input,
        pre_step_items=streamed_result.new_items,
        new_response=final_response,
        output_schema=output_schema,
        all_tools=all_tools,
        handoffs=handoffs,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
        tool_use_tracker=tool_use_tracker,
        event_queue=streamed_result._event_queue,
    )

    items_to_filter = single_step_result.new_step_items

    if emitted_tool_call_ids:
        items_to_filter = [
            item
            for item in items_to_filter
            if not (
                isinstance(item, ToolCallItem)
                and (
                    call_id := getattr(item.raw_item, "call_id", getattr(item.raw_item, "id", None))
                )
                and call_id in emitted_tool_call_ids
            )
        ]

    if emitted_reasoning_item_ids:
        items_to_filter = [
            item
            for item in items_to_filter
            if not (
                isinstance(item, ReasoningItem)
                and (reasoning_id := getattr(item.raw_item, "id", None))
                and reasoning_id in emitted_reasoning_item_ids
            )
        ]

    items_to_filter = [item for item in items_to_filter if not isinstance(item, HandoffCallItem)]

    filtered_result = _dc.replace(single_step_result, new_step_items=items_to_filter)
    stream_step_result_to_queue(filtered_result, streamed_result._event_queue)
    return single_step_result


async def run_single_turn(
    *,
    agent: Agent[TContext],
    all_tools: list[Tool],
    original_input: str | list[TResponseInputItem],
    starting_input: str | list[TResponseInputItem],
    generated_items: list[RunItem],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    should_run_agent_start_hooks: bool,
    tool_use_tracker: AgentToolUseTracker,
    server_conversation_tracker: OpenAIServerConversationTracker | None = None,
    model_responses: list[ModelResponse] | None = None,
    session: Session | None = None,
    session_items_to_rewind: list[TResponseInputItem] | None = None,
) -> SingleStepResult:
    """Run a single non-streaming turn of the agent loop."""
    try:
        context_wrapper.turn_input = ItemHelpers.input_to_new_input_list(original_input)
    except Exception:
        context_wrapper.turn_input = []

    if should_run_agent_start_hooks:
        await asyncio.gather(
            hooks.on_agent_start(context_wrapper, agent),
            (
                agent.hooks.on_start(context_wrapper, agent)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

    system_prompt, prompt_config = await asyncio.gather(
        agent.get_system_prompt(context_wrapper),
        agent.get_prompt(context_wrapper),
    )

    output_schema = get_output_schema(agent)
    handoffs = await get_handoffs(agent, context_wrapper)
    if server_conversation_tracker is not None:
        input = server_conversation_tracker.prepare_input(original_input, generated_items)
    else:
        input = ItemHelpers.input_to_new_input_list(original_input)
        if isinstance(input, list):
            append_input_items_excluding_approvals(input, generated_items)
        else:
            input = ItemHelpers.input_to_new_input_list(input)
            append_input_items_excluding_approvals(input, generated_items)

    if isinstance(input, list):
        input = normalize_input_items_for_api(input)

    new_response = await get_new_response(
        agent,
        system_prompt,
        input,
        output_schema,
        all_tools,
        handoffs,
        hooks,
        context_wrapper,
        run_config,
        tool_use_tracker,
        server_conversation_tracker,
        prompt_config,
        session=session,
        session_items_to_rewind=session_items_to_rewind,
    )

    return await get_single_step_result_from_response(
        agent=agent,
        original_input=original_input,
        pre_step_items=generated_items,
        new_response=new_response,
        output_schema=output_schema,
        all_tools=all_tools,
        handoffs=handoffs,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
        tool_use_tracker=tool_use_tracker,
    )


async def get_single_step_result_from_response(
    *,
    agent: Agent[TContext],
    all_tools: list[Tool],
    original_input: str | list[TResponseInputItem],
    pre_step_items: list[RunItem],
    new_response: ModelResponse,
    output_schema: AgentOutputSchemaBase | None,
    handoffs: list[Handoff],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    tool_use_tracker: AgentToolUseTracker,
    event_queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel] | None = None,
) -> SingleStepResult:
    """Process a model response into a single step result and execute tools."""
    processed_response = process_model_response(
        agent=agent,
        all_tools=all_tools,
        response=new_response,
        output_schema=output_schema,
        handoffs=handoffs,
    )

    tool_use_tracker.add_tool_use(agent, processed_response.tools_used)

    if event_queue is not None and processed_response.new_items:
        handoff_items = [
            item for item in processed_response.new_items if isinstance(item, HandoffCallItem)
        ]
        if handoff_items:
            stream_step_items_to_queue(cast(list[RunItem], handoff_items), event_queue)

    return await execute_tools_and_side_effects(
        agent=agent,
        original_input=original_input,
        pre_step_items=pre_step_items,
        new_response=new_response,
        processed_response=processed_response,
        output_schema=output_schema,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
    )


async def run_input_guardrails(
    agent: Agent[Any],
    guardrails: list[InputGuardrail[TContext]],
    input: str | list[TResponseInputItem],
    context: RunContextWrapper[TContext],
) -> list[InputGuardrailResult]:
    """Run input guardrails sequentially and raise on tripwires."""
    if not guardrails:
        return []

    guardrail_tasks = [
        asyncio.create_task(run_single_input_guardrail(agent, guardrail, input, context))
        for guardrail in guardrails
    ]

    guardrail_results = []

    for done in asyncio.as_completed(guardrail_tasks):
        result = await done
        if result.output.tripwire_triggered:
            for t in guardrail_tasks:
                t.cancel()
            await asyncio.gather(*guardrail_tasks, return_exceptions=True)
            _error_tracing.attach_error_to_current_span(
                SpanError(
                    message="Guardrail tripwire triggered",
                    data={"guardrail": result.guardrail.get_name()},
                )
            )
            raise InputGuardrailTripwireTriggered(result)
        else:
            guardrail_results.append(result)

    return guardrail_results


async def run_output_guardrails(
    guardrails: list[OutputGuardrail[TContext]],
    agent: Agent[TContext],
    agent_output: Any,
    context: RunContextWrapper[TContext],
) -> list[OutputGuardrailResult]:
    """Run output guardrails in parallel and raise on tripwires."""
    if not guardrails:
        return []

    guardrail_tasks = [
        asyncio.create_task(run_single_output_guardrail(guardrail, agent, agent_output, context))
        for guardrail in guardrails
    ]

    guardrail_results = []

    for done in asyncio.as_completed(guardrail_tasks):
        result = await done
        if result.output.tripwire_triggered:
            for t in guardrail_tasks:
                t.cancel()
            _error_tracing.attach_error_to_current_span(
                SpanError(
                    message="Guardrail tripwire triggered",
                    data={"guardrail": result.guardrail.get_name()},
                )
            )
            raise OutputGuardrailTripwireTriggered(result)
        else:
            guardrail_results.append(result)

    return guardrail_results


async def get_new_response(
    agent: Agent[TContext],
    system_prompt: str | None,
    input: list[TResponseInputItem],
    output_schema: AgentOutputSchemaBase | None,
    all_tools: list[Tool],
    handoffs: list[Handoff],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    tool_use_tracker: AgentToolUseTracker,
    server_conversation_tracker: OpenAIServerConversationTracker | None,
    prompt_config: ResponsePromptParam | None,
    session: Session | None = None,
    session_items_to_rewind: list[TResponseInputItem] | None = None,
) -> ModelResponse:
    """Call the model and return the raw response, handling retries and hooks."""
    filtered = await maybe_filter_model_input(
        agent=agent,
        run_config=run_config,
        context_wrapper=context_wrapper,
        input_items=input,
        system_instructions=system_prompt,
    )
    if isinstance(filtered.input, list):
        filtered.input = deduplicate_input_items(filtered.input)

    if server_conversation_tracker is not None:
        server_conversation_tracker.mark_input_as_sent(input)

    model = get_model(agent, run_config)
    model_settings = agent.model_settings.resolve(run_config.model_settings)
    model_settings = maybe_reset_tool_choice(agent, tool_use_tracker, model_settings)

    await asyncio.gather(
        hooks.on_llm_start(context_wrapper, agent, filtered.instructions, filtered.input),
        (
            agent.hooks.on_llm_start(
                context_wrapper,
                agent,
                filtered.instructions,
                filtered.input,
            )
            if agent.hooks
            else _coro.noop_coroutine()
        ),
    )

    previous_response_id = (
        server_conversation_tracker.previous_response_id
        if server_conversation_tracker
        and server_conversation_tracker.previous_response_id is not None
        else None
    )
    conversation_id = (
        server_conversation_tracker.conversation_id if server_conversation_tracker else None
    )
    if conversation_id:
        logger.debug("Using conversation_id=%s", conversation_id)
    else:
        logger.debug("No conversation_id available for request")

    try:
        new_response = await model.get_response(
            system_instructions=filtered.instructions,
            input=filtered.input,
            model_settings=model_settings,
            tools=all_tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=get_model_tracing_impl(
                run_config.tracing_disabled, run_config.trace_include_sensitive_data
            ),
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt_config,
        )
    except Exception as exc:
        from openai import BadRequestError

        if isinstance(exc, BadRequestError) and getattr(exc, "code", "") == "conversation_locked":
            max_retries = 3
            last_exception = exc
            for attempt in range(max_retries):
                wait_time = 1.0 * (2**attempt)
                logger.debug(
                    "Conversation locked, retrying in %ss (attempt %s/%s)",
                    wait_time,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(wait_time)
                items_to_rewind = (
                    session_items_to_rewind if session_items_to_rewind is not None else []
                )
                await rewind_session_items(session, items_to_rewind, server_conversation_tracker)
                if server_conversation_tracker is not None:
                    server_conversation_tracker.rewind_input(filtered.input)
                try:
                    new_response = await model.get_response(
                        system_instructions=filtered.instructions,
                        input=filtered.input,
                        model_settings=model_settings,
                        tools=all_tools,
                        output_schema=output_schema,
                        handoffs=handoffs,
                        tracing=get_model_tracing_impl(
                            run_config.tracing_disabled, run_config.trace_include_sensitive_data
                        ),
                        previous_response_id=previous_response_id,
                        conversation_id=conversation_id,
                        prompt=prompt_config,
                    )
                    break
                except BadRequestError as retry_exc:
                    last_exception = retry_exc
                    if (
                        getattr(retry_exc, "code", "") == "conversation_locked"
                        and attempt < max_retries - 1
                    ):
                        continue
                    else:
                        raise
            else:
                logger.error(
                    "Conversation locked after all retries; filtered.input=%s", filtered.input
                )
                raise last_exception
        else:
            logger.error("Error getting response; filtered.input=%s", filtered.input)
            raise

    context_wrapper.usage.add(new_response.usage)

    await asyncio.gather(
        (
            agent.hooks.on_llm_end(context_wrapper, agent, new_response)
            if agent.hooks
            else _coro.noop_coroutine()
        ),
        hooks.on_llm_end(context_wrapper, agent, new_response),
    )

    return new_response


def get_output_schema(agent: Agent[Any]) -> AgentOutputSchemaBase | None:
    """Return the resolved output schema for the agent, if any."""
    if agent.output_type is None or agent.output_type is str:
        return None
    elif isinstance(agent.output_type, AgentOutputSchemaBase):
        return agent.output_type

    return AgentOutputSchema(agent.output_type)


async def get_handoffs(agent: Agent[Any], context_wrapper: RunContextWrapper[Any]) -> list[Handoff]:
    """Return enabled handoffs for the agent."""
    handoffs = []
    for handoff_item in agent.handoffs:
        if isinstance(handoff_item, Handoff):
            handoffs.append(handoff_item)
        elif isinstance(handoff_item, Agent):
            handoffs.append(handoff(handoff_item))

    async def check_handoff_enabled(handoff_obj: Handoff) -> bool:
        attr = handoff_obj.is_enabled
        if isinstance(attr, bool):
            return attr
        res = attr(context_wrapper, agent)
        if inspect.isawaitable(res):
            return bool(await res)
        return bool(res)

    results = await asyncio.gather(*(check_handoff_enabled(h) for h in handoffs))
    enabled: list[Handoff] = [h for h, ok in zip(handoffs, results) if ok]
    return enabled


async def get_all_tools(agent: Agent[Any], context_wrapper: RunContextWrapper[Any]) -> list[Tool]:
    """Fetch all tools available to the agent."""
    return await agent.get_all_tools(context_wrapper)


def get_model(agent: Agent[Any], run_config: RunConfig) -> Model:
    """Resolve the model instance for this run."""
    if isinstance(run_config.model, Model):
        return run_config.model
    elif isinstance(run_config.model, str):
        return run_config.model_provider.get_model(run_config.model)
    elif isinstance(agent.model, Model):
        return agent.model

    return run_config.model_provider.get_model(agent.model)


async def input_guardrail_tripwire_triggered_for_stream(
    streamed_result: RunResultStreaming,
) -> bool:
    """Return True if any input guardrail triggered during a streamed run."""
    task = streamed_result._input_guardrails_task
    if task is None:
        return False

    if not task.done():
        await task

    return any(
        guardrail_result.output.tripwire_triggered
        for guardrail_result in streamed_result.input_guardrail_results
    )

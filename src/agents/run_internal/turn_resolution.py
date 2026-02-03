from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, cast

from openai.types.responses import (
    ResponseCompactionItem,
    ResponseComputerToolCall,
    ResponseCustomToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseOutputMessage,
)
from openai.types.responses.response_code_interpreter_tool_call import (
    ResponseCodeInterpreterToolCall,
)
from openai.types.responses.response_output_item import (
    ImageGenerationCall,
    LocalShellCall,
    McpApprovalRequest,
    McpCall,
    McpListTools,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from ..agent import Agent, ToolsToFinalOutputResult
from ..agent_output import AgentOutputSchemaBase
from ..agent_tool_state import peek_agent_tool_run_result
from ..exceptions import ModelBehaviorError, UserError
from ..handoffs import Handoff, HandoffInputData, nest_handoff_history
from ..items import (
    CompactionItem,
    HandoffCallItem,
    HandoffOutputItem,
    ItemHelpers,
    MCPApprovalRequestItem,
    MCPListToolsItem,
    MessageOutputItem,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from ..lifecycle import RunHooks
from ..logger import logger
from ..run_config import RunConfig
from ..run_context import AgentHookContext, RunContextWrapper, TContext
from ..run_state import RunState
from ..stream_events import StreamEvent
from ..tool import (
    ApplyPatchTool,
    ComputerTool,
    FunctionTool,
    FunctionToolResult,
    HostedMCPTool,
    LocalShellTool,
    ShellTool,
    Tool,
)
from ..tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from ..tracing import SpanError, handoff_span
from ..util import _coro, _error_tracing
from ..util._approvals import evaluate_needs_approval_setting
from .items import (
    REJECTION_MESSAGE,
    apply_patch_rejection_item,
    function_rejection_item,
    shell_rejection_item,
)
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
from .streaming import stream_step_items_to_queue
from .tool_execution import (
    build_litellm_json_tool_call,
    coerce_apply_patch_operation,
    coerce_shell_call,
    extract_apply_patch_call_id,
    extract_shell_call_id,
    extract_tool_call_id,
    function_needs_approval,
    get_mapping_or_attr,
    index_approval_items_by_call_id,
    is_apply_patch_name,
    parse_apply_patch_custom_input,
    parse_apply_patch_function_args,
    process_hosted_mcp_approvals,
    resolve_approval_rejection_message,
    should_keep_hosted_mcp_item,
)
from .tool_planning import (
    _append_mcp_callback_results,
    _build_plan_for_fresh_turn,
    _build_plan_for_resume_turn,
    _build_tool_output_index,
    _build_tool_result_items,
    _collect_runs_by_approval,
    _collect_tool_interruptions,
    _dedupe_tool_call_items,
    _execute_tool_plan,
    _make_unique_item_appender,
    _select_function_tool_runs_for_resume,
)

__all__ = [
    "execute_final_output_step",
    "execute_final_output",
    "execute_handoffs",
    "check_for_final_output_from_tools",
    "process_model_response",
    "execute_tools_and_side_effects",
    "resolve_interrupted_turn",
    "get_single_step_result_from_response",
    "run_final_output_hooks",
]


async def _maybe_finalize_from_tool_results(
    *,
    agent: Agent[TContext],
    original_input: str | list[TResponseInputItem],
    new_response: ModelResponse,
    pre_step_items: list[RunItem],
    new_step_items: list[RunItem],
    function_results: list[FunctionToolResult],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    tool_input_guardrail_results: list[ToolInputGuardrailResult],
    tool_output_guardrail_results: list[ToolOutputGuardrailResult],
) -> SingleStepResult | None:
    check_tool_use = await check_for_final_output_from_tools(
        agent, function_results, context_wrapper
    )
    if not check_tool_use.is_final_output:
        return None

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
    run_final_output_hooks_fn: Callable[
        [Agent[Any], RunHooks[Any], RunContextWrapper[Any], Any], Awaitable[None]
    ]
    | None = None,
) -> SingleStepResult:
    """Finalize a turn once final output is known and run end hooks."""
    final_output_hooks = run_final_output_hooks_fn or run_final_output_hooks
    await final_output_hooks(agent, hooks, context_wrapper, final_output)

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
    run_final_output_hooks_fn: Callable[
        [Agent[Any], RunHooks[Any], RunContextWrapper[Any], Any], Awaitable[None]
    ]
    | None = None,
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
        run_final_output_hooks_fn=run_final_output_hooks_fn,
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
    nest_handoff_history_fn: Callable[..., HandoffInputData] | None = None,
) -> SingleStepResult:
    """Execute a handoff and prepare the next turn for the new agent."""

    def nest_history(data: HandoffInputData, mapper: Any | None = None) -> HandoffInputData:
        if nest_handoff_history_fn is None:
            return nest_handoff_history(data, history_mapper=mapper)
        return nest_handoff_history_fn(data, mapper)

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
        session_step_items: list[RunItem] | None = None
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
            # For custom input filters, keep full new_items for session history and
            # use input_items for model input when provided.
            if filtered.input_items is not None:
                session_step_items = list(filtered.new_items)
                new_step_items = list(filtered.input_items)
            else:
                session_step_items = None
        elif should_nest_history and handoff_input_data is not None:
            nested = nest_history(handoff_input_data, run_config.handoff_history_mapper)
            original_input = (
                nested.input_history
                if isinstance(nested.input_history, str)
                else list(nested.input_history)
            )
            pre_step_items = list(nested.pre_handoff_items)
            # Keep full new_items for session history.
            session_step_items = list(nested.new_items)
            # Use input_items (filtered) for model input if available.
            if nested.input_items is not None:
                new_step_items = list(nested.input_items)
            else:
                new_step_items = session_step_items
        else:
            # No filtering or nesting - session_step_items not needed.
            session_step_items = None

    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        next_step=NextStepHandoff(new_agent),
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        session_step_items=session_step_items,
    )


async def check_for_final_output_from_tools(
    agent: Agent[TContext],
    tool_results: list[FunctionToolResult],
    context_wrapper: RunContextWrapper[TContext],
) -> ToolsToFinalOutputResult:
    """Determine if tool results should produce a final output."""
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
        return cast(
            ToolsToFinalOutputResult, agent.tool_use_behavior(context_wrapper, tool_results)
        )

    logger.error("Invalid tool_use_behavior: %s", agent.tool_use_behavior)
    raise UserError(f"Invalid tool_use_behavior: {agent.tool_use_behavior}")


async def execute_tools_and_side_effects(
    *,
    agent: Agent[TContext],
    original_input: str | list[TResponseInputItem],
    pre_step_items: list[RunItem],
    new_response: ModelResponse,
    processed_response: ProcessedResponse,
    output_schema: AgentOutputSchemaBase | None,
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
) -> SingleStepResult:
    """Run one turn of the loop, coordinating tools, approvals, guardrails, and handoffs."""

    execute_final_output_call = execute_final_output
    execute_handoffs_call = execute_handoffs

    pre_step_items = list(pre_step_items)
    approval_items_by_call_id = index_approval_items_by_call_id(pre_step_items)

    plan = _build_plan_for_fresh_turn(
        processed_response=processed_response,
        agent=agent,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
    )

    new_step_items = _dedupe_tool_call_items(
        existing_items=pre_step_items,
        new_items=processed_response.new_items,
    )

    (
        function_results,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        computer_results,
        shell_results,
        apply_patch_results,
        local_shell_results,
    ) = await _execute_tool_plan(
        plan=plan,
        agent=agent,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
    )
    new_step_items.extend(
        _build_tool_result_items(
            function_results=function_results,
            computer_results=computer_results,
            shell_results=shell_results,
            apply_patch_results=apply_patch_results,
            local_shell_results=local_shell_results,
        )
    )

    interruptions = _collect_tool_interruptions(
        function_results=function_results,
        shell_results=shell_results,
        apply_patch_results=apply_patch_results,
    )
    if plan.approved_mcp_responses:
        new_step_items.extend(plan.approved_mcp_responses)
    if plan.pending_interruptions:
        interruptions.extend(plan.pending_interruptions)
        new_step_items.extend(plan.pending_interruptions)

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

    await _append_mcp_callback_results(
        agent=agent,
        requests=plan.mcp_requests_with_callback,
        context_wrapper=context_wrapper,
        append_item=new_step_items.append,
    )

    if run_handoffs := processed_response.handoffs:
        return await execute_handoffs_call(
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

    tool_final_output = await _maybe_finalize_from_tool_results(
        agent=agent,
        original_input=original_input,
        new_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        function_results=function_results,
        hooks=hooks,
        context_wrapper=context_wrapper,
        tool_input_guardrail_results=tool_input_guardrail_results,
        tool_output_guardrail_results=tool_output_guardrail_results,
    )
    if tool_final_output is not None:
        return tool_final_output

    message_items = [item for item in new_step_items if isinstance(item, MessageOutputItem)]
    potential_final_output_text = (
        ItemHelpers.extract_last_text(message_items[-1].raw_item) if message_items else None
    )

    if not processed_response.has_tools_or_approvals_to_run():
        if output_schema and not output_schema.is_plain_text() and potential_final_output_text:
            final_output = output_schema.validate_json(potential_final_output_text)
            return await execute_final_output_call(
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
        if not output_schema or output_schema.is_plain_text():
            return await execute_final_output_call(
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
    nest_handoff_history_fn: Callable[..., HandoffInputData] | None = None,
) -> SingleStepResult:
    """Continue a turn that was previously interrupted waiting for tool approval."""

    execute_handoffs_call = execute_handoffs

    def nest_history(data: HandoffInputData, mapper: Any | None = None) -> HandoffInputData:
        if nest_handoff_history_fn is None:
            return nest_handoff_history(data, history_mapper=mapper)
        return nest_handoff_history_fn(data, mapper)

    def _pending_approvals_from_state() -> list[ToolApprovalItem]:
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

    async def _record_function_rejection(
        call_id: str | None,
        tool_call: ResponseFunctionToolCall,
        function_tool: FunctionTool,
    ) -> None:
        if isinstance(call_id, str) and call_id in rejected_function_call_ids:
            return
        rejection_message = REJECTION_MESSAGE
        if call_id:
            rejection_message = await resolve_approval_rejection_message(
                context_wrapper=context_wrapper,
                run_config=run_config,
                tool_type="function",
                tool_name=function_tool.name,
                call_id=call_id,
            )
        rejected_function_outputs.append(
            function_rejection_item(agent, tool_call, rejection_message=rejection_message)
        )
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

    pending_approval_items = _pending_approvals_from_state()
    approval_items_by_call_id = index_approval_items_by_call_id(pending_approval_items)

    rejected_function_outputs: list[RunItem] = []
    rejected_function_call_ids: set[str] = set()
    rerun_function_call_ids: set[str] = set()
    pending_interruptions: list[ToolApprovalItem] = []
    pending_interruption_keys: set[str] = set()

    output_index = _build_tool_output_index(original_pre_step_items)

    def _has_output_item(call_id: str, expected_type: str) -> bool:
        return (expected_type, call_id) in output_index

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

    async def _build_shell_rejection(run: ToolRunShellCall, call_id: str) -> RunItem:
        rejection_message = await resolve_approval_rejection_message(
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_type="shell",
            tool_name=run.shell_tool.name,
            call_id=call_id,
        )
        return cast(
            RunItem,
            shell_rejection_item(
                agent,
                call_id,
                rejection_message=rejection_message,
            ),
        )

    async def _build_apply_patch_rejection(run: ToolRunApplyPatchCall, call_id: str) -> RunItem:
        rejection_message = await resolve_approval_rejection_message(
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_type="apply_patch",
            tool_name=run.apply_patch_tool.name,
            call_id=call_id,
        )
        return cast(
            RunItem,
            apply_patch_rejection_item(
                agent,
                call_id,
                rejection_message=rejection_message,
            ),
        )

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

    def _nested_interruptions_status(
        interruptions: Sequence[ToolApprovalItem],
    ) -> Literal["approved", "pending", "rejected"]:
        has_pending = False
        for interruption in interruptions:
            call_id = extract_tool_call_id(interruption.raw_item)
            if not call_id:
                has_pending = True
                continue
            status = context_wrapper.get_approval_status(
                interruption.tool_name or "", call_id, existing_pending=interruption
            )
            if status is False:
                return "rejected"
            if status is None:
                has_pending = True
        return "pending" if has_pending else "approved"

    def _function_output_exists(run: ToolRunFunction) -> bool:
        call_id = extract_tool_call_id(run.tool_call)
        if not call_id:
            return False

        pending_run_result = peek_agent_tool_run_result(run.tool_call)
        if pending_run_result and getattr(pending_run_result, "interruptions", None):
            status = _nested_interruptions_status(pending_run_result.interruptions)
            if status in ("approved", "rejected"):
                rerun_function_call_ids.add(call_id)
                return False
            return True

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

    def _approval_matches_agent(approval: ToolApprovalItem) -> bool:
        approval_agent = approval.agent
        if approval_agent is None:
            return False
        if approval_agent is agent:
            return True
        return getattr(approval_agent, "name", None) == agent.name

    async def _rebuild_function_runs_from_approvals() -> list[ToolRunFunction]:
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

        def _add_unmatched_pending(approval: ToolApprovalItem) -> None:
            call_id = extract_tool_call_id(approval.raw_item)
            if not call_id:
                _add_pending_interruption(approval)
                return
            tool_name = approval.tool_name or ""
            approval_status = context_wrapper.get_approval_status(
                tool_name, call_id, existing_pending=approval
            )
            if approval_status is None:
                _add_pending_interruption(approval)

        for approval in pending_approval_items:
            if not isinstance(approval, ToolApprovalItem):
                continue
            if not _approval_matches_agent(approval):
                _add_unmatched_pending(approval)
                continue
            raw = approval.raw_item
            raw_type = get_mapping_or_attr(raw, "type")
            if raw_type != "function_call":
                _add_unmatched_pending(approval)
                continue
            name = get_mapping_or_attr(raw, "name")
            if not (isinstance(name, str) and name in tool_map):
                _add_unmatched_pending(approval)
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
                    _add_unmatched_pending(approval)
                    continue
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
                _add_unmatched_pending(approval)
                continue

            approval_status = context_wrapper.get_approval_status(
                name, rebuilt_call_id, existing_pending=approval
            )
            if approval_status is False:
                await _record_function_rejection(
                    rebuilt_call_id,
                    tool_call,
                    tool_map[name],
                )
                continue
            if approval_status is None:
                if rebuilt_call_id not in existing_pending_call_ids:
                    _add_pending_interruption(approval)
                    existing_pending_call_ids.add(rebuilt_call_id)
                continue
            rebuilt_runs.append(ToolRunFunction(function_tool=tool_map[name], tool_call=tool_call))
        return rebuilt_runs

    function_tool_runs = await _select_function_tool_runs_for_resume(
        processed_response.functions,
        approval_items_by_call_id=approval_items_by_call_id,
        context_wrapper=context_wrapper,
        needs_approval_checker=_function_requires_approval,
        output_exists_checker=_function_output_exists,
        record_rejection=_record_function_rejection,
        pending_interruption_adder=_add_pending_interruption,
        pending_item_builder=lambda run: ToolApprovalItem(agent=agent, raw_item=run.tool_call),
    )

    rebuilt_function_tool_runs = await _rebuild_function_runs_from_approvals()
    if rebuilt_function_tool_runs:
        existing_call_ids: set[str] = set()
        for run in function_tool_runs:
            call_id = extract_tool_call_id(run.tool_call)
            if call_id:
                existing_call_ids.add(call_id)
        for run in rebuilt_function_tool_runs:
            call_id = extract_tool_call_id(run.tool_call)
            if call_id and call_id in existing_call_ids:
                continue
            function_tool_runs.append(run)
            if call_id:
                existing_call_ids.add(call_id)

    pending_computer_actions: list[ToolRunComputerAction] = []
    for action in processed_response.computer_actions:
        call_id = _computer_call_id_from_run(action)
        if _computer_output_exists(call_id):
            continue
        pending_computer_actions.append(action)

    approved_shell_calls, rejected_shell_results = await _collect_runs_by_approval(
        processed_response.shell_calls,
        call_id_extractor=_shell_call_id_from_run,
        tool_name_resolver=_shell_tool_name,
        rejection_builder=_build_shell_rejection,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
        agent=agent,
        pending_interruption_adder=_add_pending_interruption,
        needs_approval_checker=_shell_needs_approval,
        output_exists_checker=_shell_output_exists,
    )

    approved_apply_patch_calls, rejected_apply_patch_results = await _collect_runs_by_approval(
        processed_response.apply_patch_calls,
        call_id_extractor=_apply_patch_call_id_from_run,
        tool_name_resolver=_apply_patch_tool_name,
        rejection_builder=_build_apply_patch_rejection,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
        agent=agent,
        pending_interruption_adder=_add_pending_interruption,
        needs_approval_checker=_apply_patch_needs_approval,
        output_exists_checker=_apply_patch_output_exists,
    )

    plan = _build_plan_for_resume_turn(
        processed_response=processed_response,
        agent=agent,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
        pending_interruptions=pending_interruptions,
        pending_interruption_adder=_add_pending_interruption,
        function_runs=function_tool_runs,
        computer_actions=pending_computer_actions,
        shell_calls=approved_shell_calls,
        apply_patch_calls=approved_apply_patch_calls,
    )

    (
        function_results,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        computer_results,
        shell_results,
        apply_patch_results,
        _local_shell_results,
    ) = await _execute_tool_plan(
        plan=plan,
        agent=agent,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
    )

    for interruption in _collect_tool_interruptions(
        function_results=function_results,
        shell_results=[],
        apply_patch_results=[],
    ):
        _add_pending_interruption(interruption)

    new_items, append_if_new = _make_unique_item_appender(original_pre_step_items)

    for item in _build_tool_result_items(
        function_results=function_results,
        computer_results=computer_results,
        shell_results=shell_results,
        apply_patch_results=apply_patch_results,
        local_shell_results=[],
    ):
        append_if_new(item)
    for rejection_item in rejected_function_outputs:
        append_if_new(rejection_item)
    for pending_item in pending_interruptions:
        if pending_item:
            append_if_new(pending_item)
    for shell_rejection in rejected_shell_results:
        append_if_new(shell_rejection)
    for apply_patch_rejection in rejected_apply_patch_results:
        append_if_new(apply_patch_rejection)
    for approved_response in plan.approved_mcp_responses:
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

    await _append_mcp_callback_results(
        agent=agent,
        requests=plan.mcp_requests_with_callback,
        context_wrapper=context_wrapper,
        append_item=append_if_new,
    )

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

    if rerun_function_call_ids:
        pre_step_items = [
            item
            for item in pre_step_items
            if not (
                item.type == "tool_call_output_item"
                and (
                    extract_tool_call_id(getattr(item, "raw_item", None)) in rerun_function_call_ids
                )
            )
        ]

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

    if pending_handoffs:
        return await execute_handoffs_call(
            agent=agent,
            original_input=original_input,
            pre_step_items=pre_step_items,
            new_step_items=new_items,
            new_response=new_response,
            run_handoffs=pending_handoffs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
            nest_handoff_history_fn=nest_history,
        )

    tool_final_output = await _maybe_finalize_from_tool_results(
        agent=agent,
        original_input=original_input,
        new_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_items,
        function_results=function_results,
        hooks=hooks,
        context_wrapper=context_wrapper,
        tool_input_guardrail_results=tool_input_guardrail_results,
        tool_output_guardrail_results=tool_output_guardrail_results,
    )
    if tool_final_output is not None:
        return tool_final_output

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
        if output_type == "compaction":
            if isinstance(output, dict):
                compaction_raw = dict(output)
            elif isinstance(output, ResponseCompactionItem):
                compaction_raw = output.model_dump(exclude_unset=True)
            else:
                logger.warning("Unexpected compaction output type, ignoring: %s", type(output))
                continue
            compaction_raw.pop("created_by", None)
            items.append(
                CompactionItem(agent=agent, raw_item=cast(TResponseInputItem, compaction_raw))
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
            # Look up MCP tool description from the server's cached tools list.
            # Tool discovery is async I/O, but this function is sync, so this is best-effort and
            # only works if the server has cached tool metadata (e.g., when cache_tools_list is
            # enabled).
            _mcp_description: str | None = None
            for _server in agent.mcp_servers:
                if _server.name == output.server_label:
                    for _tool in _server.cached_tools or []:
                        if _tool.name == output.name:
                            _mcp_description = _tool.description
                            break
                    break
            items.append(ToolCallItem(raw_item=output, agent=agent, description=_mcp_description))
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
            logger.warning("Unexpected output type, ignoring: %s", type(output))
            continue

        if not isinstance(output, ResponseFunctionToolCall):
            continue

        tools_used.append(output.name)

        if output.name in handoff_map:
            items.append(HandoffCallItem(raw_item=output, agent=agent))
            handoff = ToolRunHandoff(
                tool_call=output,
                handoff=handoff_map[output.name],
            )
            run_handoffs.append(handoff)
        else:
            if output.name not in function_map:
                if output_schema is not None and output.name == "json_tool_call":
                    items.append(ToolCallItem(raw_item=output, agent=agent))
                    functions.append(
                        ToolRunFunction(
                            tool_call=output,
                            function_tool=build_litellm_json_tool_call(output),
                        )
                    )
                    continue
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Tool not found",
                        data={"tool_name": output.name},
                    )
                )
                error = f"Tool {output.name} not found in agent {agent.name}"
                raise ModelBehaviorError(error)

            func_tool = function_map[output.name]
            items.append(
                ToolCallItem(raw_item=output, agent=agent, description=func_tool.description)
            )
            functions.append(
                ToolRunFunction(
                    tool_call=output,
                    function_tool=func_tool,
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
        interruptions=[],
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
    tool_use_tracker,
    event_queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel] | None = None,
) -> SingleStepResult:
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

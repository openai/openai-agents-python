from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Container, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any, Literal, cast

from openai.types.responses import (
    ResponseCompactionItem,
    ResponseComputerToolCall,
    ResponseCustomToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionShellToolCallOutput,
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
    Program,
    ProgramOutput,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from .. import _debug
from .._mcp_tool_metadata import collect_mcp_list_tools_metadata
from .._tool_identity import (
    FunctionToolLookupKey,
    build_function_tool_lookup_map,
    get_function_tool_lookup_key,
    get_function_tool_lookup_key_for_call,
    get_function_tool_lookup_key_for_tool,
    get_tool_approval_item_call_id,
    get_tool_call_namespace,
    get_tool_call_qualified_name,
    get_tool_call_trace_name,
    resolve_tool_name_collisions,
    restore_tool_call_routing_identity,
    should_allow_bare_name_approval_alias,
)
from .._tool_invocation import tool_invocation_call_id, tool_invocation_identity_and_scope
from ..agent import Agent, ToolsToFinalOutputResult
from ..agent_output import AgentOutputSchemaBase
from ..agent_tool_state import (
    agent_tool_resume_checkpoint_owns_approval,
    drop_agent_tool_run_result,
    get_agent_tool_resume_state,
    get_agent_tool_state_scope,
    peek_agent_tool_run_result,
    record_agent_tool_run_result,
)
from ..exceptions import (
    _DATA_REDACTED_ERROR_MESSAGE,
    ModelBehaviorError,
    ModelRefusalError,
    UserError,
    _detach_data_redacted_error_traceback,
    _is_error_data_redacted,
    _mark_error_data_redacted,
)
from ..handoffs import Handoff, HandoffInputData, HandoffInputFilter, nest_handoff_history
from ..handoffs.history import (
    _get_nested_history_owned_items,
    _nest_handoff_history_with_provenance,
)
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
    ToolSearchCallItem,
    ToolSearchOutputItem,
    TResponseInputItem,
    coerce_tool_search_call_raw_item,
    coerce_tool_search_output_raw_item,
)
from ..lifecycle import RunHooks
from ..logger import log_tool_action_error, logger
from ..run_config import RunConfig, ToolErrorFormatterArgs
from ..run_context import AgentHookContext, RunContextWrapper, TContext
from ..run_error_handlers import RunErrorHandlers
from ..run_state import RunState
from ..tool import (
    ApplyPatchTool,
    CodeInterpreterTool,
    ComputerTool,
    CustomTool,
    FunctionTool,
    FunctionToolResult,
    HostedMCPTool,
    LocalShellTool,
    ProgrammaticToolCallingTool,
    ShellTool,
    Tool,
    ToolOrigin,
    ToolOriginType,
    get_function_tool_origin,
)
from ..tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from ..tracing import SpanError, handoff_span
from ..util import _coro, _error_tracing
from ..util._approvals import evaluate_needs_approval_setting
from ..util._asyncio_tasks import gather_with_cancel
from .agent_bindings import AgentBindings
from .error_handlers import (
    build_run_error_data,
    create_message_output_item,
    format_final_output_text,
    resolve_run_error_handler_result,
    validate_handler_final_output,
)
from .items import (
    REJECTION_MESSAGE,
    NestedHistoryOwnedItem,
    apply_patch_rejection_item,
    extract_mcp_request_id_from_run,
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
    SingleStepResult,
    ToolRunApplyPatchCall,
    ToolRunComputerAction,
    ToolRunCustom,
    ToolRunFunction,
    ToolRunFunctionNotFound,
    ToolRunHandoff,
    ToolRunLocalShellCall,
    ToolRunMCPApprovalRequest,
    ToolRunShellCall,
)
from .tool_caller import ensure_programmatic_tool_call_parent, ensure_tool_caller_allowed
from .tool_execution import (
    build_litellm_json_tool_call,
    coerce_apply_patch_operations,
    coerce_shell_call,
    extract_apply_patch_call_id,
    extract_shell_call_id,
    extract_tool_call_id,
    function_needs_approval,
    get_mapping_or_attr,
    index_approval_items_by_call_id,
    is_apply_patch_name,
    normalize_apply_patch_fallback_call,
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
    _dedupe_processed_response_invocations,
    _dedupe_tool_call_items,
    _execute_tool_plan,
    _make_unique_item_appender,
    _register_tool_call_items,
    _select_function_tool_runs_for_resume,
    _validate_unresolved_function_calls,
)
from .turn_preparation import get_handoffs, get_output_schema

_DEFAULT_NEST_HANDOFF_HISTORY = nest_handoff_history

__all__ = [
    "execute_final_output_step",
    "execute_final_output",
    "execute_handoffs",
    "check_for_final_output_from_tools",
    "is_handoff_tool_call",
    "process_model_response",
    "execute_tools_and_side_effects",
    "resolve_interrupted_turn",
    "get_single_step_result_from_response",
    "run_final_output_hooks",
]


def is_handoff_tool_call(output: Any, handoff_tool_names: Container[str]) -> bool:
    """Return whether a model output item routes to a handoff instead of a tool.

    Namespaced calls never resolve to a handoff, so only bare names are matched.
    """
    if not isinstance(output, ResponseFunctionToolCall):
        return False
    return get_tool_call_qualified_name(output) == output.name and output.name in handoff_tool_names


async def _maybe_finalize_from_tool_results(
    *,
    public_agent: Agent[TContext],
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
        public_agent, function_results, context_wrapper
    )
    if not check_tool_use.is_final_output:
        return None

    if public_agent.output_type is None or public_agent.output_type is str:
        check_tool_use.final_output = str(check_tool_use.final_output)

    if check_tool_use.final_output is None:
        logger.error(
            "Model returned a final output of None. Not raising an error because we assume"
            "you know what you're doing."
        )

    return await execute_final_output(
        public_agent=public_agent,
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


def _default_tool_not_found_message(tool_name: str) -> str:
    return f"Tool '{tool_name}' not found."


async def _resolve_tool_not_found_message(
    *,
    context_wrapper: RunContextWrapper[Any],
    run_config: RunConfig,
    tool_call: ResponseFunctionToolCall,
    tool_name: str,
    call_id: str,
) -> str:
    default_message = _default_tool_not_found_message(tool_name)
    formatter = run_config.tool_error_formatter
    if formatter is None:
        return default_message

    context_wrapper._mark_tool_invocation_executed(tool_call)
    try:
        maybe_message = formatter(
            ToolErrorFormatterArgs(
                kind="tool_not_found",
                tool_type="function",
                tool_name=tool_name,
                call_id=call_id,
                default_message=default_message,
                run_context=context_wrapper,
            )
        )
        message = await maybe_message if inspect.isawaitable(maybe_message) else maybe_message
    except Exception as exc:
        log_tool_action_error(logger, "Tool error formatter failed for missing tool", exc)
        return default_message

    if message is None:
        return default_message

    if not isinstance(message, str):
        if _debug.DONT_LOG_TOOL_DATA:
            logger.error("Tool error formatter returned a non-string value for a missing tool")
        else:
            logger.error(
                "Tool error formatter returned non-string for missing tool %s: %s",
                tool_name,
                type(message).__name__,
            )
        return default_message

    return message


async def _build_tool_not_found_output_items(
    *,
    agent: Agent[Any],
    calls: Sequence[ToolRunFunctionNotFound],
    context_wrapper: RunContextWrapper[Any],
    run_config: RunConfig,
) -> list[RunItem]:
    items: list[RunItem] = []
    for call in calls:
        message = await _resolve_tool_not_found_message(
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_call=call.tool_call,
            tool_name=call.tool_name,
            call_id=call.tool_call.call_id,
        )
        items.append(
            ToolCallOutputItem(
                output=message,
                raw_item=ItemHelpers.tool_call_output_item(call.tool_call, message),
                agent=agent,
            )
        )
    return items


async def run_final_output_hooks(
    agent: Agent[TContext],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    final_output: Any,
) -> None:
    agent_hook_context = AgentHookContext(
        context=context_wrapper.context,
        usage=context_wrapper.usage,
        turn_input=context_wrapper.turn_input,
    )
    context_wrapper._share_tool_state_with(agent_hook_context)

    await gather_with_cancel(
        hooks.on_agent_end(agent_hook_context, agent, final_output),
        agent.hooks.on_end(agent_hook_context, agent, final_output)
        if agent.hooks is not None
        else _coro.noop_coroutine(),
    )


async def execute_final_output_step(
    *,
    public_agent: Agent[Any],
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
    final_output_hooks = (
        run_final_output_hooks_fn
        if run_final_output_hooks_fn is not None
        else run_final_output_hooks
    )
    await final_output_hooks(public_agent, hooks, context_wrapper, final_output)

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
    public_agent: Agent[Any],
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
        public_agent=public_agent,
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


async def _resolve_invalid_final_output(
    *,
    error_handlers: RunErrorHandlers[TContext] | None,
    error: ModelBehaviorError,
    public_agent: Agent[TContext],
    original_input: str | list[TResponseInputItem],
    new_response: ModelResponse,
    new_items: list[RunItem],
    context_wrapper: RunContextWrapper[TContext],
) -> tuple[Any, MessageOutputItem | None] | None:
    redacted = _is_error_data_redacted(error) or _debug.DONT_LOG_MODEL_DATA
    run_error_data = build_run_error_data(
        input=original_input,
        new_items=new_items,
        raw_responses=[new_response],
        last_agent=public_agent,
    )
    _detach_data_redacted_error_traceback(error)
    safe_error: UserError | None = None
    try:
        handler_result = await resolve_run_error_handler_result(
            error_handlers=error_handlers,
            error_kind="invalid_final_output",
            error=error,
            context_wrapper=context_wrapper,
            run_data=run_error_data,
        )
        if handler_result is None:
            return None

        final_output = validate_handler_final_output(
            public_agent,
            handler_result.final_output,
            data_redacted=redacted,
        )
        message_item = (
            create_message_output_item(
                public_agent,
                format_final_output_text(
                    public_agent,
                    final_output,
                    data_redacted=redacted,
                ),
            )
            if handler_result.include_in_history
            else None
        )
        return final_output, message_item
    except Exception:
        if not redacted:
            raise
        safe_error = UserError(_DATA_REDACTED_ERROR_MESSAGE)
        _mark_error_data_redacted(safe_error)

    error = cast(Any, None)
    error_handlers = cast(Any, None)
    public_agent = cast(Any, None)
    original_input = cast(Any, None)
    new_response = cast(Any, None)
    new_items = cast(Any, None)
    context_wrapper = cast(Any, None)
    run_error_data = cast(Any, None)
    handler_result = cast(Any, None)
    final_output = cast(Any, None)
    message_item = cast(Any, None)
    assert safe_error is not None
    raise safe_error from None


def _resolve_server_managed_handoff_behavior(
    *,
    handoff: Handoff[Any, Agent[Any]],
    from_agent: Agent[Any],
    to_agent: Agent[Any],
    run_config: RunConfig,
    server_manages_conversation: bool,
    input_filter: HandoffInputFilter | None,
    should_nest_history: bool,
) -> tuple[HandoffInputFilter | None, bool]:
    if not server_manages_conversation:
        return input_filter, should_nest_history

    if input_filter is not None:
        raise UserError(
            "Server-managed conversations do not support handoff input filters. "
            "Remove Handoff.input_filter or RunConfig.handoff_input_filter, "
            "or disable conversation_id, previous_response_id, and auto_previous_response_id."
        )

    if not should_nest_history:
        return input_filter, should_nest_history

    logger.warning(
        "Server-managed conversations do not support nest_handoff_history for handoff "
        "%s -> %s. Disabling nested handoff history and continuing with delta-only input.",
        from_agent.name,
        to_agent.name,
    )
    return input_filter, False


async def execute_handoffs(
    *,
    public_agent: Agent[TContext],
    original_input: str | list[TResponseInputItem],
    pre_step_items: list[RunItem],
    new_step_items: list[RunItem],
    new_response: ModelResponse,
    run_handoffs: list[ToolRunHandoff],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    server_manages_conversation: bool = False,
    nest_handoff_history_fn: Callable[..., HandoffInputData] | None = None,
    tool_input_guardrail_results: list[ToolInputGuardrailResult] | None = None,
    tool_output_guardrail_results: list[ToolOutputGuardrailResult] | None = None,
    handoff_output_committer: Callable[[HandoffOutputItem, Agent[Any]], None] | None = None,
) -> SingleStepResult:
    """Execute a handoff and prepare the next turn for the new agent."""

    def nest_history(
        data: HandoffInputData,
        mapper: Any | None = None,
    ) -> tuple[HandoffInputData, list[NestedHistoryOwnedItem]]:
        if (
            nest_handoff_history_fn is None
            and nest_handoff_history is _DEFAULT_NEST_HANDOFF_HISTORY
        ):
            nested, history_owned_items = _nest_handoff_history_with_provenance(
                data,
                history_mapper=mapper,
            )
            return nested, list(history_owned_items)
        if nest_handoff_history_fn is not None:
            return nest_handoff_history_fn(data, mapper), []
        return nest_handoff_history(data, history_mapper=mapper), []

    multiple_handoffs = len(run_handoffs) > 1
    if multiple_handoffs:
        output_message = "Multiple handoffs detected, ignoring this one."
        new_step_items.extend(
            [
                ToolCallOutputItem(
                    output=output_message,
                    raw_item=ItemHelpers.tool_call_output_item(handoff.tool_call, output_message),
                    agent=public_agent,
                )
                for handoff in run_handoffs[1:]
            ]
        )

    actual_handoff = run_handoffs[0]
    with handoff_span(from_agent=public_agent.name) as span_handoff:
        handoff = actual_handoff.handoff
        context_wrapper._mark_tool_invocation_executed(
            actual_handoff.tool_call,
            invocation_role="handoff",
        )
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

        handoff_output = HandoffOutputItem(
            agent=public_agent,
            raw_item=ItemHelpers.tool_call_output_item(
                actual_handoff.tool_call,
                handoff.get_transfer_message(new_agent),
            ),
            source_agent=public_agent,
            target_agent=new_agent,
        )
        new_step_items.append(handoff_output)
        if handoff_output_committer is not None:
            _register_tool_call_items(context_wrapper, [handoff_output])
            handoff_output_committer(handoff_output, new_agent)

        await gather_with_cancel(
            hooks.on_handoff(
                context=context_wrapper,
                from_agent=public_agent,
                to_agent=new_agent,
            ),
            (
                public_agent.hooks.on_handoff(
                    context_wrapper,
                    agent=new_agent,
                    source=public_agent,
                )
                if public_agent.hooks is not None
                else _coro.noop_coroutine()
            ),
        )

        input_filter = (
            handoff.input_filter
            if handoff.input_filter is not None
            else run_config.handoff_input_filter
        )
        handoff_nest_setting = handoff.nest_handoff_history
        should_nest_history = (
            handoff_nest_setting
            if handoff_nest_setting is not None
            else run_config.nest_handoff_history
        )
        input_filter, should_nest_history = _resolve_server_managed_handoff_behavior(
            handoff=handoff,
            from_agent=public_agent,
            to_agent=new_agent,
            run_config=run_config,
            server_manages_conversation=server_manages_conversation,
            input_filter=input_filter,
            should_nest_history=should_nest_history,
        )
        handoff_input_data: HandoffInputData | None = None
        session_step_items: list[RunItem] | None = None
        nested_history_owned_items: list[NestedHistoryOwnedItem] | None = None
        if input_filter is not None or should_nest_history:
            handoff_input_data = HandoffInputData(
                input_history=tuple(original_input)
                if isinstance(original_input, list)
                else original_input,
                pre_handoff_items=tuple(pre_step_items),
                new_items=tuple(new_step_items),
                run_context=context_wrapper,
            )

        if input_filter is not None and handoff_input_data is not None:
            filter_name = getattr(input_filter, "__qualname__", repr(input_filter))
            from_agent = getattr(public_agent, "name", public_agent.__class__.__name__)
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
            nested_history_owned_items = list(
                _get_nested_history_owned_items(filtered, source_data=handoff_input_data)
            )
        elif should_nest_history and handoff_input_data is not None:
            nested, nested_history_owned_items = nest_history(
                handoff_input_data,
                run_config.handoff_history_mapper,
            )
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

        if handoff_output_committer is None and (
            handoff_output in new_step_items
            or (session_step_items is not None and handoff_output in session_step_items)
        ):
            _register_tool_call_items(context_wrapper, [handoff_output])

    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        next_step=NextStepHandoff(new_agent),
        tool_input_guardrail_results=list(tool_input_guardrail_results or []),
        tool_output_guardrail_results=list(tool_output_guardrail_results or []),
        session_step_items=session_step_items,
        nested_history_owned_items=nested_history_owned_items,
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
            if tool_result.tool.name in names or tool_result.tool.qualified_name in names:
                return ToolsToFinalOutputResult(
                    is_final_output=True, final_output=tool_result.output
                )
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
    elif callable(agent.tool_use_behavior):
        result = agent.tool_use_behavior(context_wrapper, tool_results)
        if inspect.isawaitable(result):
            return await result
        return result

    logger.error("Invalid tool_use_behavior: %s", agent.tool_use_behavior)
    raise UserError(f"Invalid tool_use_behavior: {agent.tool_use_behavior}")


async def execute_tools_and_side_effects(
    *,
    bindings: AgentBindings[TContext],
    original_input: str | list[TResponseInputItem],
    pre_step_items: list[RunItem],
    new_response: ModelResponse,
    processed_response: ProcessedResponse,
    output_schema: AgentOutputSchemaBase | None,
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    error_handlers: RunErrorHandlers[TContext] | None = None,
    server_manages_conversation: bool = False,
    precomputed_skipped_raw_item_ids: set[int] | None = None,
    run_state: RunState[Any] | None = None,
) -> SingleStepResult:
    """Run one turn of the loop, coordinating tools, approvals, guardrails, and handoffs."""
    public_agent = bindings.public_agent

    execute_final_output_call = execute_final_output
    execute_handoffs_call = execute_handoffs

    pre_step_items = list(pre_step_items)
    _register_tool_call_items(
        context_wrapper,
        pre_step_items,
        validate_invocations=False,
    )
    skipped_raw_item_ids = (
        precomputed_skipped_raw_item_ids
        if precomputed_skipped_raw_item_ids is not None
        else _dedupe_processed_response_invocations(
            processed_response,
            context_wrapper=context_wrapper,
            existing_items=pre_step_items,
        )
    )
    _register_tool_call_items(context_wrapper, processed_response.new_items)
    _validate_unresolved_function_calls(
        context_wrapper,
        processed_response.function_tools_not_found,
    )
    approval_items_by_call_id = index_approval_items_by_call_id(pre_step_items)

    plan = _build_plan_for_fresh_turn(
        processed_response=processed_response,
        agent=public_agent,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
    )
    new_step_items = _dedupe_tool_call_items(
        existing_items=pre_step_items,
        new_items=processed_response.new_items,
        skipped_raw_item_ids=skipped_raw_item_ids,
    )

    def _commit_accepted_response_tool_output(item: RunItem) -> None:
        if run_state is None or not isinstance(run_state._current_step, NextStepInterruption):
            return
        if not run_state._current_step.response_accepted:
            return
        for target in (run_state._generated_items, run_state._session_items):
            if item not in target:
                target.append(item)

    (
        function_results,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        computer_results,
        custom_tool_results,
        shell_results,
        apply_patch_results,
        local_shell_results,
    ) = await _execute_tool_plan(
        plan=plan,
        bindings=bindings,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
        tool_output_committer=_commit_accepted_response_tool_output,
    )
    new_step_items.extend(
        _build_tool_result_items(
            function_results=function_results,
            computer_results=computer_results,
            custom_tool_results=custom_tool_results,
            shell_results=shell_results,
            apply_patch_results=apply_patch_results,
            local_shell_results=local_shell_results,
        )
    )
    new_step_items.extend(
        await _build_tool_not_found_output_items(
            agent=public_agent,
            calls=processed_response.function_tools_not_found,
            context_wrapper=context_wrapper,
            run_config=run_config,
        )
    )

    interruptions = _collect_tool_interruptions(
        function_results=function_results,
        custom_tool_results=custom_tool_results,
        shell_results=shell_results,
        apply_patch_results=apply_patch_results,
    )
    if plan.approved_mcp_responses:
        new_step_items.extend(plan.approved_mcp_responses)
    if plan.pending_interruptions:
        interruptions.extend(plan.pending_interruptions)
        new_step_items.extend(plan.pending_interruptions)

    _register_tool_call_items(context_wrapper, new_step_items)

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
        agent=public_agent,
        requests=plan.mcp_requests_with_callback,
        context_wrapper=context_wrapper,
        append_item=new_step_items.append,
    )
    _register_tool_call_items(context_wrapper, new_step_items)

    if run_handoffs := processed_response.handoffs:
        return await execute_handoffs_call(
            public_agent=public_agent,
            original_input=original_input,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            new_response=new_response,
            run_handoffs=run_handoffs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
            server_manages_conversation=server_manages_conversation,
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )

    tool_final_output = await _maybe_finalize_from_tool_results(
        public_agent=public_agent,
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
    refusal = ItemHelpers.extract_refusal(message_items[-1].raw_item) if message_items else None
    potential_final_output_text = (
        ItemHelpers.extract_text(message_items[-1].raw_item) if message_items else None
    )

    if not processed_response.has_tools_or_approvals_to_run():
        has_tool_activity_without_message = not message_items and bool(
            processed_response.tools_used or skipped_raw_item_ids
        )
        if not has_tool_activity_without_message:
            if refusal:
                refusal_error = ModelRefusalError(refusal)
                run_error_data = build_run_error_data(
                    input=original_input,
                    new_items=pre_step_items + new_step_items,
                    raw_responses=[new_response],
                    last_agent=public_agent,
                )
                handler_result = await resolve_run_error_handler_result(
                    error_handlers=error_handlers,
                    error_kind="model_refusal",
                    error=refusal_error,
                    context_wrapper=context_wrapper,
                    run_data=run_error_data,
                )
                if handler_result is None:
                    raise refusal_error

                final_output = validate_handler_final_output(
                    public_agent, handler_result.final_output
                )
                if handler_result.include_in_history:
                    output_text = format_final_output_text(public_agent, final_output)
                    new_step_items.append(create_message_output_item(public_agent, output_text))
                return await execute_final_output_call(
                    public_agent=public_agent,
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
            if output_schema is not None and not output_schema.is_plain_text():
                if potential_final_output_text:
                    validation_error: ModelBehaviorError | None = None
                    try:
                        final_output = output_schema.validate_json(potential_final_output_text)
                    except ModelBehaviorError as error:
                        if _is_error_data_redacted(error):
                            validation_error = error
                        else:
                            resolved_handler_output = await _resolve_invalid_final_output(
                                error_handlers=error_handlers,
                                error=error,
                                public_agent=public_agent,
                                original_input=original_input,
                                new_response=new_response,
                                new_items=pre_step_items + new_step_items,
                                context_wrapper=context_wrapper,
                            )
                            if resolved_handler_output is None:
                                raise
                            final_output, message_item = resolved_handler_output
                            if message_item is not None:
                                new_step_items.append(message_item)

                    if validation_error is not None:
                        resolved_handler_output = await _resolve_invalid_final_output(
                            error_handlers=error_handlers,
                            error=validation_error,
                            public_agent=public_agent,
                            original_input=original_input,
                            new_response=new_response,
                            new_items=pre_step_items + new_step_items,
                            context_wrapper=context_wrapper,
                        )
                        if resolved_handler_output is None:
                            raise validation_error
                        final_output, message_item = resolved_handler_output
                        if message_item is not None:
                            new_step_items.append(message_item)
                else:
                    resolved_handler_output = await _resolve_invalid_final_output(
                        error_handlers=error_handlers,
                        error=ModelBehaviorError(
                            "Model returned no final output for the structured output type."
                        ),
                        public_agent=public_agent,
                        original_input=original_input,
                        new_response=new_response,
                        new_items=pre_step_items + new_step_items,
                        context_wrapper=context_wrapper,
                    )
                    if resolved_handler_output is None:
                        return SingleStepResult(
                            original_input=original_input,
                            model_response=new_response,
                            pre_step_items=pre_step_items,
                            new_step_items=new_step_items,
                            next_step=NextStepRunAgain(),
                            tool_input_guardrail_results=tool_input_guardrail_results,
                            tool_output_guardrail_results=tool_output_guardrail_results,
                        )
                    final_output, message_item = resolved_handler_output
                    if message_item is not None:
                        new_step_items.append(message_item)

                return await execute_final_output_call(
                    public_agent=public_agent,
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
            if output_schema is None or output_schema.is_plain_text():
                return await execute_final_output_call(
                    public_agent=public_agent,
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


def _collect_program_parent_state(
    items: Sequence[Any],
    *,
    server_manages_conversation: bool = False,
) -> tuple[set[str], set[str]]:
    """Collect retained program parents and completed program outputs."""
    program_call_ids: set[str] = set()
    completed_program_call_ids: set[str] = set()
    for item in items:
        raw_item = getattr(item, "raw_item", item)
        item_type = get_mapping_or_attr(raw_item, "type")
        call_id = get_mapping_or_attr(raw_item, "call_id")
        if not isinstance(call_id, str) or not call_id:
            call_id = None
        if item_type == "program" and call_id is not None:
            program_call_ids.add(call_id)
        elif item_type == "program_output" and call_id is not None:
            if server_manages_conversation:
                program_call_ids.add(call_id)
            if get_mapping_or_attr(raw_item, "status") == "completed":
                completed_program_call_ids.add(call_id)
        if server_manages_conversation:
            caller = get_mapping_or_attr(raw_item, "caller")
            caller_id = get_mapping_or_attr(caller, "caller_id")
            if (
                get_mapping_or_attr(caller, "type") == "program"
                and isinstance(caller_id, str)
                and caller_id
            ):
                program_call_ids.add(caller_id)
    return program_call_ids, completed_program_call_ids


async def resolve_interrupted_turn(
    *,
    bindings: AgentBindings[TContext],
    original_input: str | list[TResponseInputItem],
    original_pre_step_items: list[RunItem],
    new_response: ModelResponse,
    processed_response: ProcessedResponse,
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    server_manages_conversation: bool = False,
    run_state: RunState | None = None,
    error_handlers: RunErrorHandlers[TContext] | None = None,
    nest_handoff_history_fn: Callable[..., HandoffInputData] | None = None,
) -> SingleStepResult:
    """Continue a turn that was previously interrupted waiting for tool approval."""
    public_agent = bindings.public_agent
    execution_agent = bindings.execution_agent

    current_step = run_state._current_step if run_state is not None else None
    if (
        isinstance(current_step, NextStepInterruption)
        and current_step.response_accepted
        and not current_step.llm_end_hooks_started
    ):
        current_step.llm_end_hooks_started = True
        await gather_with_cancel(
            (
                public_agent.hooks.on_llm_end(context_wrapper, public_agent, new_response)
                if public_agent.hooks is not None
                else _coro.noop_coroutine()
            ),
            hooks.on_llm_end(context_wrapper, public_agent, new_response),
        )

    if (
        isinstance(current_step, NextStepInterruption)
        and current_step.response_accepted
        and not processed_response.has_tools_or_approvals_to_run()
    ):
        return await execute_tools_and_side_effects(
            bindings=bindings,
            original_input=original_input,
            pre_step_items=original_pre_step_items,
            new_response=new_response,
            processed_response=processed_response,
            output_schema=get_output_schema(execution_agent),
            hooks=hooks,
            context_wrapper=context_wrapper,
            run_config=run_config,
            error_handlers=error_handlers,
            server_manages_conversation=server_manages_conversation,
            run_state=run_state,
        )

    execute_handoffs_call = execute_handoffs

    _register_tool_call_items(
        context_wrapper,
        original_pre_step_items,
        validate_invocations=False,
    )

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
            tool_namespace = get_tool_call_namespace(tool_call)
            rejection_message = await resolve_approval_rejection_message(
                context_wrapper=context_wrapper,
                run_config=run_config,
                tool_call=tool_call,
                tool_type="function",
                tool_name=get_tool_call_trace_name(tool_call) or function_tool.name,
                call_id=call_id,
                tool_namespace=tool_namespace,
                tool_lookup_key=get_function_tool_lookup_key_for_tool(function_tool),
                existing_pending=(
                    function_approval_items_by_call_id.get(call_id)
                    or approval_items_by_call_id.get(call_id)
                ),
            )
        rejected_function_outputs.append(
            function_rejection_item(
                public_agent,
                tool_call,
                rejection_message=rejection_message,
                output_json_schema=function_tool.output_json_schema,
                scope_id=tool_state_scope_id,
                tool_origin=get_function_tool_origin(function_tool),
            )
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

    def _allow_legacy_name_agent_match() -> bool:
        schema_version = getattr(run_state, "_schema_version", None)
        if not isinstance(schema_version, str):
            return False
        try:
            version_parts = tuple(int(part) for part in schema_version.split("."))
        except ValueError:
            return False
        # Schema 1.6 and earlier only serialized approval owners by agent name. With duplicate-name
        # agents, deserialization can legitimately resolve the approval to a sibling instance, so
        # resume must accept a same-name match for those legacy snapshots. Schema 1.7+ persists
        # duplicate-name identities, so newer snapshots should continue requiring object identity.
        return version_parts < (1, 7)

    allow_legacy_name_agent_match = _allow_legacy_name_agent_match()

    def _approval_matches_agent(approval: ToolApprovalItem) -> bool:
        approval_agent = approval.agent
        if approval_agent is None:
            return False
        if approval_agent is public_agent:
            return True
        return allow_legacy_name_agent_match and approval_agent.name == public_agent.name

    approval_items_by_call_id = index_approval_items_by_call_id(
        [item for item in pending_approval_items if _approval_matches_agent(item)]
    )
    tool_state_scope_id = get_agent_tool_state_scope(context_wrapper)

    rejected_function_outputs: list[RunItem] = []
    rejected_function_call_ids: set[str] = set()
    rerun_function_call_ids: set[str] = set()
    pending_interruptions: list[ToolApprovalItem] = []
    pending_interruption_keys: set[str] = set()
    stable_function_approval_sources: dict[int, ToolApprovalItem] = {}

    output_index = _build_tool_output_index(original_pre_step_items)

    def _has_output_item(call_id: str, expected_type: str) -> bool:
        return (expected_type, call_id) in output_index

    def _shell_call_id_from_run(run: ToolRunShellCall) -> str:
        return extract_shell_call_id(run.tool_call)

    def _apply_patch_call_id_from_run(run: ToolRunApplyPatchCall) -> str:
        return extract_apply_patch_call_id(run.tool_call)

    def _custom_call_id_from_run(run: ToolRunCustom) -> str:
        call_id = extract_tool_call_id(run.tool_call)
        if not call_id:
            raise ModelBehaviorError("Custom tool call is missing call_id.")
        return call_id

    def _computer_call_id_from_run(run: ToolRunComputerAction) -> str:
        call_id = extract_tool_call_id(run.tool_call)
        if not call_id:
            raise ModelBehaviorError("Computer action is missing call_id.")
        return call_id

    def _shell_tool_name(run: ToolRunShellCall) -> str:
        return run.shell_tool.name

    def _apply_patch_tool_name(run: ToolRunApplyPatchCall) -> str:
        return run.apply_patch_tool.name

    def _custom_tool_name(run: ToolRunCustom) -> str:
        return run.custom_tool.name

    async def _build_shell_rejection(run: ToolRunShellCall, call_id: str) -> RunItem:
        rejection_message = await resolve_approval_rejection_message(
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_call=run.tool_call,
            tool_type="shell",
            tool_name=run.shell_tool.name,
            call_id=call_id,
        )
        return cast(
            RunItem,
            shell_rejection_item(
                public_agent,
                call_id,
                tool_call=run.tool_call,
                rejection_message=rejection_message,
            ),
        )

    async def _build_apply_patch_rejection(run: ToolRunApplyPatchCall, call_id: str) -> RunItem:
        rejection_message = await resolve_approval_rejection_message(
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_call=run.tool_call,
            tool_type="apply_patch",
            tool_name=run.apply_patch_tool.name,
            call_id=call_id,
        )
        return cast(
            RunItem,
            apply_patch_rejection_item(
                public_agent,
                call_id,
                tool_call=run.tool_call,
                output_type="apply_patch_call_output",
                rejection_message=rejection_message,
            ),
        )

    async def _build_custom_rejection(run: ToolRunCustom, call_id: str) -> RunItem:
        rejection_message = await resolve_approval_rejection_message(
            context_wrapper=context_wrapper,
            run_config=run_config,
            tool_call=run.tool_call,
            tool_type="custom",
            tool_name=run.custom_tool.name,
            call_id=call_id,
        )
        raw_item = {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": rejection_message,
        }
        ItemHelpers.copy_tool_call_caller(run.tool_call, raw_item)
        return ToolCallOutputItem(
            agent=public_agent,
            output=rejection_message,
            raw_item=cast(Any, raw_item),
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
        operations = coerce_apply_patch_operations(
            run.tool_call,
            context_wrapper=context_wrapper,
        )
        call_id = extract_apply_patch_call_id(run.tool_call)
        for operation in operations:
            needs_approval = await evaluate_needs_approval_setting(
                run.apply_patch_tool.needs_approval, context_wrapper, operation, call_id
            )
            approval_status = context_wrapper.get_approval_status(
                run.apply_patch_tool.name,
                call_id,
                existing_pending=approval_items_by_call_id.get(call_id),
            )
            if approval_status is not None or needs_approval:
                return needs_approval
        return False

    async def _custom_tool_needs_approval(run: ToolRunCustom) -> bool:
        tool_input = get_mapping_or_attr(run.tool_call, "input")
        call_id = _custom_call_id_from_run(run)
        if not isinstance(tool_input, str):
            raise ModelBehaviorError("Custom tool call is missing input.")
        return await evaluate_needs_approval_setting(
            run.custom_tool.runtime_needs_approval(),
            context_wrapper,
            tool_input,
            call_id,
        )

    def _shell_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "shell_call_output")

    def _apply_patch_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "apply_patch_call_output")

    def _custom_tool_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "custom_tool_call_output")

    def _computer_output_exists(call_id: str) -> bool:
        return _has_output_item(call_id, "computer_call_output")

    def _nested_interruptions_status(
        nested_run_result: Any,
    ) -> Literal["approved", "pending", "rejected"]:
        interruptions = cast(Sequence[ToolApprovalItem], nested_run_result.interruptions)
        nested_state = nested_run_result.to_state()
        nested_decision_context = getattr(nested_state, "_context", None)
        has_pending = False
        for interruption in interruptions:
            call_id = get_tool_approval_item_call_id(interruption)
            if not call_id:
                has_pending = True
                continue
            status = (
                nested_decision_context.get_approval_status(
                    interruption.tool_name or "",
                    call_id,
                    tool_namespace=interruption.tool_namespace,
                    existing_pending=interruption,
                )
                if isinstance(nested_decision_context, RunContextWrapper)
                else None
            )
            if status is None and context_wrapper._allow_legacy_approval_binding_reconstruction:
                status = context_wrapper.get_approval_status(
                    interruption.tool_name or "",
                    call_id,
                    tool_namespace=interruption.tool_namespace,
                    existing_pending=interruption,
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

        if call_id not in approval_items_by_call_id and _has_output_item(
            call_id, "function_call_output"
        ):
            return True

        pending_run_result = peek_agent_tool_run_result(
            run.tool_call,
            scope_id=tool_state_scope_id,
        )
        if pending_run_result and getattr(pending_run_result, "interruptions", None):
            status = _nested_interruptions_status(pending_run_result)
            if status in ("approved", "rejected"):
                rerun_function_call_ids.add(call_id)
                return False
            return True

        binding_status = context_wrapper._approved_tool_invocation_status(
            run.tool_call,
            tool_lookup_key=get_function_tool_lookup_key_for_tool(run.function_tool),
        )
        if binding_status is not None:
            return binding_status[1]

        return False

    def _add_pending_interruption(item: ToolApprovalItem | None) -> None:
        if item is None:
            return
        source_item = stable_function_approval_sources.get(id(item))
        if source_item is not None:
            source_item.agent = item.agent
            source_item.raw_item = cast(
                ResponseFunctionToolCall,
                item.raw_item,
            ).model_copy(deep=True)
            source_item.tool_name = item.tool_name
            source_item.tool_namespace = item.tool_namespace
            source_item.tool_origin = item.tool_origin
            source_item.tool_lookup_key = item.tool_lookup_key
            source_item._allow_bare_name_alias = item._allow_bare_name_alias
            item = source_item
        call_id = get_tool_approval_item_call_id(item)
        key = call_id or f"raw:{id(item.raw_item)}"
        if key in pending_interruption_keys:
            return
        pending_interruption_keys.add(key)
        pending_interruptions.append(item)

    def _approval_persisted_lookup_key(
        approval: ToolApprovalItem,
    ) -> FunctionToolLookupKey | None:
        persisted_key = approval.tool_lookup_key
        if persisted_key is not None:
            return persisted_key
        if not approval.tool_name:
            return None
        return get_function_tool_lookup_key(
            approval.tool_name,
            approval.tool_namespace,
        )

    deferred_binding_validation_raw_item_ids = {
        id(run.tool_call)
        for run in processed_response.functions
        if (
            (
                nested_result := peek_agent_tool_run_result(
                    run.tool_call,
                    scope_id=tool_state_scope_id,
                )
            )
            is not None
            and (
                getattr(nested_result, "interruptions", None)
                or get_agent_tool_resume_state(nested_result) is not None
            )
        )
    }
    for function_run in processed_response.functions:
        if id(function_run.tool_call) not in deferred_binding_validation_raw_item_ids:
            continue
        approval_item = approval_items_by_call_id.get(function_run.tool_call.call_id)
        persisted_lookup_key = (
            _approval_persisted_lookup_key(approval_item)
            if approval_item is not None
            else get_function_tool_lookup_key_for_tool(function_run.function_tool)
        )
        current_lookup_key = get_function_tool_lookup_key_for_tool(function_run.function_tool)
        if persisted_lookup_key != current_lookup_key:
            # Preserve the more specific interrupted Agent.as_tool() replacement error below.
            continue
        context_wrapper._approved_tool_invocation_status(
            function_run.tool_call,
            tool_lookup_key=persisted_lookup_key,
        )
    _dedupe_processed_response_invocations(
        processed_response,
        context_wrapper=context_wrapper,
        existing_items=original_pre_step_items,
        deferred_binding_validation_raw_item_ids=deferred_binding_validation_raw_item_ids,
        filter_completed=False,
    )

    queued_call_id_counts: dict[str, int] = {}
    queued_call_items = [
        *(run.tool_call for run in processed_response.functions),
        *(run.tool_call for run in processed_response.handoffs),
        *(run.tool_call for run in processed_response.computer_actions),
        *(run.tool_call for run in processed_response.custom_tool_calls),
        *(run.tool_call for run in processed_response.local_shell_calls),
        *(run.tool_call for run in processed_response.shell_calls),
        *(run.tool_call for run in processed_response.apply_patch_calls),
        *(run.request_item for run in processed_response.mcp_approval_requests),
        *(run.tool_call for run in processed_response.function_tools_not_found),
    ]
    for queued_call_item in queued_call_items:
        queued_call_id = extract_tool_call_id(queued_call_item)
        if queued_call_id is not None:
            queued_call_id_counts[queued_call_id] = queued_call_id_counts.get(queued_call_id, 0) + 1
    duplicate_queued_call_ids = {
        call_id for call_id, count in queued_call_id_counts.items() if count > 1
    }
    non_function_owned_call_ids = {
        call_id
        for call_item in [
            *(run.tool_call for run in processed_response.computer_actions),
            *(run.tool_call for run in processed_response.custom_tool_calls),
            *(run.tool_call for run in processed_response.local_shell_calls),
            *(run.tool_call for run in processed_response.shell_calls),
            *(run.tool_call for run in processed_response.apply_patch_calls),
            *(run.request_item for run in processed_response.mcp_approval_requests),
        ]
        if (call_id := extract_tool_call_id(call_item)) is not None
    }

    stable_function_call_sources: dict[int, ResponseFunctionToolCall] = {}
    stable_function_nested_results: dict[int, Any] = {}

    def _snapshot_function_run(run: ToolRunFunction) -> ToolRunFunction:
        if run.tool_call.call_id in duplicate_queued_call_ids:
            return run
        stable_call = run.tool_call.model_copy(deep=True)
        stable_function_call_sources[id(stable_call)] = run.tool_call
        nested_result = peek_agent_tool_run_result(
            run.tool_call,
            scope_id=tool_state_scope_id,
        )
        if nested_result is not None:
            stable_function_nested_results[id(stable_call)] = nested_result
        return ToolRunFunction(
            tool_call=stable_call,
            function_tool=run.function_tool,
        )

    stable_function_runs = [_snapshot_function_run(run) for run in processed_response.functions]
    stable_handoff_runs = [
        run
        if run.tool_call.call_id in duplicate_queued_call_ids
        else ToolRunHandoff(
            handoff=run.handoff,
            tool_call=run.tool_call.model_copy(deep=True),
        )
        for run in processed_response.handoffs
    ]
    response_function_calls = [
        output.model_copy(deep=True)
        for output in new_response.output
        if isinstance(output, ResponseFunctionToolCall)
    ]
    authoritative_response_function_calls = [
        call for call in response_function_calls if call.call_id not in non_function_owned_call_ids
    ]
    response_call_positions: dict[str, int] = {}
    for index, output in enumerate(new_response.output):
        output_call_id = extract_tool_call_id(output)
        if output_call_id is not None:
            response_call_positions.setdefault(output_call_id, index)

    queued_function_call_ids_by_lookup_key: dict[FunctionToolLookupKey, set[str]] = {}
    queued_function_lookup_keys_by_call_id: dict[str, set[FunctionToolLookupKey]] = {}
    canonical_queued_function_calls: dict[str, ResponseFunctionToolCall] = {}
    authoritative_function_calls = [
        *(run.tool_call for run in stable_function_runs),
        *authoritative_response_function_calls,
    ]
    for function_call in authoritative_function_calls:
        lookup_key = get_function_tool_lookup_key_for_call(function_call)
        if lookup_key is None:
            continue
        queued_function_call_ids_by_lookup_key.setdefault(lookup_key, set()).add(
            function_call.call_id
        )
        queued_function_lookup_keys_by_call_id.setdefault(
            function_call.call_id,
            set(),
        ).add(lookup_key)
    for function_run in stable_function_runs:
        if function_run.tool_call.call_id not in duplicate_queued_call_ids:
            canonical_queued_function_calls[function_run.tool_call.call_id] = function_run.tool_call
    for response_call in authoritative_response_function_calls:
        if response_call.call_id not in duplicate_queued_call_ids:
            canonical_queued_function_calls.setdefault(response_call.call_id, response_call)
    queued_function_call_ids = {
        call_id
        for call_ids in queued_function_call_ids_by_lookup_key.values()
        for call_id in call_ids
    }

    def _is_function_approval(approval: ToolApprovalItem) -> bool:
        call_id = get_tool_approval_item_call_id(approval)
        if call_id in non_function_owned_call_ids and call_id not in queued_function_call_ids:
            return False
        if get_mapping_or_attr(approval.raw_item, "type") == "function_call":
            return True
        return approval.tool_lookup_key is not None or call_id in queued_function_call_ids

    function_approval_items = [
        approval
        for approval in pending_approval_items
        if _approval_matches_agent(approval) and _is_function_approval(approval)
    ]

    def _validate_approval_identity(approval: ToolApprovalItem) -> None:
        persisted_key = _approval_persisted_lookup_key(approval)
        raw_name = get_mapping_or_attr(approval.raw_item, "name")
        if persisted_key is None or not isinstance(raw_name, str) or not raw_name:
            return
        raw_namespace = get_tool_call_namespace(approval.raw_item)
        raw_key = get_function_tool_lookup_key(raw_name, raw_namespace)
        if persisted_key[-1] == raw_name and (raw_namespace is None or persisted_key == raw_key):
            return
        raw_identity = get_tool_call_qualified_name(approval.raw_item) or raw_name
        raise ModelBehaviorError(
            f"Persisted tool identity {persisted_key!r} does not match raw tool call "
            f"{raw_identity}. Restore a consistent RunState before resuming."
        )

    def _coerce_approval_call(
        approval: ToolApprovalItem,
    ) -> ResponseFunctionToolCall | None:
        raw = approval.raw_item
        if get_mapping_or_attr(raw, "type") != "function_call":
            return None
        name = get_mapping_or_attr(raw, "name")
        call_id = get_mapping_or_attr(raw, "call_id")
        arguments = get_mapping_or_attr(raw, "arguments")
        if not (
            isinstance(name, str)
            and isinstance(call_id, str)
            and call_id
            and isinstance(arguments, str)
        ):
            return None
        payload: dict[str, Any] = {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": arguments,
        }
        status = get_mapping_or_attr(raw, "status")
        if status in ("in_progress", "completed", "incomplete"):
            payload["status"] = status
        namespace = get_tool_call_namespace(raw)
        if namespace is not None:
            payload["namespace"] = namespace
        caller = get_mapping_or_attr(raw, "caller")
        if caller is not None:
            payload["caller"] = deepcopy(caller)
        item_id = get_mapping_or_attr(raw, "id")
        if isinstance(item_id, str):
            payload["id"] = item_id
        call = ResponseFunctionToolCall(**payload)

        persisted_key = _approval_persisted_lookup_key(approval)
        if get_tool_call_namespace(call) is None and persisted_key is not None:
            call = cast(
                ResponseFunctionToolCall,
                restore_tool_call_routing_identity(call, persisted_key),
            )
        return call

    validated_function_approval_items: dict[ToolApprovalItem, ToolApprovalItem] = {}
    malformed_function_approvals: list[ToolApprovalItem] = []
    for approval in function_approval_items:
        _validate_approval_identity(approval)
        approval_call = _coerce_approval_call(approval)
        if approval_call is None:
            malformed_function_approvals.append(approval)
            continue
        persisted_key = _approval_persisted_lookup_key(approval)
        queued_lookup_keys = queued_function_lookup_keys_by_call_id.get(
            approval_call.call_id,
            set(),
        )
        if queued_lookup_keys and (
            persisted_key is None or persisted_key not in queued_lookup_keys
        ):
            malformed_function_approvals.append(approval)
            continue
        queued_call_ids = (
            queued_function_call_ids_by_lookup_key.get(persisted_key, set())
            if persisted_key is not None
            else set()
        )
        if queued_call_ids and approval_call.call_id not in queued_call_ids:
            malformed_function_approvals.append(approval)
            continue
        stable_approval = ToolApprovalItem(
            agent=public_agent,
            raw_item=approval_call,
            tool_name=approval.tool_name,
            tool_namespace=approval.tool_namespace,
            tool_origin=approval.tool_origin,
            tool_lookup_key=approval.tool_lookup_key,
            _allow_bare_name_alias=approval._allow_bare_name_alias,
        )
        validated_function_approval_items[approval] = stable_approval
        stable_function_approval_sources[id(stable_approval)] = approval

    if malformed_function_approvals:
        processed_response.interruptions = list(pending_approval_items)
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=original_pre_step_items,
            new_step_items=[],
            next_step=NextStepInterruption(interruptions=list(pending_approval_items)),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
            processed_response=processed_response,
        )

    function_approval_items = list(validated_function_approval_items.values())
    function_approval_items_by_call_id = {
        cast(ResponseFunctionToolCall, approval.raw_item).call_id: approval
        for approval in function_approval_items
    }

    classifier_context_items = [
        deepcopy(output)
        for output in new_response.output
        if get_mapping_or_attr(output, "type") in ("program", "program_output")
    ]
    classifier_existing_items = cast(
        Sequence[RunItem],
        [deepcopy(getattr(item, "raw_item", item)) for item in original_pre_step_items],
    )
    classifier_server_managed_input_items = (
        deepcopy(ItemHelpers.input_to_new_input_list(original_input))
        if server_manages_conversation
        else None
    )

    available_handoffs = await get_handoffs(execution_agent, context_wrapper)
    with execution_agent._use_mcp_handoff_snapshot(available_handoffs):
        current_tool_inventory = await execution_agent.get_all_tools(context_wrapper)
    resolved_tools, resolved_handoffs = resolve_tool_name_collisions(
        current_tool_inventory,
        available_handoffs,
        collision_policy=run_config.tool_name_collision_policy,
    )
    resolved_function_tools = [tool for tool in resolved_tools if isinstance(tool, FunctionTool)]
    local_function_tool_ids = {
        id(tool) for tool in execution_agent.tools if isinstance(tool, FunctionTool)
    }
    available_function_tools = [
        tool for tool in resolved_function_tools if id(tool) in local_function_tool_ids
    ]

    stale_functions = {
        run.tool_call.call_id: run
        for run in stable_function_runs
        if run.tool_call.call_id not in duplicate_queued_call_ids
    }
    executed_handoff_call_ids = {
        executed_handoff_call_id
        for item in original_pre_step_items
        if isinstance(item, HandoffOutputItem)
        and (executed_handoff_call_id := extract_tool_call_id(item.raw_item)) is not None
    }

    calls_to_reconcile: list[ResponseFunctionToolCall] = []
    reconciled_call_ids: set[str] = set()

    def _append_reconciliation_call(call: ResponseFunctionToolCall) -> None:
        call_id = call.call_id
        if (
            call_id in duplicate_queued_call_ids
            or call_id in reconciled_call_ids
            or call_id in executed_handoff_call_ids
        ):
            return
        stale_run = stale_functions.get(call_id)
        nested_result = (
            peek_agent_tool_run_result(stale_run.tool_call, scope_id=tool_state_scope_id)
            if stale_run is not None
            else None
        )
        if _has_output_item(call_id, "function_call_output") and not (
            nested_result and getattr(nested_result, "interruptions", None)
        ):
            return
        approval = function_approval_items_by_call_id.get(call_id)
        queued_call = canonical_queued_function_calls.get(call_id)
        if queued_call is not None:
            call = queued_call.model_copy(deep=True)
        elif approval is not None:
            call = cast(ResponseFunctionToolCall, approval.raw_item)
        reconciled_call_ids.add(call_id)
        calls_to_reconcile.append(call)

    for output in authoritative_response_function_calls:
        _append_reconciliation_call(output)
    for function_run in stable_function_runs:
        _append_reconciliation_call(function_run.tool_call)
    for handoff_run in stable_handoff_runs:
        _append_reconciliation_call(handoff_run.tool_call)
    for approval_item in function_approval_items:
        _append_reconciliation_call(cast(ResponseFunctionToolCall, approval_item.raw_item))

    classifier_tools: list[Tool] = [*resolved_function_tools]
    classifier_tools.extend(
        tool for tool in resolved_tools if isinstance(tool, ProgrammaticToolCallingTool)
    )
    classifier_response = ModelResponse(
        output=cast(Any, [*classifier_context_items, *calls_to_reconcile]),
        usage=new_response.usage,
        response_id=new_response.response_id,
        request_id=new_response.request_id,
    )
    classified = process_model_response(
        agent=public_agent,
        all_tools=classifier_tools,
        response=classifier_response,
        output_schema=get_output_schema(execution_agent),
        handoffs=cast(list[Handoff], resolved_handoffs),
        existing_items=classifier_existing_items,
        run_config=replace(run_config, tool_not_found_behavior="return_error_to_model"),
        server_manages_conversation=server_manages_conversation,
        server_managed_input_items=classifier_server_managed_input_items,
        allow_apply_patch_function_fallback=False,
    )
    current_functions = {run.tool_call.call_id: run for run in classified.functions}
    current_handoffs = {run.tool_call.call_id: run for run in classified.handoffs}
    current_missing = {run.tool_call.call_id: run for run in classified.function_tools_not_found}
    pending_nested_transfers: list[tuple[ResponseFunctionToolCall, Any]] = []
    pending_nested_drops: list[ResponseFunctionToolCall] = []

    def _cached_nested_result(run: ToolRunFunction) -> Any | None:
        stable_result = stable_function_nested_results.get(id(run.tool_call))
        if stable_result is not None:
            return stable_result
        return peek_agent_tool_run_result(run.tool_call, scope_id=tool_state_scope_id)

    def _drop_stable_nested_result(call: ResponseFunctionToolCall) -> None:
        stable_function_nested_results.pop(id(call), None)
        drop_agent_tool_run_result(call, scope_id=tool_state_scope_id)
        source_call = stable_function_call_sources.get(id(call))
        if source_call is not None:
            drop_agent_tool_run_result(source_call, scope_id=tool_state_scope_id)

    def _pending_nested_result(run: ToolRunFunction) -> Any | None:
        result = _cached_nested_result(run)
        return result if result and getattr(result, "interruptions", None) else None

    def _reject_nested_replacement(run: ToolRunFunction) -> None:
        if _pending_nested_result(run) is None:
            pending_nested_drops.append(run.tool_call)
            return
        qualified_name = get_tool_call_qualified_name(run.tool_call) or run.tool_call.name
        # TODO: Persist Agent.as_tool() owner identity so replacement before RunState
        # restoration can be detected and safely migrated.
        raise ModelBehaviorError(
            f"Cannot reconcile queued tool {qualified_name} with a new tool or handoff while "
            "its Agent.as_tool() run is interrupted. Restore the original tool configuration "
            "or start a new run."
        )

    def _rebind_function_run(
        stale_run: ToolRunFunction | None,
        current_run: ToolRunFunction,
    ) -> ToolRunFunction:
        if stale_run is None:
            return current_run
        cached_result = _cached_nested_result(stale_run)
        pending_result = _pending_nested_result(stale_run)
        if stale_run.function_tool is not current_run.function_tool:
            stale_owner = getattr(stale_run.function_tool, "_agent_instance", None)
            current_owner = getattr(current_run.function_tool, "_agent_instance", None)
            if pending_result is not None and (
                stale_owner is None or stale_owner is not current_owner
            ):
                _reject_nested_replacement(stale_run)
            if cached_result is not None and pending_result is None:
                pending_nested_drops.append(stale_run.tool_call)
        if pending_result is not None and current_run.tool_call is not stale_run.tool_call:
            pending_nested_transfers.append((current_run.tool_call, pending_result))
            pending_nested_drops.append(stale_run.tool_call)
        return current_run

    reconciled_functions: list[ToolRunFunction] = [
        run for run in stable_function_runs if run.tool_call.call_id not in reconciled_call_ids
    ]
    reconciled_handoffs: list[ToolRunHandoff] = [
        run for run in stable_handoff_runs if run.tool_call.call_id not in reconciled_call_ids
    ]
    missing_function_tools: list[ToolRunFunctionNotFound] = []
    missing_state_calls: list[ResponseFunctionToolCall] = []
    missing_function_call_ids: set[str] = set()

    for call in calls_to_reconcile:
        call_id = call.call_id
        approval_record = function_approval_items_by_call_id.get(call_id)
        approval_status = (
            context_wrapper.get_approval_status(
                approval_record.tool_name or call.name,
                call_id,
                tool_namespace=approval_record.tool_namespace,
                existing_pending=approval_record,
                current_invocation=ToolApprovalItem(
                    agent=public_agent,
                    raw_item=call,
                    tool_name=call.name,
                    tool_namespace=get_tool_call_namespace(call),
                ),
            )
            if approval_record is not None
            else True
        )
        stale_function = stale_functions.get(call_id)
        current_function = current_functions.get(call_id)
        if current_function is not None:
            reconciled_functions.append(_rebind_function_run(stale_function, current_function))
            continue

        current_handoff = current_handoffs.get(call_id)
        if current_handoff is not None and approval_record is not None:
            approval_status = context_wrapper.get_approval_status(
                approval_record.tool_name or call.name,
                call_id,
                tool_namespace=approval_record.tool_namespace,
                existing_pending=approval_record,
                current_invocation=ToolApprovalItem(
                    agent=public_agent,
                    raw_item=current_handoff.tool_call,
                    tool_name=current_handoff.tool_call.name,
                    tool_namespace=get_tool_call_namespace(current_handoff.tool_call),
                ),
            )
        if current_handoff is not None and approval_status is True:
            if stale_function is not None:
                _reject_nested_replacement(stale_function)
            reconciled_handoffs.append(current_handoff)
            continue

        missing = current_missing.get(call_id)
        if missing is not None and approval_status is True:
            if run_config.tool_not_found_behavior != "return_error_to_model":
                qualified_name = (
                    get_tool_call_qualified_name(missing.tool_call) or missing.tool_name
                )
                raise ModelBehaviorError(
                    f"Tool {qualified_name} not found in agent {public_agent.name}"
                )
            cached_result = (
                _cached_nested_result(stale_function)
                if stale_function is not None
                else peek_agent_tool_run_result(
                    missing.tool_call,
                    scope_id=tool_state_scope_id,
                )
            )
            if cached_result is not None:
                state_call = missing.tool_call
                if stale_function is not None:
                    reconciled_functions.append(stale_function)
                    state_call = stale_function.tool_call
                missing_state_calls.append(state_call)
            missing_function_call_ids.add(call_id)
            missing_function_tools.append(missing)
            continue

        if stale_function is not None:
            reconciled_functions.append(stale_function)
        elif approval_record is not None:
            if approval_status is None:
                _add_pending_interruption(approval_record)
            elif approval_status is False:
                rejection_call = cast(
                    ResponseFunctionToolCall,
                    approval_record.raw_item,
                )
                rejection_message = await resolve_approval_rejection_message(
                    context_wrapper=context_wrapper,
                    run_config=run_config,
                    tool_call=rejection_call,
                    tool_type="function",
                    tool_name=get_tool_call_trace_name(rejection_call) or rejection_call.name,
                    call_id=call_id,
                    tool_namespace=get_tool_call_namespace(rejection_call),
                    tool_lookup_key=approval_record.tool_lookup_key,
                    existing_pending=approval_record,
                )
                rejected_function_outputs.append(
                    function_rejection_item(
                        public_agent,
                        rejection_call,
                        rejection_message=rejection_message,
                        output_json_schema=None,
                        scope_id=tool_state_scope_id,
                        tool_origin=approval_record.tool_origin,
                    )
                )
                rejected_function_call_ids.add(call_id)

    collector_owned_call_ids = {
        *(_shell_call_id_from_run(run) for run in processed_response.shell_calls),
        *(_apply_patch_call_id_from_run(run) for run in processed_response.apply_patch_calls),
        *(_custom_call_id_from_run(run) for run in processed_response.custom_tool_calls),
        *(extract_mcp_request_id_from_run(run) for run in processed_response.mcp_approval_requests),
    }
    for original_approval in pending_approval_items:
        if not _approval_matches_agent(original_approval):
            nested_approval = (
                run_state._find_nested_approval_state(original_approval)
                if run_state is not None
                else None
            )
            if nested_approval is None:
                if any(
                    agent_tool_resume_checkpoint_owns_approval(
                        nested_result,
                        original_approval,
                    )
                    for nested_result in stable_function_nested_results.values()
                ):
                    continue
                _add_pending_interruption(original_approval)
                continue
            nested_state, nested_item = nested_approval
            nested_context = nested_state._context
            nested_call_id = get_tool_approval_item_call_id(nested_item)
            nested_status = (
                nested_context.get_approval_status(
                    nested_item.tool_name or "",
                    nested_call_id,
                    tool_namespace=nested_item.tool_namespace,
                    existing_pending=nested_item,
                )
                if nested_context is not None and nested_call_id is not None
                else None
            )
            if nested_status is None:
                _add_pending_interruption(original_approval)
            continue
        approval_snapshot = validated_function_approval_items.get(original_approval)
        if approval_snapshot is None:
            approval = original_approval
            approval_call_id = get_tool_approval_item_call_id(approval)
            if approval_call_id in collector_owned_call_ids:
                continue
            if (
                approval_call_id is None
                or context_wrapper.get_approval_status(
                    approval.tool_name or "",
                    approval_call_id,
                    tool_namespace=approval.tool_namespace,
                    existing_pending=approval,
                )
                is None
            ):
                _add_pending_interruption(approval)
            continue
        approval_call_id = cast(
            ResponseFunctionToolCall,
            approval_snapshot.raw_item,
        ).call_id
        if (
            approval_call_id not in reconciled_call_ids
            and context_wrapper.get_approval_status(
                approval_snapshot.tool_name or "",
                approval_call_id,
                tool_namespace=approval_snapshot.tool_namespace,
                existing_pending=approval_snapshot,
            )
            is None
        ):
            _add_pending_interruption(approval_snapshot)

    selectable_function_runs = [
        run
        for run in reconciled_functions
        if run.tool_call.call_id not in missing_function_call_ids
    ]

    # Validate every current execution candidate before any dynamic approval callback or
    # output-based replay suppression can run. This keeps a changed invocation under an approved
    # call ID from triggering sibling user code or hiding behind a previously committed output.
    for function_run in selectable_function_runs:
        context_wrapper._approved_tool_invocation_status(
            function_run.tool_call,
            tool_lookup_key=get_function_tool_lookup_key_for_tool(function_run.function_tool),
        )
    for handoff_run in reconciled_handoffs:
        context_wrapper._approved_tool_invocation_status(
            handoff_run.tool_call,
            invocation_role="handoff",
        )
    _validate_unresolved_function_calls(context_wrapper, missing_function_tools)
    function_tool_runs = await _select_function_tool_runs_for_resume(
        selectable_function_runs,
        approval_items_by_call_id=function_approval_items_by_call_id,
        context_wrapper=context_wrapper,
        needs_approval_checker=_function_requires_approval,
        output_exists_checker=_function_output_exists,
        record_rejection=_record_function_rejection,
        pending_interruption_adder=_add_pending_interruption,
        pending_item_builder=lambda run: ToolApprovalItem(
            agent=public_agent,
            raw_item=run.tool_call,
            tool_name=run.function_tool.name,
            tool_namespace=get_tool_call_namespace(run.tool_call),
            tool_origin=get_function_tool_origin(run.function_tool),
            tool_lookup_key=get_function_tool_lookup_key_for_call(run.tool_call),
            _allow_bare_name_alias=should_allow_bare_name_approval_alias(
                run.function_tool,
                available_function_tools,
            ),
        ),
    )

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
        agent=public_agent,
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
        agent=public_agent,
        pending_interruption_adder=_add_pending_interruption,
        needs_approval_checker=_apply_patch_needs_approval,
        output_exists_checker=_apply_patch_output_exists,
    )

    approved_custom_tool_calls, rejected_custom_tool_results = await _collect_runs_by_approval(
        processed_response.custom_tool_calls,
        call_id_extractor=_custom_call_id_from_run,
        tool_name_resolver=_custom_tool_name,
        rejection_builder=_build_custom_rejection,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
        agent=public_agent,
        pending_interruption_adder=_add_pending_interruption,
        needs_approval_checker=_custom_tool_needs_approval,
        output_exists_checker=_custom_tool_output_exists,
    )

    plan = _build_plan_for_resume_turn(
        processed_response=processed_response,
        agent=public_agent,
        context_wrapper=context_wrapper,
        approval_items_by_call_id=approval_items_by_call_id,
        pending_interruptions=pending_interruptions,
        pending_interruption_adder=_add_pending_interruption,
        function_runs=function_tool_runs,
        computer_actions=pending_computer_actions,
        custom_tool_calls=approved_custom_tool_calls,
        shell_calls=approved_shell_calls,
        apply_patch_calls=approved_apply_patch_calls,
    )
    missing_output_items = await _build_tool_not_found_output_items(
        agent=public_agent,
        calls=missing_function_tools,
        context_wrapper=context_wrapper,
        run_config=run_config,
    )

    for current_call, nested_result in pending_nested_transfers:
        record_agent_tool_run_result(
            current_call,
            nested_result,
            scope_id=tool_state_scope_id,
        )
    processed_response.functions = reconciled_functions
    processed_response.handoffs = reconciled_handoffs
    processed_response.function_tools_not_found = missing_function_tools
    dropped_nested_call_ids: set[int] = set()
    for stale_call in pending_nested_drops:
        if id(stale_call) in dropped_nested_call_ids:
            continue
        dropped_nested_call_ids.add(id(stale_call))
        _drop_stable_nested_result(stale_call)

    call_positions = dict(response_call_positions)
    next_call_position = len(new_response.output)
    for call in calls_to_reconcile:
        if call.call_id not in call_positions:
            call_positions[call.call_id] = next_call_position
            next_call_position += 1

    committed_tool_outputs: list[RunItem] = []

    def _commit_tool_output(item: RunItem) -> None:
        if any(existing is item for existing in committed_tool_outputs):
            return
        committed_tool_outputs.append(item)
        committed_tool_outputs.sort(
            key=lambda output: call_positions.get(
                extract_tool_call_id(getattr(output, "raw_item", None)) or "",
                len(call_positions),
            )
        )
        if run_state is not None:
            run_state._generated_items = [*original_pre_step_items, *committed_tool_outputs]
        _register_tool_call_items(context_wrapper, [item])

    (
        function_results,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        computer_results,
        custom_tool_results,
        shell_results,
        apply_patch_results,
        _local_shell_results,
    ) = await _execute_tool_plan(
        plan=plan,
        bindings=bindings,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
        tool_output_committer=_commit_tool_output,
    )

    for interruption in _collect_tool_interruptions(
        function_results=function_results,
        custom_tool_results=custom_tool_results,
        shell_results=shell_results,
        apply_patch_results=apply_patch_results,
    ):
        _add_pending_interruption(interruption)

    new_items, append_if_new = _make_unique_item_appender(original_pre_step_items)

    function_result_items = _build_tool_result_items(
        function_results=function_results,
        computer_results=[],
        custom_tool_results=[],
        shell_results=[],
        apply_patch_results=[],
        local_shell_results=[],
    )
    function_outcomes = [
        *function_result_items,
        *missing_output_items,
        *rejected_function_outputs,
    ]
    function_outcomes.sort(
        key=lambda item: call_positions.get(
            extract_tool_call_id(getattr(item, "raw_item", None)) or "",
            len(call_positions),
        )
    )
    for item in function_outcomes:
        append_if_new(item)
    for item in _build_tool_result_items(
        function_results=[],
        computer_results=computer_results,
        custom_tool_results=custom_tool_results,
        shell_results=shell_results,
        apply_patch_results=apply_patch_results,
        local_shell_results=[],
    ):
        append_if_new(item)
    for pending_item in pending_interruptions:
        append_if_new(pending_item)
    for shell_rejection in rejected_shell_results:
        append_if_new(shell_rejection)
    for custom_tool_rejection in rejected_custom_tool_results:
        append_if_new(custom_tool_rejection)
    for apply_patch_rejection in rejected_apply_patch_results:
        append_if_new(apply_patch_rejection)
    for approved_response in plan.approved_mcp_responses:
        append_if_new(approved_response)

    def _checkpoint_new_items() -> None:
        if run_state is not None:
            run_state._generated_items = [*original_pre_step_items, *new_items]
        _register_tool_call_items(context_wrapper, new_items)

    _checkpoint_new_items()

    def _commit_missing_state(result: SingleStepResult) -> SingleStepResult:
        _checkpoint_new_items()
        if missing_function_call_ids:
            processed_response.functions = [
                run
                for run in processed_response.functions
                if run.tool_call.call_id not in missing_function_call_ids
            ]
        for call in missing_state_calls:
            _drop_stable_nested_result(call)
        return result

    processed_response.interruptions = pending_interruptions
    if pending_interruptions:
        return _commit_missing_state(
            SingleStepResult(
                original_input=original_input,
                model_response=new_response,
                pre_step_items=original_pre_step_items,
                new_step_items=new_items,
                next_step=NextStepInterruption(interruptions=list(pending_interruptions)),
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
                processed_response=processed_response,
            )
        )

    await _append_mcp_callback_results(
        agent=public_agent,
        requests=plan.mcp_requests_with_callback,
        context_wrapper=context_wrapper,
        append_item=append_if_new,
    )
    _checkpoint_new_items()
    (
        pending_hosted_mcp_approvals,
        pending_hosted_mcp_approval_ids,
    ) = process_hosted_mcp_approvals(
        original_pre_step_items=original_pre_step_items,
        mcp_approval_requests=processed_response.mcp_approval_requests,
        context_wrapper=context_wrapper,
        agent=public_agent,
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

    pending_handoffs = [
        handoff
        for handoff in processed_response.handoffs
        if not handoff.tool_call.call_id
        or handoff.tool_call.call_id not in executed_handoff_call_ids
    ]

    if pending_handoffs:

        def _commit_handoff_output(
            _handoff_output: HandoffOutputItem,
            new_agent: Agent[Any],
        ) -> None:
            _checkpoint_new_items()
            if run_state is not None:
                run_state._current_agent = new_agent

        return _commit_missing_state(
            await execute_handoffs_call(
                public_agent=public_agent,
                original_input=original_input,
                pre_step_items=pre_step_items,
                new_step_items=new_items,
                new_response=new_response,
                run_handoffs=pending_handoffs,
                hooks=hooks,
                context_wrapper=context_wrapper,
                run_config=run_config,
                server_manages_conversation=server_manages_conversation,
                nest_handoff_history_fn=nest_handoff_history_fn,
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
                handoff_output_committer=_commit_handoff_output,
            )
        )

    tool_final_output = await _maybe_finalize_from_tool_results(
        public_agent=public_agent,
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
        return _commit_missing_state(tool_final_output)

    return _commit_missing_state(
        SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_items,
            next_step=NextStepRunAgain(),
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )
    )


def process_model_response(
    *,
    agent: Agent[Any],
    all_tools: list[Tool],
    response: ModelResponse,
    output_schema: AgentOutputSchemaBase | None,
    handoffs: list[Handoff],
    existing_items: Sequence[RunItem] | None = None,
    run_config: RunConfig | None = None,
    server_manages_conversation: bool = False,
    server_managed_input_items: Sequence[Any] | None = None,
    allow_apply_patch_function_fallback: bool = True,
) -> ProcessedResponse:
    items: list[RunItem] = []

    run_handoffs = []
    functions = []
    computer_actions = []
    custom_tool_calls = []
    local_shell_calls = []
    shell_calls = []
    apply_patch_calls = []
    mcp_approval_requests = []
    function_tools_not_found = []
    tools_used: list[str] = []
    handoff_map = {handoff.tool_name: handoff for handoff in handoffs}
    function_map = build_function_tool_lookup_map(
        [tool for tool in all_tools if isinstance(tool, FunctionTool)]
    )
    custom_tool_map = {tool.name: tool for tool in all_tools if isinstance(tool, CustomTool)}
    computer_tool = next((tool for tool in all_tools if isinstance(tool, ComputerTool)), None)
    local_shell_tool = next((tool for tool in all_tools if isinstance(tool, LocalShellTool)), None)
    shell_tool = next((tool for tool in all_tools if isinstance(tool, ShellTool)), None)
    apply_patch_tool = next((tool for tool in all_tools if isinstance(tool, ApplyPatchTool)), None)
    code_interpreter_tool = next(
        (tool for tool in all_tools if isinstance(tool, CodeInterpreterTool)),
        None,
    )
    programmatic_tool = next(
        (tool for tool in all_tools if isinstance(tool, ProgrammaticToolCallingTool)), None
    )
    hosted_mcp_server_map = {
        tool.tool_config["server_label"]: tool
        for tool in all_tools
        if isinstance(tool, HostedMCPTool)
    }
    hosted_mcp_tool_metadata = collect_mcp_list_tools_metadata(existing_items or ())
    hosted_mcp_tool_metadata.update(collect_mcp_list_tools_metadata(response.output))

    program_call_ids, completed_program_call_ids = _collect_program_parent_state(
        [*(server_managed_input_items or ()), *(existing_items or ())],
        server_manages_conversation=server_manages_conversation,
    )
    _, response_completed_program_call_ids = _collect_program_parent_state(response.output)

    def _dump_output_item(raw_item: Any) -> dict[str, Any]:
        if isinstance(raw_item, dict):
            return dict(raw_item)
        if hasattr(raw_item, "model_dump"):
            dumped = cast(Any, raw_item).model_dump(exclude_unset=True)
            if isinstance(dumped, Mapping):
                return dict(dumped)
            return {"type": get_mapping_or_attr(raw_item, "type")}
        return {
            "type": get_mapping_or_attr(raw_item, "type"),
            "id": get_mapping_or_attr(raw_item, "id"),
        }

    for output in response.output:
        output_type = get_mapping_or_attr(output, "type")
        logger.debug(
            "Processing output item type=%s class=%s",
            output_type,
            output.__class__.__name__ if hasattr(output, "__class__") else type(output),
        )
        caller = get_mapping_or_attr(output, "caller")
        is_program_owned = get_mapping_or_attr(caller, "type") == "program"
        if (
            output_type in ("program", "program_output") or is_program_owned
        ) and programmatic_tool is None:
            tools_used.append("programmatic_tool_calling")
            _error_tracing.attach_error_to_current_span(
                SpanError(
                    message="Programmatic tool not found",
                    data={},
                )
            )
            raise ModelBehaviorError(
                f"Model produced {output_type} item without a programmatic_tool_calling tool."
            )
        if is_program_owned:
            ensure_programmatic_tool_call_parent(
                tool_call=output,
                programmatic_tool_present=programmatic_tool is not None,
                program_call_ids=program_call_ids,
                completed_program_call_ids=(
                    completed_program_call_ids | response_completed_program_call_ids
                ),
                agent_name=agent.name,
            )
        if output_type == "program":
            call_id = get_mapping_or_attr(output, "call_id")
            if not isinstance(call_id, str) or not call_id:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Program call ID missing",
                        data={"call_id": call_id},
                    )
                )
                raise ModelBehaviorError("Model produced program item without a valid call_id.")
            program_call_ids.add(call_id)
            items.append(ToolCallItem(raw_item=cast(Program, output), agent=agent))
            tools_used.append("programmatic_tool_calling")
            continue
        if output_type == "program_output":
            call_id = get_mapping_or_attr(output, "call_id")
            if not isinstance(call_id, str) or call_id not in program_call_ids:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Program parent not found",
                        data={
                            "output_type": output_type,
                            "call_id": call_id,
                        },
                    )
                )
                raise ModelBehaviorError(
                    "Model produced program_output item that does not match a parent program item."
                )
            if call_id in completed_program_call_ids:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Program parent already completed",
                        data={
                            "output_type": output_type,
                            "call_id": call_id,
                        },
                    )
                )
                raise ModelBehaviorError(
                    "Model produced program_output item whose parent program is already completed."
                )
            status = get_mapping_or_attr(output, "status")
            if status not in ("completed", "incomplete"):
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Program output status invalid",
                        data={
                            "call_id": call_id,
                            "status": status,
                        },
                    )
                )
                raise ModelBehaviorError(
                    "Model produced program_output item without a valid status."
                )
            result = get_mapping_or_attr(output, "result")
            if not isinstance(result, str):
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Program output result invalid",
                        data={
                            "call_id": call_id,
                            "result_type": type(result).__name__,
                        },
                    )
                )
                raise ModelBehaviorError(
                    "Model produced program_output item without a string result."
                )
            items.append(
                ToolCallOutputItem(
                    raw_item=cast(ProgramOutput, output),
                    output=result,
                    agent=agent,
                )
            )
            if status == "completed":
                completed_program_call_ids.add(call_id)
            tools_used.append("programmatic_tool_calling")
            continue
        if output_type == "shell_call":
            if isinstance(output, dict):
                shell_call_raw = dict(output)
            elif hasattr(output, "model_dump"):
                shell_call_raw = cast(Any, output).model_dump(exclude_unset=True)
            else:
                shell_call_raw = {
                    "type": "shell_call",
                    "id": get_mapping_or_attr(output, "id"),
                    "call_id": get_mapping_or_attr(output, "call_id"),
                    "status": get_mapping_or_attr(output, "status"),
                    "action": get_mapping_or_attr(output, "action"),
                    "environment": get_mapping_or_attr(output, "environment"),
                    "created_by": get_mapping_or_attr(output, "created_by"),
                }
            shell_call_raw.pop("created_by", None)
            if shell_tool is None:
                tools_used.append("shell")
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Shell tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError("Model produced shell call without a shell tool.")
            items.append(
                ToolCallItem(
                    raw_item=cast(Any, shell_call_raw),
                    agent=agent,
                    _resolved_tool_name=shell_tool.name,
                )
            )
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=shell_tool.allowed_callers,
                tool_name=shell_tool.name,
                agent_name=agent.name,
            )
            tools_used.append(shell_tool.name)
            shell_environment = shell_tool.environment
            if shell_environment is None or shell_environment["type"] != "local":
                logger.debug(
                    "Skipping local shell execution for hosted shell tool %s", shell_tool.name
                )
                continue
            if shell_tool.executor is None:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Local shell executor not found",
                        data={},
                    )
                )
                raise ModelBehaviorError(
                    "Model produced local shell call without a local shell executor."
                )
            call_identifier = get_mapping_or_attr(output, "call_id")
            logger.debug("Queuing shell_call %s", call_identifier)
            shell_calls.append(ToolRunShellCall(tool_call=output, shell_tool=shell_tool))
            continue
        if output_type == "shell_call_output" and isinstance(
            output, dict | ResponseFunctionShellToolCallOutput
        ):
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=shell_tool.allowed_callers if shell_tool is not None else None,
                tool_name=shell_tool.name if shell_tool is not None else "shell",
                agent_name=agent.name,
            )
            tools_used.append(shell_tool.name if shell_tool is not None else "shell")
            if isinstance(output, dict):
                shell_output_raw = dict(output)
            else:
                shell_output_raw = output.model_dump(exclude_unset=True)
            shell_output_raw.pop("created_by", None)
            shell_outputs = shell_output_raw.get("output")
            if isinstance(shell_outputs, list):
                for shell_output in shell_outputs:
                    if isinstance(shell_output, dict):
                        shell_output.pop("created_by", None)
            items.append(
                ToolCallOutputItem(
                    raw_item=cast(Any, shell_output_raw),
                    output=shell_output_raw.get("output"),
                    agent=agent,
                )
            )
            continue
        if output_type == "apply_patch_call":
            if isinstance(output, dict):
                apply_patch_call_raw = dict(output)
            elif hasattr(output, "model_dump"):
                apply_patch_call_raw = cast(Any, output).model_dump(exclude_unset=True)
            else:
                apply_patch_call_raw = {
                    "type": "apply_patch_call",
                    "id": get_mapping_or_attr(output, "id"),
                    "call_id": get_mapping_or_attr(output, "call_id"),
                    "status": get_mapping_or_attr(output, "status"),
                    "operation": get_mapping_or_attr(output, "operation"),
                    "created_by": get_mapping_or_attr(output, "created_by"),
                }
            apply_patch_call_raw.pop("created_by", None)
            if apply_patch_tool is not None:
                ensure_tool_caller_allowed(
                    tool_call=apply_patch_call_raw,
                    allowed_callers=apply_patch_tool.allowed_callers,
                    tool_name=apply_patch_tool.name,
                    agent_name=agent.name,
                )
                items.append(
                    ToolCallItem(
                        raw_item=cast(Any, apply_patch_call_raw),
                        agent=agent,
                        _resolved_tool_name=apply_patch_tool.name,
                    )
                )
                tools_used.append(apply_patch_tool.name)
                call_identifier = get_mapping_or_attr(apply_patch_call_raw, "call_id")
                logger.debug("Queuing apply_patch_call %s", call_identifier)
                apply_patch_calls.append(
                    ToolRunApplyPatchCall(
                        tool_call=apply_patch_call_raw,
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
        if output_type == "tool_search_call":
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=None,
                tool_name="tool_search",
                agent_name=agent.name,
            )
            tool_search_call_raw = coerce_tool_search_call_raw_item(output)
            if get_mapping_or_attr(tool_search_call_raw, "execution") == "client":
                raise ModelBehaviorError(
                    "Client-executed tool_search calls are not supported by the standard "
                    "agent runner. Handle the tool_search_call yourself and return a matching "
                    "tool_search_output item with the same call_id."
                )
            items.append(ToolSearchCallItem(raw_item=tool_search_call_raw, agent=agent))
            tools_used.append("tool_search")
            continue
        if output_type == "tool_search_output":
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=None,
                tool_name="tool_search",
                agent_name=agent.name,
            )
            items.append(
                ToolSearchOutputItem(
                    raw_item=coerce_tool_search_output_raw_item(output),
                    agent=agent,
                )
            )
            tools_used.append("tool_search")
            continue
        if isinstance(output, ResponseOutputMessage):
            items.append(MessageOutputItem(raw_item=output, agent=agent))
        elif isinstance(output, ResponseFileSearchToolCall):
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=None,
                tool_name="file_search",
                agent_name=agent.name,
            )
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("file_search")
        elif isinstance(output, ResponseFunctionWebSearch):
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=None,
                tool_name="web_search",
                agent_name=agent.name,
            )
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("web_search")
        elif isinstance(output, ResponseReasoningItem):
            items.append(ReasoningItem(raw_item=output, agent=agent))
        elif isinstance(output, ResponseComputerToolCall):
            if computer_tool is None:
                tools_used.append("computer")
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Computer tool not found",
                        data={},
                    )
                )
                raise ModelBehaviorError("Model produced computer action without a computer tool.")
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=None,
                tool_name=computer_tool.name,
                agent_name=agent.name,
            )
            items.append(
                ToolCallItem(
                    raw_item=output,
                    agent=agent,
                    _resolved_tool_name=computer_tool.name,
                )
            )
            tools_used.append(computer_tool.name)
            computer_actions.append(
                ToolRunComputerAction(tool_call=output, computer_tool=computer_tool)
            )
        elif isinstance(output, McpApprovalRequest):
            if output.server_label not in hosted_mcp_server_map:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="MCP server label not found",
                        data={"server_label": output.server_label},
                    )
                )
                raise ModelBehaviorError(f"MCP server label {output.server_label} not found")
            server = hosted_mcp_server_map[output.server_label]
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=server.tool_config.get("allowed_callers"),
                tool_name=server.name,
                agent_name=agent.name,
            )
            items.append(MCPApprovalRequestItem(raw_item=output, agent=agent))
            mcp_approval_requests.append(
                ToolRunMCPApprovalRequest(
                    request_item=output,
                    mcp_tool=server,
                )
            )
            if server.on_approval_request is None:
                logger.debug(
                    "Hosted MCP server %s has no on_approval_request hook; approvals will be "
                    "surfaced as interruptions for the caller to handle.",
                    output.server_label,
                )
        elif isinstance(output, McpListTools):
            mcp_server = hosted_mcp_server_map.get(output.server_label)
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=(
                    mcp_server.tool_config.get("allowed_callers")
                    if mcp_server is not None
                    else None
                ),
                tool_name=mcp_server.name if mcp_server is not None else "hosted_mcp",
                agent_name=agent.name,
            )
            items.append(MCPListToolsItem(raw_item=output, agent=agent))
        elif isinstance(output, McpCall):
            mcp_server = hosted_mcp_server_map.get(output.server_label)
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=(
                    mcp_server.tool_config.get("allowed_callers")
                    if mcp_server is not None
                    else None
                ),
                tool_name=mcp_server.name if mcp_server is not None else "hosted_mcp",
                agent_name=agent.name,
            )
            metadata = hosted_mcp_tool_metadata.get((output.server_label, output.name))
            items.append(
                ToolCallItem(
                    raw_item=output,
                    agent=agent,
                    description=metadata.description if metadata is not None else None,
                    title=metadata.title if metadata is not None else None,
                    tool_origin=ToolOrigin(
                        type=ToolOriginType.MCP,
                        mcp_server_name=output.server_label,
                    ),
                )
            )
            tools_used.append("mcp")
        elif isinstance(output, ImageGenerationCall):
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=None,
                tool_name="image_generation",
                agent_name=agent.name,
            )
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("image_generation")
        elif isinstance(output, ResponseCodeInterpreterToolCall):
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=(
                    code_interpreter_tool.tool_config.get("allowed_callers")
                    if code_interpreter_tool is not None
                    else None
                ),
                tool_name=(
                    code_interpreter_tool.name
                    if code_interpreter_tool is not None
                    else "code_interpreter"
                ),
                agent_name=agent.name,
            )
            items.append(ToolCallItem(raw_item=output, agent=agent))
            tools_used.append("code_interpreter")
        elif isinstance(output, LocalShellCall):
            if local_shell_tool is not None:
                ensure_tool_caller_allowed(
                    tool_call=output,
                    allowed_callers=None,
                    tool_name=local_shell_tool.name,
                    agent_name=agent.name,
                )
                items.append(
                    ToolCallItem(
                        raw_item=output,
                        agent=agent,
                        _resolved_tool_name=local_shell_tool.name,
                    )
                )
                tools_used.append("local_shell")
                local_shell_calls.append(
                    ToolRunLocalShellCall(tool_call=output, local_shell_tool=local_shell_tool)
                )
            elif shell_tool is not None:
                ensure_tool_caller_allowed(
                    tool_call=output,
                    allowed_callers=shell_tool.allowed_callers,
                    tool_name=shell_tool.name,
                    agent_name=agent.name,
                )
                items.append(
                    ToolCallItem(
                        raw_item=output,
                        agent=agent,
                        _resolved_tool_name=shell_tool.name,
                    )
                )
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
        elif isinstance(output, ResponseCustomToolCall):
            custom_tool = custom_tool_map.get(output.name)
            if custom_tool is not None:
                ensure_tool_caller_allowed(
                    tool_call=output,
                    allowed_callers=custom_tool.allowed_callers,
                    tool_name=custom_tool.name,
                    agent_name=agent.name,
                )
                items.append(ToolCallItem(raw_item=cast(Any, output), agent=agent))
                tools_used.append(custom_tool.name)
                custom_tool_calls.append(ToolRunCustom(tool_call=output, custom_tool=custom_tool))
            elif is_apply_patch_name(output.name, apply_patch_tool):
                pseudo_call = normalize_apply_patch_fallback_call(output)
                assert pseudo_call is not None
                if apply_patch_tool is not None:
                    ensure_tool_caller_allowed(
                        tool_call=pseudo_call,
                        allowed_callers=apply_patch_tool.allowed_callers,
                        tool_name=apply_patch_tool.name,
                        agent_name=agent.name,
                    )
                    items.append(
                        ToolCallItem(
                            raw_item=cast(Any, pseudo_call),
                            agent=agent,
                            _resolved_tool_name=apply_patch_tool.name,
                        )
                    )
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
            else:
                items.append(ToolCallItem(raw_item=cast(Any, output), agent=agent))
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Custom tool not found",
                        data={"tool_name": output.name},
                    )
                )
                raise ModelBehaviorError(f"Tool {output.name} not found in agent {agent.name}")
        elif (
            isinstance(output, ResponseFunctionToolCall)
            and allow_apply_patch_function_fallback
            and is_apply_patch_name(output.name, apply_patch_tool)
            and get_function_tool_lookup_key_for_call(output) not in function_map
        ):
            pseudo_call = normalize_apply_patch_fallback_call(output)
            assert pseudo_call is not None
            if apply_patch_tool is not None:
                ensure_tool_caller_allowed(
                    tool_call=pseudo_call,
                    allowed_callers=apply_patch_tool.allowed_callers,
                    tool_name=apply_patch_tool.name,
                    agent_name=agent.name,
                )
                items.append(
                    ToolCallItem(
                        raw_item=cast(Any, pseudo_call),
                        agent=agent,
                        _resolved_tool_name=apply_patch_tool.name,
                    )
                )
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

        tools_used.append(get_tool_call_trace_name(output) or output.name)
        qualified_output_name = get_tool_call_qualified_name(output)

        if is_handoff_tool_call(output, handoff_map):
            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=None,
                tool_name=output.name,
                agent_name=agent.name,
            )
            items.append(HandoffCallItem(raw_item=output, agent=agent))
            handoff = ToolRunHandoff(
                tool_call=output,
                handoff=handoff_map[output.name],
            )
            run_handoffs.append(handoff)
        else:
            lookup_key = get_function_tool_lookup_key_for_call(output)
            func_tool = function_map.get(lookup_key) if lookup_key is not None else None
            if func_tool is None:
                if output_schema is not None and output.name == "json_tool_call":
                    synthetic_tool = build_litellm_json_tool_call(output)
                    ensure_tool_caller_allowed(
                        tool_call=output,
                        allowed_callers=None,
                        tool_name=synthetic_tool.name,
                        agent_name=agent.name,
                    )
                    items.append(
                        ToolCallItem(
                            raw_item=output,
                            agent=agent,
                            description=synthetic_tool.description,
                            tool_origin=get_function_tool_origin(synthetic_tool),
                        )
                    )
                    functions.append(
                        ToolRunFunction(
                            tool_call=output,
                            function_tool=synthetic_tool,
                        )
                    )
                    continue
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Tool not found",
                        data={"tool_name": qualified_output_name or output.name},
                    )
                )
                if run_config is not None and (
                    run_config.tool_not_found_behavior == "return_error_to_model"
                ):
                    tool_name = qualified_output_name or output.name
                    items.append(ToolCallItem(raw_item=output, agent=agent))
                    function_tools_not_found.append(
                        ToolRunFunctionNotFound(tool_call=output, tool_name=tool_name)
                    )
                    continue
                error = (
                    f"Tool {qualified_output_name or output.name} not found in agent {agent.name}"
                )
                raise ModelBehaviorError(error)

            ensure_tool_caller_allowed(
                tool_call=output,
                allowed_callers=func_tool.allowed_callers,
                tool_name=qualified_output_name or func_tool.name,
                agent_name=agent.name,
            )
            items.append(
                ToolCallItem(
                    raw_item=output,
                    agent=agent,
                    description=func_tool.description,
                    title=func_tool._mcp_title,
                    tool_origin=get_function_tool_origin(func_tool),
                )
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
        custom_tool_calls=custom_tool_calls,
        local_shell_calls=local_shell_calls,
        shell_calls=shell_calls,
        apply_patch_calls=apply_patch_calls,
        tools_used=tools_used,
        mcp_approval_requests=mcp_approval_requests,
        interruptions=[],
        function_tools_not_found=function_tools_not_found,
    )


def _preflight_response_invocations_after_processing_error(
    *,
    response: ModelResponse,
    all_tools: Sequence[Tool],
    handoffs: Sequence[Handoff],
    context_wrapper: RunContextWrapper[Any],
) -> None:
    """Reject call-ID reuse before reporting an unrelated response-processing error."""
    handoff_map = {handoff.tool_name: handoff for handoff in handoffs}
    function_map = build_function_tool_lookup_map(
        [tool for tool in all_tools if isinstance(tool, FunctionTool)]
    )
    custom_tool_map = {tool.name: tool for tool in all_tools if isinstance(tool, CustomTool)}
    computer_tool = next((tool for tool in all_tools if isinstance(tool, ComputerTool)), None)
    local_shell_tool = next((tool for tool in all_tools if isinstance(tool, LocalShellTool)), None)
    shell_tool = next((tool for tool in all_tools if isinstance(tool, ShellTool)), None)
    apply_patch_tool = next((tool for tool in all_tools if isinstance(tool, ApplyPatchTool)), None)
    response_identities: dict[str, tuple[str, str, str, str] | None] = {}

    for output in response.output:
        raw_item: Any = output
        output_type = get_mapping_or_attr(output, "type")
        tool_lookup_key: FunctionToolLookupKey | None = None
        tool_name: str | None = None
        invocation_role: str | None = None

        if isinstance(output, ResponseFunctionToolCall):
            tool_lookup_key = get_function_tool_lookup_key_for_call(output)
            if is_handoff_tool_call(output, handoff_map):
                invocation_role = "handoff"
            elif (
                is_apply_patch_name(output.name, apply_patch_tool)
                and tool_lookup_key not in function_map
            ):
                raw_item = normalize_apply_patch_fallback_call(output) or output
                tool_name = apply_patch_tool.name if apply_patch_tool is not None else None
        elif isinstance(output, ResponseCustomToolCall):
            custom_tool = custom_tool_map.get(output.name)
            if custom_tool is not None:
                tool_name = custom_tool.name
            elif is_apply_patch_name(output.name, apply_patch_tool):
                raw_item = normalize_apply_patch_fallback_call(output) or output
                tool_name = apply_patch_tool.name if apply_patch_tool is not None else None
        elif output_type == "shell_call":
            tool_name = shell_tool.name if shell_tool is not None else None
        elif isinstance(output, LocalShellCall):
            selected_shell_tool = local_shell_tool if local_shell_tool is not None else shell_tool
            tool_name = selected_shell_tool.name if selected_shell_tool is not None else None
        elif output_type == "apply_patch_call":
            tool_name = apply_patch_tool.name if apply_patch_tool is not None else None
        elif isinstance(output, ResponseComputerToolCall):
            tool_name = computer_tool.name if computer_tool is not None else None

        call_identity = tool_invocation_call_id(raw_item)
        if call_identity is None:
            continue
        _, call_id = call_identity
        if call_id is None:
            raise ModelBehaviorError(
                "Tool invocations require a non-empty string call ID before execution."
            )
        identity = tool_invocation_identity_and_scope(
            raw_item,
            tool_lookup_key=tool_lookup_key,
            tool_name=tool_name,
            invocation_role=invocation_role,
        )
        if call_id in response_identities:
            previous_identity = response_identities[call_id]
            if previous_identity is None or identity is None or previous_identity != identity:
                raise ModelBehaviorError(
                    "Model reused a tool call ID for a different invocation in one response. "
                    "Use a unique call ID for each tool invocation."
                )
        response_identities[call_id] = identity

        record = context_wrapper._tool_invocations.get(call_id)
        if record is None:
            continue
        if identity is None:
            if not isinstance(output, McpApprovalRequest):
                raise ModelBehaviorError(
                    "Model reused a tool call ID for a different invocation. "
                    "Use a unique call ID for each tool invocation."
                )
            continue
        invocation_type, _, approval_scope, fingerprint = identity
        if (
            record.invocation_type != invocation_type
            or record.approval_scope != approval_scope
            or record.fingerprint != fingerprint
        ):
            raise ModelBehaviorError(
                "Model reused a tool call ID for a different invocation. "
                "Use a unique call ID for each tool invocation."
            )


async def get_single_step_result_from_response(
    *,
    bindings: AgentBindings[TContext],
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
    error_handlers: RunErrorHandlers[TContext] | None = None,
    server_manages_conversation: bool = False,
    after_invocation_validation: Callable[[ProcessedResponse | None], Awaitable[bool]]
    | None = None,
    before_side_effects: Callable[[], Awaitable[None]] | None = None,
    run_state: RunState[Any] | None = None,
) -> SingleStepResult:
    item_agent = bindings.public_agent
    try:
        processed_response = process_model_response(
            agent=item_agent,
            all_tools=all_tools,
            response=new_response,
            output_schema=output_schema,
            handoffs=handoffs,
            existing_items=pre_step_items,
            run_config=run_config,
            server_manages_conversation=server_manages_conversation,
            server_managed_input_items=(
                ItemHelpers.input_to_new_input_list(original_input)
                if server_manages_conversation
                else None
            ),
        )
    except ModelBehaviorError:
        _preflight_response_invocations_after_processing_error(
            response=new_response,
            all_tools=all_tools,
            handoffs=handoffs,
            context_wrapper=context_wrapper,
        )
        if after_invocation_validation is not None:
            await after_invocation_validation(None)
        raise

    _register_tool_call_items(
        context_wrapper,
        pre_step_items,
        validate_invocations=False,
    )
    skipped_raw_item_ids = _dedupe_processed_response_invocations(
        processed_response,
        context_wrapper=context_wrapper,
        existing_items=pre_step_items,
    )

    if after_invocation_validation is not None:
        await after_invocation_validation(processed_response)

    if before_side_effects is not None:
        await before_side_effects()

    tool_use_tracker.record_processed_response(item_agent, processed_response)

    return await execute_tools_and_side_effects(
        bindings=bindings,
        original_input=original_input,
        pre_step_items=pre_step_items,
        new_response=new_response,
        processed_response=processed_response,
        output_schema=output_schema,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
        error_handlers=error_handlers,
        server_manages_conversation=server_manages_conversation,
        precomputed_skipped_raw_item_ids=skipped_raw_item_ids,
        run_state=run_state,
    )

"""
Run-loop orchestration helpers used by the Agent runner. This module coordinates tool execution,
approvals, and turn processing; all symbols here are internal and not part of the public SDK.
"""

from __future__ import annotations

import asyncio
import dataclasses as _dc
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar, cast

from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemDoneEvent,
)
from openai.types.responses.response_output_item import McpCall, McpListTools, ResponseOutputItem
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from .._mcp_tool_metadata import collect_mcp_list_tools_metadata
from .._tool_identity import (
    NamedToolLookupKey,
    build_function_tool_lookup_map,
    get_function_tool_lookup_key_for_call,
    get_tool_trace_name_for_tool,
)
from ..agent import Agent
from ..agent_output import AgentOutputSchemaBase
from ..exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    OutputGuardrailTripwireTriggered,
    RunErrorDetails,
    UserError,
)
from ..handoffs import Handoff
from ..items import (
    HandoffCallItem,
    ItemHelpers,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallItemTypes,
    ToolSearchCallItem,
    ToolSearchOutputItem,
    TResponseInputItem,
    coerce_tool_search_call_raw_item,
    coerce_tool_search_output_raw_item,
)
from ..lifecycle import RunHooks
from ..logger import logger
from ..memory import Session
from ..result import RunResultStreaming
from ..run_config import ReasoningItemIdPolicy, RunConfig
from ..run_context import AgentHookContext, RunContextWrapper, TContext
from ..run_error_handlers import RunErrorHandlers
from ..run_state import RunState
from ..sandbox.runtime import SandboxRuntime
from ..stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)
from ..tool import (
    FunctionTool,
    Tool,
    ToolOrigin,
    ToolOriginType,
    dispose_resolved_computers,
    get_function_tool_origin,
)
from ..tracing import Span, SpanError, agent_span, get_current_trace, task_span, turn_span
from ..tracing.model_tracing import get_model_tracing_impl
from ..tracing.span_data import AgentSpanData, TaskSpanData
from ..usage import Usage
from ..util import _coro, _error_tracing
from .agent_bindings import AgentBindings, bind_public_agent
from .agent_runner_helpers import (
    apply_resumed_conversation_settings,
    attach_usage_to_span,
    get_unsent_tool_call_ids_for_interrupted_state,
    snapshot_usage,
    usage_delta,
)
from .approvals import approvals_from_step
from .error_handlers import (
    build_run_error_data,
    create_message_output_item,
    format_final_output_text,
    resolve_run_error_handler_result,
    validate_handler_final_output,
)
from .guardrails import (
    input_guardrail_tripwire_triggered_for_stream,
    run_input_guardrails,
    run_input_guardrails_with_queue,
    run_output_guardrails,
    run_single_input_guardrail,
    run_single_output_guardrail,
)
from .items import (
    REJECTION_MESSAGE,
    copy_input_items,
    deduplicate_input_items_preferring_latest,
    ensure_input_item_format,
    normalize_resumed_input,
    prepare_model_input_items,
    run_items_to_input_items,
)
from .model_retry import (
    apply_retry_attempt_usage,
    get_response_with_retry,
    stream_response_with_retry,
)
from .oai_conversation import OpenAIServerConversationTracker
from .prompt_cache_key import PromptCacheKeyResolver, model_settings_with_prompt_cache_key
from .run_steps import (
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
    persist_session_items_for_guardrail_trip,
    prepare_input_with_session,
    resumed_turn_items,
    rewind_session_items,
    save_result_to_session,
    save_resumed_turn_items,
    session_items_for_turn,
    update_run_state_after_resume,
)
from .streaming import stream_step_items_to_queue, stream_step_result_to_queue
from .tool_actions import ApplyPatchAction, ComputerAction, LocalShellAction, ShellAction
from .tool_execution import (
    build_litellm_json_tool_call,
    coerce_shell_call,
    execute_apply_patch_calls,
    execute_computer_actions,
    execute_function_tool_calls,
    execute_local_shell_calls,
    execute_shell_calls,
    extract_tool_call_id,
    initialize_computer_tools,
    maybe_reset_tool_choice,
    normalize_shell_output,
    serialize_shell_output,
)
from .tool_planning import execute_mcp_approval_requests
from .tool_use_tracker import (
    TOOL_CALL_TYPES,
    AgentToolUseTracker,
    hydrate_tool_use_tracker,
    serialize_tool_use_tracker,
)
from .turn_preparation import (
    get_all_tools,
    get_handoffs,
    get_model,
    get_output_schema,
    maybe_filter_model_input,
    validate_run_hooks,
)
from .turn_resolution import (
    check_for_final_output_from_tools,
    execute_final_output,
    execute_handoffs,
    execute_tools_and_side_effects,
    get_single_step_result_from_response,
    process_model_response,
    resolve_interrupted_turn,
    run_final_output_hooks,
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


def _should_attach_generic_agent_error(exc: Exception) -> bool:
    return not isinstance(
        exc,
        ModelBehaviorError | InputGuardrailTripwireTriggered | OutputGuardrailTripwireTriggered,
    )


async def _should_persist_stream_items(
    *,
    session: Session | None,
    server_conversation_tracker: OpenAIServerConversationTracker | None,
    streamed_result: RunResultStreaming,
) -> bool:
    if session is None or server_conversation_tracker is not None:
        return False
    should_skip_session_save = await input_guardrail_tripwire_triggered_for_stream(streamed_result)
    return should_skip_session_save is False


def _prepare_turn_input_items(
    caller_input: str | list[TResponseInputItem],
    generated_items: list[RunItem],
    reasoning_item_id_policy: ReasoningItemIdPolicy | None,
) -> list[TResponseInputItem]:
    caller_items = ItemHelpers.input_to_new_input_list(caller_input)
    continuation_items = run_items_to_input_items(generated_items, reasoning_item_id_policy)
    return prepare_model_input_items(caller_items, continuation_items)


def _complete_stream_interruption(
    streamed_result: RunResultStreaming,
    *,
    interruptions: list[ToolApprovalItem],
    processed_response: ProcessedResponse | None,
) -> None:
    streamed_result.interruptions = interruptions
    streamed_result._last_processed_response = processed_response
    streamed_result.is_complete = True
    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())


async def _save_resumed_stream_items(
    *,
    session: Session | None,
    server_conversation_tracker: OpenAIServerConversationTracker | None,
    streamed_result: RunResultStreaming,
    run_state: RunState | None,
    items: list[RunItem],
    response_id: str | None,
    store: bool | None = None,
) -> None:
    if not await _should_persist_stream_items(
        session=session,
        server_conversation_tracker=server_conversation_tracker,
        streamed_result=streamed_result,
    ):
        return
    streamed_result._current_turn_persisted_item_count = await save_resumed_turn_items(
        session=session,
        items=items,
        persisted_count=streamed_result._current_turn_persisted_item_count,
        response_id=response_id,
        reasoning_item_id_policy=streamed_result._reasoning_item_id_policy,
        store=store,
    )
    if run_state is not None:
        run_state._current_turn_persisted_item_count = (
            streamed_result._current_turn_persisted_item_count
        )


async def _save_stream_items(
    *,
    session: Session | None,
    server_conversation_tracker: OpenAIServerConversationTracker | None,
    streamed_result: RunResultStreaming,
    run_state: RunState | None,
    items: list[RunItem],
    response_id: str | None,
    update_persisted_count: bool,
    store: bool | None = None,
) -> None:
    if not await _should_persist_stream_items(
        session=session,
        server_conversation_tracker=server_conversation_tracker,
        streamed_result=streamed_result,
    ):
        return
    await save_result_to_session(
        session,
        [],
        list(items),
        run_state,
        response_id=response_id,
        store=store,
    )
    if update_persisted_count and streamed_result._state is not None:
        streamed_result._current_turn_persisted_item_count = (
            streamed_result._state._current_turn_persisted_item_count
        )


async def _run_output_guardrails_for_stream(
    *,
    agent: Agent[TContext],
    run_config: RunConfig,
    output: Any,
    context_wrapper: RunContextWrapper[TContext],
    streamed_result: RunResultStreaming,
) -> list[Any]:
    streamed_result._output_guardrails_task = asyncio.create_task(
        run_output_guardrails(
            agent.output_guardrails + (run_config.output_guardrails or []),
            agent,
            output,
            context_wrapper,
        )
    )

    try:
        return cast(list[Any], await streamed_result._output_guardrails_task)
    except OutputGuardrailTripwireTriggered:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("Unexpected error in output guardrails", exc_info=True)
        return []


async def _finalize_streamed_final_output(
    *,
    streamed_result: RunResultStreaming,
    agent: Agent[TContext],
    run_config: RunConfig,
    output: Any,
    context_wrapper: RunContextWrapper[TContext],
    save_items: Callable[[list[RunItem], str | None, bool | None], Awaitable[None]],
    items: list[RunItem],
    response_id: str | None,
    store_setting: bool | None,
) -> None:
    output_guardrail_results = await _run_output_guardrails_for_stream(
        agent=agent,
        run_config=run_config,
        output=output,
        context_wrapper=context_wrapper,
        streamed_result=streamed_result,
    )
    streamed_result.output_guardrail_results = output_guardrail_results
    streamed_result.final_output = output
    streamed_result.is_complete = True

    await save_items(items, response_id, store_setting)

    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())


async def _finalize_streamed_interruption(
    *,
    streamed_result: RunResultStreaming,
    save_items: Callable[[list[RunItem], str | None, bool | None], Awaitable[None]],
    items: list[RunItem],
    response_id: str | None,
    store_setting: bool | None,
    interruptions: list[ToolApprovalItem],
    processed_response: ProcessedResponse | None,
) -> None:
    await save_items(items, response_id, store_setting)
    _complete_stream_interruption(
        streamed_result,
        interruptions=interruptions,
        processed_response=processed_response,
    )


T = TypeVar("T")


async def start_streaming(
    starting_input: str | list[TResponseInputItem],
    streamed_result: RunResultStreaming,
    starting_agent: Agent[TContext],
    max_turns: int,
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    error_handlers: RunErrorHandlers[TContext] | None,
    previous_response_id: str | None,
    auto_previous_response_id: bool,
    conversation_id: str | None,
    session: Session | None,
    run_state: RunState[TContext] | None = None,
    *,
    is_resumed_state: bool = False,
    sandbox_runtime: SandboxRuntime[TContext] | None = None,
):
    """Run the streaming loop for a run result."""
    if streamed_result.trace:
        streamed_result.trace.start(mark_as_current=True)
    if run_state is not None:
        run_state.set_trace(get_current_trace() or streamed_result.trace)
        streamed_result._trace_state = run_state._trace_state

    if is_resumed_state and run_state is not None:
        (
            conversation_id,
            previous_response_id,
            auto_previous_response_id,
        ) = apply_resumed_conversation_settings(
            run_state=run_state,
            conversation_id=conversation_id,
            previous_response_id=previous_response_id,
            auto_previous_response_id=auto_previous_response_id,
        )

    current_trace = streamed_result.trace or get_current_trace()
    current_task_span: Span[TaskSpanData] | None = (
        task_span(name=current_trace.name) if current_trace else None
    )
    if current_task_span:
        current_task_span.start(mark_as_current=True)
    task_usage_start = snapshot_usage(context_wrapper.usage)

    try:
        resolved_reasoning_item_id_policy: ReasoningItemIdPolicy | None = (
            run_config.reasoning_item_id_policy
            if run_config.reasoning_item_id_policy is not None
            else (run_state._reasoning_item_id_policy if run_state is not None else None)
        )
        if run_state is not None:
            run_state._reasoning_item_id_policy = resolved_reasoning_item_id_policy
        streamed_result._reasoning_item_id_policy = resolved_reasoning_item_id_policy

        if (
            conversation_id is not None
            or previous_response_id is not None
            or auto_previous_response_id
        ):
            server_conversation_tracker = OpenAIServerConversationTracker(
                conversation_id=conversation_id,
                previous_response_id=previous_response_id,
                auto_previous_response_id=auto_previous_response_id,
                reasoning_item_id_policy=resolved_reasoning_item_id_policy,
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
            run_state._reasoning_item_id_policy = resolved_reasoning_item_id_policy
            streamed_result._state = run_state
        elif streamed_result._state is None:
            streamed_result._state = run_state
        if run_state is not None:
            streamed_result._model_input_items = list(run_state._generated_items)
            # Streamed follow-ups need the same normalized replay signal as sync runs when the
            # runner's continuation differs from the richer session history.
            streamed_result._replay_from_model_input_items = list(
                run_state._generated_items
            ) != list(run_state._session_items)

        if run_state is not None:
            run_state._conversation_id = conversation_id
            run_state._previous_response_id = previous_response_id
            run_state._auto_previous_response_id = auto_previous_response_id
        streamed_result._conversation_id = conversation_id
        streamed_result._previous_response_id = previous_response_id
        streamed_result._auto_previous_response_id = auto_previous_response_id
        prompt_cache_key_resolver = PromptCacheKeyResolver.from_run_state(
            run_state=run_state,
        )

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
                unsent_tool_call_ids=get_unsent_tool_call_ids_for_interrupted_state(run_state),
            )

        streamed_result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=current_agent))

        prepared_input: str | list[TResponseInputItem]
        if is_resumed_state and run_state is not None:
            prepared_input = normalize_resumed_input(starting_input)
            streamed_result.input = prepared_input
            streamed_result._original_input_for_persistence = []
            streamed_result._stream_input_persisted = True
        else:
            server_manages_conversation = server_conversation_tracker is not None
            prepared_input, session_items_snapshot = await prepare_input_with_session(
                starting_input,
                session,
                run_config.session_input_callback,
                run_config.session_settings,
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

        async def _save_resumed_items(
            items: list[RunItem], response_id: str | None, store_setting: bool | None
        ) -> None:
            await _save_resumed_stream_items(
                session=session,
                server_conversation_tracker=server_conversation_tracker,
                streamed_result=streamed_result,
                run_state=run_state,
                items=items,
                response_id=response_id,
                store=store_setting,
            )

        async def _save_stream_items_with_count(
            items: list[RunItem], response_id: str | None, store_setting: bool | None
        ) -> None:
            await _save_stream_items(
                session=session,
                server_conversation_tracker=server_conversation_tracker,
                streamed_result=streamed_result,
                run_state=run_state,
                items=items,
                response_id=response_id,
                update_persisted_count=True,
                store=store_setting,
            )

        async def _save_stream_items_without_count(
            items: list[RunItem], response_id: str | None, store_setting: bool | None
        ) -> None:
            await _save_stream_items(
                session=session,
                server_conversation_tracker=server_conversation_tracker,
                streamed_result=streamed_result,
                run_state=run_state,
                items=items,
                response_id=response_id,
                update_persisted_count=False,
                store=store_setting,
            )
    except BaseException:
        if current_task_span:
            attach_usage_to_span(
                current_task_span,
                usage_delta(task_usage_start, context_wrapper.usage),
            )
            current_task_span.finish(reset_current=True)
        if streamed_result.trace:
            streamed_result.trace.finish(reset_current=True)
        if not streamed_result.is_complete:
            streamed_result.is_complete = True
            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
        raise

    try:
        while True:
            all_input_guardrails = (
                starting_agent.input_guardrails + (run_config.input_guardrails or [])
                if current_turn == 0 and not is_resumed_state
                else []
            )
            sequential_guardrails = [g for g in all_input_guardrails if not g.run_in_parallel]
            parallel_guardrails = [g for g in all_input_guardrails if g.run_in_parallel]
            current_bindings = bind_public_agent(current_agent)
            execution_agent = current_bindings.execution_agent
            prepared_turn_input = copy_input_items(streamed_result.input)
            if sandbox_runtime is not None and sandbox_runtime.enabled and sequential_guardrails:
                # Mirror the non-streaming path: a blocking first-turn guardrail should fire
                # before sandbox prep can create, start, or mutate sandbox state.
                existing_input_guardrail_count = len(streamed_result.input_guardrail_results)
                await run_input_guardrails_with_queue(
                    starting_agent,
                    sequential_guardrails,
                    ItemHelpers.input_to_new_input_list(prepared_turn_input),
                    context_wrapper,
                    streamed_result,
                    None,
                )
                for result in streamed_result.input_guardrail_results[
                    existing_input_guardrail_count:
                ]:
                    if result.output.tripwire_triggered:
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        session_input_items_for_persistence = (
                            await persist_session_items_for_guardrail_trip(
                                session,
                                server_conversation_tracker,
                                session_input_items_for_persistence,
                                starting_input,
                                run_state,
                                store=current_agent.model_settings.resolve(
                                    run_config.model_settings
                                ).store,
                            )
                        )
                        raise InputGuardrailTripwireTriggered(result)
                sequential_guardrails = []

            if sandbox_runtime is not None:
                prepared_sandbox = await sandbox_runtime.prepare_agent(
                    current_agent=current_agent,
                    current_input=prepared_turn_input,
                    context_wrapper=context_wrapper,
                    is_resumed_state=is_resumed_state,
                )
                current_bindings = prepared_sandbox.bindings
                execution_agent = current_bindings.execution_agent
                prepared_turn_input = copy_input_items(prepared_sandbox.input)
                streamed_result.input = prepared_turn_input
                streamed_result._original_input = copy_input_items(prepared_turn_input)
                if run_state is not None:
                    run_state._original_input = copy_input_items(prepared_turn_input)
                sandbox_runtime.apply_result_metadata(streamed_result)

            if is_resumed_state and run_state is not None and run_state._current_step is not None:
                if isinstance(run_state._current_step, NextStepInterruption):
                    if not run_state._model_responses or not run_state._last_processed_response:
                        raise UserError("No model response found in previous state")

                    last_model_response = run_state._model_responses[-1]

                    turn_result = await resolve_interrupted_turn(
                        bindings=current_bindings,
                        original_input=run_state._original_input,
                        original_pre_step_items=run_state._generated_items,
                        new_response=last_model_response,
                        processed_response=run_state._last_processed_response,
                        hooks=hooks,
                        context_wrapper=context_wrapper,
                        run_config=run_config,
                        server_manages_conversation=server_conversation_tracker is not None,
                        run_state=run_state,
                    )

                    tool_use_tracker.record_processed_response(
                        current_agent, run_state._last_processed_response
                    )
                    streamed_result._tool_use_tracker_snapshot = serialize_tool_use_tracker(
                        tool_use_tracker,
                        starting_agent=(
                            run_state._starting_agent
                            if run_state is not None and run_state._starting_agent is not None
                            else starting_agent
                        ),
                    )

                    streamed_result.input = turn_result.original_input
                    streamed_result._original_input = copy_input_items(turn_result.original_input)
                    generated_items, turn_session_items = resumed_turn_items(turn_result)
                    base_session_items = (
                        list(run_state._session_items) if run_state is not None else []
                    )
                    streamed_result._model_input_items = generated_items
                    streamed_result.new_items = base_session_items + list(turn_session_items)
                    streamed_result._replay_from_model_input_items = list(
                        streamed_result._model_input_items
                    ) != list(streamed_result.new_items)
                    if run_state is not None:
                        update_run_state_after_resume(
                            run_state,
                            turn_result=turn_result,
                            generated_items=generated_items,
                            session_items=streamed_result.new_items,
                        )
                        run_state._current_turn_persisted_item_count = (
                            streamed_result._current_turn_persisted_item_count
                        )

                    stream_step_items_to_queue(
                        list(turn_session_items), streamed_result._event_queue
                    )
                    store_setting = current_agent.model_settings.resolve(
                        run_config.model_settings
                    ).store

                    if isinstance(turn_result.next_step, NextStepInterruption):
                        await _finalize_streamed_interruption(
                            streamed_result=streamed_result,
                            save_items=_save_resumed_items,
                            items=list(turn_session_items),
                            response_id=turn_result.model_response.response_id,
                            store_setting=store_setting,
                            interruptions=approvals_from_step(turn_result.next_step),
                            processed_response=run_state._last_processed_response,
                        )
                        break

                    if isinstance(turn_result.next_step, NextStepHandoff):
                        current_agent = turn_result.next_step.new_agent
                        if run_state is not None:
                            run_state._current_agent = current_agent
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
                        await _finalize_streamed_final_output(
                            streamed_result=streamed_result,
                            agent=current_agent,
                            run_config=run_config,
                            output=turn_result.next_step.output,
                            context_wrapper=context_wrapper,
                            save_items=_save_resumed_items,
                            items=list(turn_session_items),
                            response_id=turn_result.model_response.response_id,
                            store_setting=store_setting,
                        )
                        break

                    if isinstance(turn_result.next_step, NextStepRunAgain):
                        await _save_resumed_items(
                            list(turn_session_items),
                            turn_result.model_response.response_id,
                            store_setting,
                        )
                        run_state._current_step = NextStepRunAgain()  # type: ignore[assignment]
                        continue

                    run_state._current_step = None

            if streamed_result._cancel_mode == "after_turn":
                streamed_result.is_complete = True
                streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                break

            if streamed_result.is_complete:
                break

            all_tools = await get_all_tools(execution_agent, context_wrapper)
            await initialize_computer_tools(tools=all_tools, context_wrapper=context_wrapper)

            if current_span is None:
                handoff_names = [
                    h.agent_name for h in await get_handoffs(execution_agent, context_wrapper)
                ]
                if output_schema := get_output_schema(execution_agent):
                    output_type_name = output_schema.name()
                else:
                    output_type_name = "str"

                current_span = agent_span(
                    name=current_agent.name,
                    handoffs=handoff_names,
                    output_type=output_type_name,
                )
                current_span.start(mark_as_current=True)
                tool_names = [
                    tool_name
                    for tool in all_tools
                    if (tool_name := get_tool_trace_name_for_tool(tool)) is not None
                ]
                current_span.span_data.tools = tool_names

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
                max_turns_error = MaxTurnsExceeded(f"Max turns ({max_turns}) exceeded")
                handler_configured = bool(
                    error_handlers and error_handlers.get("max_turns") is not None
                )
                if handler_configured:
                    streamed_result._max_turns_handled = True
                run_error_data = build_run_error_data(
                    input=streamed_result.input,
                    new_items=streamed_result.new_items,
                    raw_responses=streamed_result.raw_responses,
                    last_agent=current_agent,
                    reasoning_item_id_policy=streamed_result._reasoning_item_id_policy,
                )
                handler_result = await resolve_run_error_handler_result(
                    error_handlers=error_handlers,
                    error=max_turns_error,
                    context_wrapper=context_wrapper,
                    run_data=run_error_data,
                )
                if handler_result is None:
                    if handler_configured:
                        streamed_result._max_turns_handled = False
                    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                    break

                validated_output = validate_handler_final_output(
                    current_agent, handler_result.final_output
                )
                output_text = format_final_output_text(current_agent, validated_output)
                synthesized_item = create_message_output_item(current_agent, output_text)
                include_in_history = handler_result.include_in_history
                if include_in_history:
                    streamed_result._model_input_items.append(synthesized_item)
                    streamed_result.new_items.append(synthesized_item)
                    if run_state is not None:
                        run_state._generated_items = list(streamed_result._model_input_items)
                        run_state._clear_generated_items_last_processed_marker()
                        run_state._session_items = list(streamed_result.new_items)
                    stream_step_items_to_queue([synthesized_item], streamed_result._event_queue)
                    store_setting = current_agent.model_settings.resolve(
                        run_config.model_settings
                    ).store
                    if is_resumed_state:
                        await _save_resumed_items([synthesized_item], None, store_setting)
                    else:
                        await _save_stream_items_with_count([synthesized_item], None, store_setting)

                await run_final_output_hooks(
                    current_agent, hooks, context_wrapper, validated_output
                )
                output_guardrail_results = await _run_output_guardrails_for_stream(
                    agent=current_agent,
                    run_config=run_config,
                    output=validated_output,
                    context_wrapper=context_wrapper,
                    streamed_result=streamed_result,
                )
                streamed_result.output_guardrail_results = output_guardrail_results
                streamed_result.final_output = validated_output
                streamed_result.is_complete = True
                streamed_result._stored_exception = None
                streamed_result._max_turns_handled = True
                streamed_result.current_turn = max_turns
                if run_state is not None:
                    run_state._current_turn = max_turns
                    run_state._current_step = None
                streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                break

            if current_turn == 1:
                if sequential_guardrails:
                    await run_input_guardrails_with_queue(
                        starting_agent,
                        sequential_guardrails,
                        ItemHelpers.input_to_new_input_list(prepared_turn_input),
                        context_wrapper,
                        streamed_result,
                        current_span,
                    )
                    for result in streamed_result.input_guardrail_results:
                        if result.output.tripwire_triggered:
                            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                            session_input_items_for_persistence = (
                                await persist_session_items_for_guardrail_trip(
                                    session,
                                    server_conversation_tracker,
                                    session_input_items_for_persistence,
                                    starting_input,
                                    run_state,
                                    store=current_agent.model_settings.resolve(
                                        run_config.model_settings
                                    ).store,
                                )
                            )
                            raise InputGuardrailTripwireTriggered(result)

                if parallel_guardrails:
                    streamed_result._input_guardrails_task = asyncio.create_task(
                        run_input_guardrails_with_queue(
                            starting_agent,
                            parallel_guardrails,
                            ItemHelpers.input_to_new_input_list(prepared_turn_input),
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
                turn_usage_start = snapshot_usage(context_wrapper.usage)
                current_turn_span = turn_span(
                    turn=current_turn,
                    agent_name=current_agent.name,
                )
                current_turn_span.start(mark_as_current=True)
                try:
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
                        current_bindings,
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
                        reasoning_item_id_policy=resolved_reasoning_item_id_policy,
                        prompt_cache_key_resolver=prompt_cache_key_resolver,
                    )
                finally:
                    attach_usage_to_span(
                        current_turn_span,
                        usage_delta(turn_usage_start, context_wrapper.usage),
                    )
                    current_turn_span.finish(reset_current=True)
                logger.debug(
                    "Turn %s complete, next_step type=%s",
                    current_turn,
                    type(turn_result.next_step).__name__,
                )
                should_run_agent_start_hooks = False
                streamed_result._tool_use_tracker_snapshot = serialize_tool_use_tracker(
                    tool_use_tracker,
                    starting_agent=(
                        run_state._starting_agent
                        if run_state is not None and run_state._starting_agent is not None
                        else starting_agent
                    ),
                )

                streamed_result.raw_responses = streamed_result.raw_responses + [
                    turn_result.model_response
                ]
                streamed_result.input = turn_result.original_input
                if isinstance(turn_result.next_step, NextStepHandoff):
                    streamed_result._original_input = copy_input_items(turn_result.original_input)
                    if run_state is not None:
                        run_state._original_input = copy_input_items(turn_result.original_input)
                streamed_result._model_input_items = (
                    turn_result.pre_step_items + turn_result.new_step_items
                )
                turn_session_items = session_items_for_turn(turn_result)
                streamed_result.new_items.extend(turn_session_items)
                streamed_result._replay_from_model_input_items = list(
                    streamed_result._model_input_items
                ) != list(streamed_result.new_items)
                store_setting = current_agent.model_settings.resolve(
                    run_config.model_settings
                ).store
                if server_conversation_tracker is not None:
                    pending_server_items = list(turn_result.new_step_items)

                if isinstance(turn_result.next_step, NextStepRunAgain):
                    streamed_result._current_turn_persisted_item_count = 0
                    if run_state:
                        run_state._current_turn_persisted_item_count = 0

                if server_conversation_tracker is not None:
                    server_conversation_tracker.track_server_items(turn_result.model_response)

                if isinstance(turn_result.next_step, NextStepHandoff):
                    await _save_stream_items_without_count(
                        turn_session_items,
                        turn_result.model_response.response_id,
                        store_setting,
                    )
                    current_agent = turn_result.next_step.new_agent
                    if run_state is not None:
                        run_state._current_agent = current_agent
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
                    await _finalize_streamed_final_output(
                        streamed_result=streamed_result,
                        agent=current_agent,
                        run_config=run_config,
                        output=turn_result.next_step.output,
                        context_wrapper=context_wrapper,
                        save_items=_save_stream_items_with_count,
                        items=turn_session_items,
                        response_id=turn_result.model_response.response_id,
                        store_setting=store_setting,
                    )
                    break
                elif isinstance(turn_result.next_step, NextStepInterruption):
                    processed_response_for_state = turn_result.processed_response
                    if processed_response_for_state is None and run_state is not None:
                        processed_response_for_state = run_state._last_processed_response
                    if run_state is not None:
                        run_state._model_responses = streamed_result.raw_responses
                        run_state._last_processed_response = processed_response_for_state
                        run_state._generated_items = streamed_result._model_input_items
                        run_state._mark_generated_items_merged_with_last_processed()
                        run_state._session_items = list(streamed_result.new_items)
                        run_state._current_step = turn_result.next_step
                        run_state._current_turn = current_turn
                        run_state._current_turn_persisted_item_count = (
                            streamed_result._current_turn_persisted_item_count
                        )
                    await _finalize_streamed_interruption(
                        streamed_result=streamed_result,
                        save_items=_save_stream_items_with_count,
                        items=turn_session_items,
                        response_id=turn_result.model_response.response_id,
                        store_setting=store_setting,
                        interruptions=approvals_from_step(turn_result.next_step),
                        processed_response=processed_response_for_state,
                    )
                    break
                elif isinstance(turn_result.next_step, NextStepRunAgain):
                    if streamed_result._state is not None:
                        streamed_result._state._current_step = NextStepRunAgain()

                    await _save_stream_items_with_count(
                        turn_session_items,
                        turn_result.model_response.response_id,
                        store_setting,
                    )

                    if streamed_result._cancel_mode == "after_turn":  # type: ignore[comparison-overlap]
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        break
            except Exception as e:
                if current_span and _should_attach_generic_agent_error(e):
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
        if current_span and _should_attach_generic_agent_error(e):
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
        if current_task_span:
            attach_usage_to_span(
                current_task_span,
                usage_delta(task_usage_start, context_wrapper.usage),
            )
            current_task_span.finish(reset_current=True)
        if streamed_result.trace:
            streamed_result.trace.finish(reset_current=True)

        if not streamed_result.is_complete:
            streamed_result.is_complete = True
            streamed_result._event_queue.put_nowait(QueueCompleteSentinel())


async def run_single_turn_streamed(
    streamed_result: RunResultStreaming,
    bindings: AgentBindings[TContext],
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
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None,
    prompt_cache_key_resolver: PromptCacheKeyResolver | None = None,
) -> SingleStepResult:
    """Run a single streamed turn and emit events as results arrive."""
    public_agent = bindings.public_agent
    execution_agent = bindings.execution_agent

    async def raise_if_input_guardrail_tripwire_known() -> None:
        tripwire_result = streamed_result._triggered_input_guardrail_result
        if tripwire_result is not None:
            raise InputGuardrailTripwireTriggered(tripwire_result)

        task = streamed_result._input_guardrails_task
        if task is None or not task.done():
            return

        guardrail_exception = task.exception()
        if guardrail_exception is not None:
            raise guardrail_exception

        tripwire_result = streamed_result._triggered_input_guardrail_result
        if tripwire_result is not None:
            raise InputGuardrailTripwireTriggered(tripwire_result)

    emitted_tool_call_ids: set[str] = set()
    emitted_reasoning_item_ids: set[str] = set()
    emitted_tool_search_fingerprints: set[str] = set()
    # Precompute the lookup map used for streaming descriptions. Function tools use the same
    # collision-free lookup keys as runtime dispatch, including deferred top-level aliases.
    tool_map: dict[NamedToolLookupKey, Any] = cast(
        dict[NamedToolLookupKey, Any],
        build_function_tool_lookup_map(
            [tool for tool in all_tools if isinstance(tool, FunctionTool)]
        ),
    )
    for tool in all_tools:
        tool_name = getattr(tool, "name", None)
        if not isinstance(tool_name, str) or not tool_name:
            continue
        if isinstance(tool, FunctionTool):
            continue
        tool_map[tool_name] = tool

    def _tool_search_fingerprint(raw_item: Any) -> str:
        if isinstance(raw_item, Mapping):
            payload: Any = dict(raw_item)
        elif hasattr(raw_item, "model_dump"):
            payload = cast(Any, raw_item).model_dump(exclude_unset=True)
        else:
            payload = {
                "type": getattr(raw_item, "type", None),
                "id": getattr(raw_item, "id", None),
            }
        return json.dumps(payload, sort_keys=True, default=str)

    try:
        turn_input = ItemHelpers.input_to_new_input_list(streamed_result.input)
    except Exception:
        turn_input = []
    context_wrapper.turn_input = list(turn_input)

    if should_run_agent_start_hooks:
        agent_hook_context = AgentHookContext(
            context=context_wrapper.context,
            usage=context_wrapper.usage,
            _approvals=context_wrapper._approvals,
            turn_input=turn_input,
        )
        await asyncio.gather(
            hooks.on_agent_start(agent_hook_context, public_agent),
            (
                public_agent.hooks.on_start(agent_hook_context, public_agent)
                if public_agent.hooks
                else _coro.noop_coroutine()
            ),
        )

    output_schema = get_output_schema(execution_agent)

    streamed_result.current_agent = public_agent
    streamed_result._current_agent_output_schema = get_output_schema(public_agent)

    system_prompt, prompt_config = await asyncio.gather(
        execution_agent.get_system_prompt(context_wrapper),
        execution_agent.get_prompt(context_wrapper),
    )

    handoffs = await get_handoffs(execution_agent, context_wrapper)
    model = get_model(execution_agent, run_config)
    model_settings = execution_agent.model_settings.resolve(run_config.model_settings)
    model_settings = maybe_reset_tool_choice(public_agent, tool_use_tracker, model_settings)

    final_response: ModelResponse | None = None
    streamed_response_output: list[ResponseOutputItem] = []

    if server_conversation_tracker is not None:
        items_for_input = (
            pending_server_items if pending_server_items else streamed_result._model_input_items
        )
        input = server_conversation_tracker.prepare_input(streamed_result.input, items_for_input)
        logger.debug(
            "prepare_input returned %s items; remaining_initial_input=%s",
            len(input),
            len(server_conversation_tracker.remaining_initial_input)
            if server_conversation_tracker.remaining_initial_input
            else 0,
        )
    else:
        input = _prepare_turn_input_items(
            streamed_result.input,
            streamed_result._model_input_items,
            reasoning_item_id_policy,
        )

    filtered = await maybe_filter_model_input(
        agent=public_agent,
        run_config=run_config,
        context_wrapper=context_wrapper,
        input_items=input,
        system_instructions=system_prompt,
    )
    if isinstance(filtered.input, list):
        filtered.input = deduplicate_input_items_preferring_latest(filtered.input)
    hosted_mcp_tool_metadata = collect_mcp_list_tools_metadata(streamed_result._model_input_items)
    if isinstance(filtered.input, list):
        hosted_mcp_tool_metadata.update(collect_mcp_list_tools_metadata(filtered.input))
    if server_conversation_tracker is not None:
        logger.debug(
            "filtered.input has %s items; ids=%s",
            len(filtered.input),
            [id(i) for i in filtered.input],
        )
        # Track only the items actually sent after call_model_input_filter runs. Retry helpers
        # explicitly rewind this state before replaying a failed request.
        server_conversation_tracker.mark_input_as_sent(filtered.input)
    if not filtered.input and server_conversation_tracker is None:
        raise RuntimeError("Prepared model input is empty")

    await asyncio.gather(
        hooks.on_llm_start(context_wrapper, public_agent, filtered.instructions, filtered.input),
        (
            public_agent.hooks.on_llm_start(
                context_wrapper,
                public_agent,
                filtered.instructions,
                filtered.input,
            )
            if public_agent.hooks
            else _coro.noop_coroutine()
        ),
    )

    if (
        not streamed_result._stream_input_persisted
        and session is not None
        and server_conversation_tracker is None
        and streamed_result._original_input_for_persistence is not None
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

    prompt_cache_key = (
        prompt_cache_key_resolver.resolve(
            model_settings,
            model=model,
            conversation_id=conversation_id,
            session=session,
            group_id=run_config.group_id,
        )
        if prompt_cache_key_resolver is not None
        else None
    )
    model_settings = model_settings_with_prompt_cache_key(model_settings, prompt_cache_key)

    async def rewind_model_request() -> None:
        items_to_rewind = session_items_to_rewind if session_items_to_rewind is not None else []
        await rewind_session_items(session, items_to_rewind, server_conversation_tracker)
        if server_conversation_tracker is not None:
            server_conversation_tracker.rewind_input(filtered.input)

    stream_failed_retry_attempts: list[int] = [0]

    retry_stream = stream_response_with_retry(
        get_stream=lambda: model.stream_response(
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
        ),
        rewind=rewind_model_request,
        retry_settings=model_settings.retry,
        get_retry_advice=model.get_retry_advice,
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
        failed_retry_attempts_out=stream_failed_retry_attempts,
    )

    async for event in retry_stream:
        streamed_result._event_queue.put_nowait(RawResponsesStreamEvent(data=event))

        terminal_response: Response | None = None
        is_completed_event = False
        if isinstance(event, ResponseCompletedEvent):
            is_completed_event = True
            terminal_response = event.response
        elif getattr(event, "type", None) in {"response.incomplete", "response.failed"}:
            maybe_response = getattr(event, "response", None)
            if isinstance(maybe_response, Response):
                terminal_response = maybe_response

        if terminal_response is not None:
            if is_completed_event and not terminal_response.output and streamed_response_output:
                # Some streaming backends emit output items during item.done events while leaving
                # the terminal response output empty. Preserve those items so the runner can
                # resolve the completed step correctly.
                terminal_response.output = list(streamed_response_output)
            usage = (
                apply_retry_attempt_usage(
                    Usage(
                        requests=1,
                        input_tokens=terminal_response.usage.input_tokens,
                        output_tokens=terminal_response.usage.output_tokens,
                        total_tokens=terminal_response.usage.total_tokens,
                        input_tokens_details=terminal_response.usage.input_tokens_details,
                        output_tokens_details=terminal_response.usage.output_tokens_details,
                    ),
                    stream_failed_retry_attempts[0],
                )
                if terminal_response.usage
                else Usage()
            )
            final_response = ModelResponse(
                output=terminal_response.output,
                usage=usage,
                response_id=terminal_response.id,
                request_id=getattr(terminal_response, "_request_id", None),
            )

        if isinstance(event, ResponseOutputItemDoneEvent):
            output_item = event.item
            streamed_response_output.append(output_item)
            output_item_type = getattr(output_item, "type", None)

            if output_item_type == "tool_search_call":
                emitted_tool_search_fingerprints.add(_tool_search_fingerprint(output_item))
                streamed_result._event_queue.put_nowait(
                    RunItemStreamEvent(
                        item=ToolSearchCallItem(
                            raw_item=coerce_tool_search_call_raw_item(output_item),
                            agent=public_agent,
                        ),
                        name="tool_search_called",
                    )
                )

            elif output_item_type == "tool_search_output":
                emitted_tool_search_fingerprints.add(_tool_search_fingerprint(output_item))
                streamed_result._event_queue.put_nowait(
                    RunItemStreamEvent(
                        item=ToolSearchOutputItem(
                            raw_item=coerce_tool_search_output_raw_item(output_item),
                            agent=public_agent,
                        ),
                        name="tool_search_output_created",
                    )
                )

            elif isinstance(output_item, McpListTools):
                hosted_mcp_tool_metadata.update(collect_mcp_list_tools_metadata([output_item]))

            elif isinstance(output_item, TOOL_CALL_TYPES):
                output_call_id: str | None = getattr(
                    output_item, "call_id", getattr(output_item, "id", None)
                )

                if (
                    output_call_id
                    and isinstance(output_call_id, str)
                    and output_call_id not in emitted_tool_call_ids
                ):
                    emitted_tool_call_ids.add(output_call_id)

                    # Look up tool description from precomputed map ("last wins" matches
                    # execution behavior in process_model_response).
                    tool_lookup_key = get_function_tool_lookup_key_for_call(output_item)
                    matched_tool = (
                        tool_map.get(tool_lookup_key) if tool_lookup_key is not None else None
                    )
                    if (
                        matched_tool is None
                        and output_schema is not None
                        and isinstance(output_item, ResponseFunctionToolCall)
                        and output_item.name == "json_tool_call"
                    ):
                        matched_tool = build_litellm_json_tool_call(output_item)
                    tool_description: str | None = None
                    tool_title: str | None = None
                    tool_origin = None
                    if isinstance(output_item, McpCall):
                        metadata = hosted_mcp_tool_metadata.get(
                            (output_item.server_label, output_item.name)
                        )
                        if metadata is not None:
                            tool_description = metadata.description
                            tool_title = metadata.title
                        tool_origin = ToolOrigin(
                            type=ToolOriginType.MCP,
                            mcp_server_name=output_item.server_label,
                        )
                    elif matched_tool is not None:
                        tool_description = getattr(matched_tool, "description", None)
                        tool_title = getattr(matched_tool, "_mcp_title", None)
                        tool_origin = get_function_tool_origin(matched_tool)

                    tool_item = ToolCallItem(
                        raw_item=cast(ToolCallItemTypes, output_item),
                        agent=public_agent,
                        description=tool_description,
                        title=tool_title,
                        tool_origin=tool_origin,
                    )
                    streamed_result._event_queue.put_nowait(
                        RunItemStreamEvent(item=tool_item, name="tool_called")
                    )

            elif isinstance(output_item, ResponseReasoningItem):
                reasoning_id: str | None = getattr(output_item, "id", None)

                if reasoning_id and reasoning_id not in emitted_reasoning_item_ids:
                    emitted_reasoning_item_ids.add(reasoning_id)

                    reasoning_item = ReasoningItem(raw_item=output_item, agent=public_agent)
                    streamed_result._event_queue.put_nowait(
                        RunItemStreamEvent(item=reasoning_item, name="reasoning_item_created")
                    )

    if final_response is not None:
        context_wrapper.usage.add(final_response.usage)
        await asyncio.gather(
            (
                public_agent.hooks.on_llm_end(context_wrapper, public_agent, final_response)
                if public_agent.hooks
                else _coro.noop_coroutine()
            ),
            hooks.on_llm_end(context_wrapper, public_agent, final_response),
        )

    if not final_response:
        raise ModelBehaviorError("Model did not produce a final response!")

    if server_conversation_tracker is not None:
        # Streaming uses the same rewind helper, so a successful retry must restore delivered
        # input tracking before the next turn computes server-managed deltas.
        server_conversation_tracker.mark_input_as_sent(filtered.input)
        server_conversation_tracker.track_server_items(final_response)

    single_step_result = await get_single_step_result_from_response(
        bindings=bindings,
        original_input=streamed_result.input,
        pre_step_items=streamed_result._model_input_items,
        new_response=final_response,
        output_schema=output_schema,
        all_tools=all_tools,
        handoffs=handoffs,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
        tool_use_tracker=tool_use_tracker,
        server_manages_conversation=server_conversation_tracker is not None,
        event_queue=streamed_result._event_queue,
        before_side_effects=raise_if_input_guardrail_tripwire_known,
    )

    items_to_filter = session_items_for_turn(single_step_result)

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

    if emitted_tool_search_fingerprints:
        items_to_filter = [
            item
            for item in items_to_filter
            if not (
                isinstance(item, ToolSearchCallItem | ToolSearchOutputItem)
                and _tool_search_fingerprint(item.raw_item) in emitted_tool_search_fingerprints
            )
        ]

    items_to_filter = [item for item in items_to_filter if not isinstance(item, HandoffCallItem)]

    filtered_result = _dc.replace(single_step_result, new_step_items=items_to_filter)
    stream_step_result_to_queue(filtered_result, streamed_result._event_queue)
    return single_step_result


async def run_single_turn(
    *,
    bindings: AgentBindings[TContext],
    all_tools: list[Tool],
    original_input: str | list[TResponseInputItem],
    generated_items: list[RunItem],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    should_run_agent_start_hooks: bool,
    tool_use_tracker: AgentToolUseTracker,
    server_conversation_tracker: OpenAIServerConversationTracker | None = None,
    session: Session | None = None,
    session_items_to_rewind: list[TResponseInputItem] | None = None,
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None,
    prompt_cache_key_resolver: PromptCacheKeyResolver | None = None,
) -> SingleStepResult:
    """Run a single non-streaming turn of the agent loop."""
    public_agent = bindings.public_agent
    execution_agent = bindings.execution_agent
    try:
        turn_input = ItemHelpers.input_to_new_input_list(original_input)
    except Exception:
        turn_input = []
    context_wrapper.turn_input = list(turn_input)

    if should_run_agent_start_hooks:
        agent_hook_context = AgentHookContext(
            context=context_wrapper.context,
            usage=context_wrapper.usage,
            _approvals=context_wrapper._approvals,
            turn_input=turn_input,
        )
        await asyncio.gather(
            hooks.on_agent_start(agent_hook_context, public_agent),
            (
                public_agent.hooks.on_start(agent_hook_context, public_agent)
                if public_agent.hooks
                else _coro.noop_coroutine()
            ),
        )

    system_prompt, prompt_config = await asyncio.gather(
        execution_agent.get_system_prompt(context_wrapper),
        execution_agent.get_prompt(context_wrapper),
    )

    output_schema = get_output_schema(execution_agent)
    handoffs = await get_handoffs(execution_agent, context_wrapper)
    if server_conversation_tracker is not None:
        input = server_conversation_tracker.prepare_input(original_input, generated_items)
    else:
        input = _prepare_turn_input_items(original_input, generated_items, reasoning_item_id_policy)

    new_response = await get_new_response(
        bindings,
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
        prompt_cache_key_resolver=prompt_cache_key_resolver,
    )

    return await get_single_step_result_from_response(
        bindings=bindings,
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
        server_manages_conversation=server_conversation_tracker is not None,
    )


async def get_new_response(
    bindings: AgentBindings[TContext],
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
    prompt_cache_key_resolver: PromptCacheKeyResolver | None = None,
) -> ModelResponse:
    """Call the model and return the raw response, handling retries and hooks."""
    public_agent = bindings.public_agent
    execution_agent = bindings.execution_agent
    filtered = await maybe_filter_model_input(
        agent=public_agent,
        run_config=run_config,
        context_wrapper=context_wrapper,
        input_items=input,
        system_instructions=system_prompt,
    )
    if isinstance(filtered.input, list):
        filtered.input = deduplicate_input_items_preferring_latest(filtered.input)

    model = get_model(execution_agent, run_config)
    model_settings = execution_agent.model_settings.resolve(run_config.model_settings)
    model_settings = maybe_reset_tool_choice(public_agent, tool_use_tracker, model_settings)

    if server_conversation_tracker is not None:
        server_conversation_tracker.mark_input_as_sent(filtered.input)

    await asyncio.gather(
        hooks.on_llm_start(context_wrapper, public_agent, filtered.instructions, filtered.input),
        (
            public_agent.hooks.on_llm_start(
                context_wrapper,
                public_agent,
                filtered.instructions,
                filtered.input,
            )
            if public_agent.hooks
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

    prompt_cache_key = (
        prompt_cache_key_resolver.resolve(
            model_settings,
            model=model,
            conversation_id=conversation_id,
            session=session,
            group_id=run_config.group_id,
        )
        if prompt_cache_key_resolver is not None
        else None
    )
    model_settings = model_settings_with_prompt_cache_key(model_settings, prompt_cache_key)

    async def rewind_model_request() -> None:
        items_to_rewind = session_items_to_rewind if session_items_to_rewind is not None else []
        await rewind_session_items(session, items_to_rewind, server_conversation_tracker)
        if server_conversation_tracker is not None:
            server_conversation_tracker.rewind_input(filtered.input)

    new_response = await get_response_with_retry(
        get_response=lambda: model.get_response(
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
        ),
        rewind=rewind_model_request,
        retry_settings=model_settings.retry,
        get_retry_advice=model.get_retry_advice,
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
    )
    if server_conversation_tracker is not None:
        # Retry helpers rewind sent-input tracking before replaying a failed request. Mark the
        # filtered input as delivered again once a retry succeeds so subsequent turns only send
        # new deltas.
        server_conversation_tracker.mark_input_as_sent(filtered.input)

    context_wrapper.usage.add(new_response.usage)

    await asyncio.gather(
        (
            public_agent.hooks.on_llm_end(context_wrapper, public_agent, new_response)
            if public_agent.hooks
            else _coro.noop_coroutine()
        ),
        hooks.on_llm_end(context_wrapper, public_agent, new_response),
    )

    return new_response

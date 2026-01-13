"""
Run-loop orchestration helpers used by the Agent runner. This module coordinates tool execution,
approvals, and turn processing; all symbols here are internal and not part of the public SDK.
"""

from __future__ import annotations

import asyncio
import dataclasses as _dc
from typing import TypeVar, cast

from openai.types.responses import ResponseCompletedEvent, ResponseOutputItemDoneEvent
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from ..agent import Agent
from ..agent_output import AgentOutputSchemaBase
from ..exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    ModelBehaviorError,
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
    ToolCallItem,
    ToolCallItemTypes,
    TResponseInputItem,
)
from ..lifecycle import RunHooks
from ..logger import logger
from ..memory import Session
from ..result import RunResultStreaming
from ..run_config import RunConfig
from ..run_context import RunContextWrapper, TContext
from ..run_state import RunState
from ..stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)
from ..tool import Tool, dispose_resolved_computers
from ..tracing import Span, SpanError, agent_span
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
    deduplicate_input_items,
    drop_orphan_function_calls,
    ensure_input_item_format,
    normalize_input_items_for_api,
)
from .oai_conversation import OpenAIServerConversationTracker
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
    prepare_input_with_session,
    rewind_session_items,
    save_result_to_session,
)
from .streaming import stream_step_items_to_queue, stream_step_result_to_queue
from .tool_actions import ApplyPatchAction, ComputerAction, LocalShellAction, ShellAction
from .tool_execution import (
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

T = TypeVar("T")


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
    generated_items: list[RunItem],
    hooks: RunHooks[TContext],
    context_wrapper: RunContextWrapper[TContext],
    run_config: RunConfig,
    should_run_agent_start_hooks: bool,
    tool_use_tracker: AgentToolUseTracker,
    server_conversation_tracker: OpenAIServerConversationTracker | None = None,
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
        server_conversation_tracker.mark_input_as_sent(filtered.input)

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

"""
Run-loop orchestration helpers used by the Agent runner. This module coordinates tool execution,
approvals, and turn processing; all symbols here are internal and not part of the public SDK.
"""

from __future__ import annotations

import asyncio
import dataclasses as _dc
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, TypeVar, cast
from uuid import uuid4

from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseOutputItemDoneEvent,
)
from openai.types.responses.response_output_item import ResponseOutputItem
from openai.types.responses.response_prompt_param import ResponsePromptParam

from .._tool_identity import (
    get_tool_trace_name_for_tool,
    resolve_tool_name_collisions,
)
from ..agent import Agent
from ..agent_output import AgentOutputSchemaBase
from ..exceptions import (
    _DATA_REDACTED_ERROR_MESSAGE,
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelBehaviorError,
    OutputGuardrailTripwireTriggered,
    RunErrorDetails,
    UserError,
    _clear_data_redacted_error_traceback,
    _detach_data_redacted_error_traceback,
    _is_error_data_redacted,
    _mark_error_data_redacted,
)
from ..handoffs import Handoff
from ..items import (
    InputItem,
    ItemHelpers,
    ModelResponse,
    RunItem,
    ToolApprovalItem,
    TResponseInputItem,
)
from ..lifecycle import RunHooks
from ..logger import (
    log_model_action_error,
    log_model_action_warning,
    log_model_and_tool_action_debug,
    log_tool_action_warning,
    logger,
)
from ..memory import Session
from ..models._response_terminal import (
    response_error_event_failure_error,
    response_terminal_failure_error,
)
from ..models._run_context import model_run_context, model_run_context_stream
from ..result import RunResultStreaming
from ..run_config import ReasoningItemIdPolicy, RunConfig
from ..run_context import AgentHookContext, RunContextWrapper, TContext
from ..run_error_handlers import RunErrorHandlers
from ..run_state import RunState
from ..sandbox.runtime import SandboxRuntime
from ..stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
)
from ..tool import (
    ProgrammaticToolCallingTool,
    Tool,
    dispose_resolved_computers,
)
from ..tracing import Span, SpanError, agent_span, get_current_trace, task_span, turn_span
from ..tracing.config import include_task_and_turn_spans
from ..tracing.model_tracing import get_model_tracing_impl
from ..tracing.span_data import AgentSpanData, TaskSpanData
from ..usage import (
    Usage,
    _extract_raw_usage_snapshot,
    _requests_for_response_without_usage,
    _response_usage_to_usage,
)
from ..util import _coro, _error_tracing
from ..util._asyncio_tasks import gather_with_cancel
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
    attach_generic_agent_error,
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
    reconcile_nested_history_owned_input_after_rewrite,
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
    _session_get_items,
    admit_pending_input,
    commit_server_pending_input,
    persist_session_items_for_guardrail_trip,
    prepare_input_with_session,
    reconcile_nested_history_owned_session_item_refs,
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
    AgentToolUseTracker,
    hydrate_tool_use_tracker,
    serialize_tool_use_tracker,
)
from .turn_preparation import (
    get_all_tools,
    get_handoffs,
    get_model,
    get_model_settings,
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
    "cleanup_models_after_run",
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

_STREAM_EVENT_ITEM_OCCURRENCE_KEY = "_agents_stream_event_item_occurrence_key"


def _stream_event_item_occurrence_key(item: RunItem) -> str | None:
    key = getattr(item, _STREAM_EVENT_ITEM_OCCURRENCE_KEY, None)
    return key if isinstance(key, str) and key else None


def _ensure_stream_event_item_occurrence_key(item: RunItem) -> str:
    key = _stream_event_item_occurrence_key(item)
    if key is None:
        key = uuid4().hex
        setattr(item, _STREAM_EVENT_ITEM_OCCURRENCE_KEY, key)
    return key


async def cleanup_models_after_run(tool_use_tracker: AgentToolUseTracker) -> None:
    """Notify every model resolved during the run that its owning run has ended."""
    for model in tool_use_tracker.models:
        try:
            await model._cleanup_on_run_end(tool_use_tracker)
        except Exception as error:
            log_model_action_warning(logger, "Failed to clean up model resources after run", error)


def _agent_diagnostic_extra(agent: Agent[Any]) -> dict[str, object]:
    return {"agent_name": agent.name}


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


async def _wait_for_streamed_turn_events_and_stop_if_cancelled(
    streamed_result: RunResultStreaming,
) -> bool:
    """Let consumers process the completed turn before starting another one."""
    await streamed_result._wait_for_turn_event_consumption()
    if streamed_result._cancel_mode != "after_turn":
        return False

    streamed_result.is_complete = True
    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
    return True


def _publish_streamed_result_agent(
    streamed_result: RunResultStreaming,
    agent: Agent[Any],
) -> None:
    """Publish an agent transition before cancellation can complete the streamed run."""
    streamed_result.current_agent = agent
    streamed_result._current_agent_output_schema = get_output_schema(agent)


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
        wrapper=streamed_result.context_wrapper,
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
        wrapper=streamed_result.context_wrapper,
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
    # Recorded as each guardrail completes so a tripwire still publishes the results that
    # already finished, mirroring the non-streamed path.
    completed_results: list[Any] = []
    streamed_result._output_guardrails_task = asyncio.create_task(
        run_output_guardrails(
            agent.output_guardrails + (run_config.output_guardrails or []),
            agent,
            output,
            context_wrapper,
            completed_results,
        )
    )

    try:
        return cast(list[Any], await streamed_result._output_guardrails_task)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Publish at a single boundary so no failure path can omit results that already
        # finished. A guardrail raising a non-tripwire error reports the same completed
        # results a tripwire does.
        streamed_result.output_guardrail_results = (
            streamed_result.output_guardrail_results + completed_results
        )
        if not isinstance(exc, OutputGuardrailTripwireTriggered):
            log_model_action_error(logger, "Unexpected error in output guardrails", exc)
        raise


_SIDE_EFFECT_ITEM_TYPES = frozenset({"tool_call_item", "tool_call_output_item"})


def _reasoning_indexes_tied_to_retained_items(
    items: list[RunItem],
    retained_indexes: set[int],
) -> set[int]:
    """Indexes of the reasoning items whose tied item is being retained.

    Applies the same association rule as
    ``agents.run_internal.items._drop_reasoning_items_preceding_dropped_calls``: a reasoning item
    is tied to the next *non-reasoning* model-emitted item. Keeping a group whose following item is
    dropped would leave a dangling reasoning item, which the Responses API rejects on the next
    request (``reasoning was provided without its required following item``); dropping a group
    whose following item is retained would strip the context that call needs to be replayed.

    A trailing reasoning group - one with no following non-reasoning item at all - is not tied to
    anything retained, so it is dropped. Note this is stricter than the reference, which keeps such
    a group because the item it belongs to may still arrive later in a longer history; here the
    turn is complete, so there is nothing left to tie it to.
    """
    tied: set[int] = set()
    for index in range(len(items) - 1, -1, -1):
        if items[index].type != "reasoning_item":
            continue
        for next_index in range(index + 1, len(items)):
            if items[next_index].type == "reasoning_item":
                continue
            if next_index in retained_indexes:
                tied.add(index)
            break
    return tied


def _retained_items_for_blocked_output(items: list[RunItem]) -> list[RunItem]:
    """Pick out the items of a final turn to keep when its output is not deliverable.

    A tool that already ran has to stay in the session, together with the context needed to replay
    its call. Everything else - the assistant message the guardrail rejected above all - is dropped,
    including the reasoning that belongs to the rejected message rather than to a retained call.

    ``_SIDE_EFFECT_ITEM_TYPES`` is enumerated rather than derived, so an item type added later is
    *discarded* here by default and has to be classified deliberately. A record of a side effect
    that goes unclassified is a bug, so the safer default is the one that surfaces as a missing item
    rather than as a rejected message quietly reaching the session.
    """
    retained_indexes = {
        index for index, item in enumerate(items) if item.type in _SIDE_EFFECT_ITEM_TYPES
    }
    if not retained_indexes:
        return []
    # Reasoning items are not side effects themselves, but a reasoning model requires the reasoning
    # item tied to a function call to accompany it in the next request.
    retained_indexes |= _reasoning_indexes_tied_to_retained_items(items, retained_indexes)
    # Indexed rather than filtered by type so the retained items keep the model's own order.
    return [item for index, item in enumerate(items) if index in retained_indexes]


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
    persist_before_output_guardrails: bool,
) -> None:
    redacted_persistence_error: BaseException | None = None
    if persist_before_output_guardrails:
        # A resumed approval has already committed the tool side effect, so keep its call/output
        # pair even when an agent output guardrail blocks delivery of the final result.
        await save_items(items, response_id, store_setting)

    try:
        output_guardrail_results = await _run_output_guardrails_for_stream(
            agent=agent,
            run_config=run_config,
            output=output,
            context_wrapper=context_wrapper,
            streamed_result=streamed_result,
        )
    except OutputGuardrailTripwireTriggered:
        # The blocked output itself is not persisted, but a tool that already ran is: the next run
        # has to see that side effect rather than re-issue it. This turn reaches here with tool
        # items when `tool_use_behavior="stop_on_first_tool"` (or `stop_at_tool_names`, or a custom
        # callable) turned a tool result straight into the final output.
        if not persist_before_output_guardrails:
            retained_items = _retained_items_for_blocked_output(items)
            if retained_items:
                await save_items(retained_items, response_id, store_setting)
        raise
    except Exception as guardrail_error:
        # Only a tripwire means the output was judged undeliverable. A guardrail error leaves the
        # verdict unknown, so the completed final turn is persisted whole and remains replayable.
        # `asyncio.CancelledError` is deliberately not caught here: `cancel()` in its default
        # immediate mode has to stay prompt, and awaiting a session write would block
        # `stream_events()` on an arbitrary backend. `after_turn` is the mode that finishes the
        # turn and saves.
        guardrail_error_is_redacted = _is_error_data_redacted(guardrail_error)
        if guardrail_error_is_redacted:
            _detach_data_redacted_error_traceback(guardrail_error)
        if not persist_before_output_guardrails:
            try:
                await save_items(items, response_id, store_setting)
            except (Exception, asyncio.CancelledError) as persistence_error:
                if guardrail_error_is_redacted:
                    if isinstance(persistence_error, asyncio.CancelledError):
                        safe_persistence_error: BaseException = asyncio.CancelledError(
                            _DATA_REDACTED_ERROR_MESSAGE
                        )
                    else:
                        safe_persistence_error = UserError(_DATA_REDACTED_ERROR_MESSAGE)
                    _mark_error_data_redacted(safe_persistence_error)
                    if (
                        isinstance(safe_persistence_error, asyncio.CancelledError)
                        and streamed_result._cancel_mode != "immediate"
                    ):
                        # A cancelled session write is distinct from the caller requesting
                        # immediate cancellation. Retain a safe cancellation for `stream_events()`
                        # without completing the run-loop task with the payload-bearing backend
                        # exception.
                        streamed_result._stored_exception = safe_persistence_error
                        streamed_result.is_complete = True
                        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                        return
                    if isinstance(safe_persistence_error, asyncio.CancelledError):
                        # Public immediate cancellation already owns stream completion and must
                        # not surface a recovery failure.
                        return
                    redacted_persistence_error = safe_persistence_error
                if (
                    isinstance(persistence_error, asyncio.CancelledError)
                    and streamed_result._cancel_mode != "immediate"
                ):
                    # A cancelled session write is distinct from the caller requesting immediate
                    # cancellation. The run-loop task itself becomes cancelled, so retain the
                    # backend cancellation for `stream_events()` to surface.
                    streamed_result._stored_exception = persistence_error
                if redacted_persistence_error is None:
                    raise
        if redacted_persistence_error is None:
            raise

    if redacted_persistence_error is not None:
        raise redacted_persistence_error from None

    streamed_result.output_guardrail_results = output_guardrail_results
    streamed_result.final_output = output
    streamed_result.is_complete = True

    if not persist_before_output_guardrails:
        # Saved as one ordered batch so the session mirrors the model response. Doing it in two
        # halves would both reorder the turn and, because the first save advances the turn's
        # persisted-item count, make the second one a no-op.
        await save_items(items, response_id, store_setting)

    streamed_result._event_queue.put_nowait(QueueCompleteSentinel())


def _accumulate_tool_guardrail_results(
    streamed_result: RunResultStreaming,
    turn_result: SingleStepResult,
) -> None:
    """Carry a turn's tool guardrail results onto the streamed result.

    The non-streaming loop extends its run-wide lists from every turn result, so the streaming
    loop has to do the same for `RunResultStreaming` to report the guardrails that ran.
    """
    streamed_result.tool_input_guardrail_results = (
        streamed_result.tool_input_guardrail_results + turn_result.tool_input_guardrail_results
    )
    streamed_result.tool_output_guardrail_results = (
        streamed_result.tool_output_guardrail_results + turn_result.tool_output_guardrail_results
    )


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
    max_turns: int | None,
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
    trace_workflow_name: str,
    is_resumed_state: bool = False,
    sandbox_runtime: SandboxRuntime[TContext] | None = None,
):
    """Run the streaming loop for a run result."""
    if streamed_result.trace is not None:
        streamed_result.trace.start(mark_as_current=True)
    if run_state is not None:
        current_trace = get_current_trace()
        run_state.set_trace(current_trace if current_trace is not None else streamed_result.trace)
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

    use_task_and_turn_spans = include_task_and_turn_spans(run_config.tracing)
    current_task_span: Span[TaskSpanData] | None = (
        task_span(name=trace_workflow_name) if use_task_and_turn_spans else None
    )
    if current_task_span is not None:
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
            streamed_result._nested_history_owned_session_item_refs = list(
                run_state._nested_history_owned_session_item_refs
            )
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
        pending_input_admission_items: list[InputItem] = []
        session_input_items_for_persistence: list[TResponseInputItem] | None = None

        def _commit_pending_server_response(
            model_response: ModelResponse,
            processed_response: ProcessedResponse | None,
        ) -> bool:
            if (
                run_state is None
                or server_conversation_tracker is None
                or not pending_input_admission_items
            ):
                return False
            return commit_server_pending_input(
                run_state=run_state,
                tracker=server_conversation_tracker,
                admission_items=pending_input_admission_items,
                generated_items=streamed_result._model_input_items,
                session_items=streamed_result.new_items,
                model_response=model_response,
                processed_response=processed_response,
                current_turn=current_turn,
            )

        def _mark_response_hooks_started() -> None:
            if run_state is None or not isinstance(run_state._current_step, NextStepInterruption):
                return
            if run_state._current_step.response_accepted:
                run_state._current_step.llm_end_hooks_started = True

        if is_resumed_state and server_conversation_tracker is not None and run_state is not None:
            session_items: list[TResponseInputItem] | None = None
            if session is not None:
                try:
                    session_items = await _session_get_items(
                        session,
                        wrapper=context_wrapper,
                    )
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
            (
                prepared_input,
                run_state._nested_history_owned_session_item_refs,
            ) = reconcile_nested_history_owned_input_after_rewrite(
                starting_input,
                prepared_input,
                run_state._nested_history_owned_session_item_refs,
            )
            run_state._original_input = copy_input_items(prepared_input)
            streamed_result._nested_history_owned_session_item_refs = list(
                run_state._nested_history_owned_session_item_refs
            )
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
                reasoning_item_id_policy=resolved_reasoning_item_id_policy,
                wrapper=context_wrapper,
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
        if current_task_span is not None:
            attach_usage_to_span(
                current_task_span,
                usage_delta(task_usage_start, context_wrapper.usage),
            )
            current_task_span.finish(reset_current=True)
        if streamed_result.trace is not None:
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
                                wrapper=context_wrapper,
                            )
                        )
                        raise InputGuardrailTripwireTriggered(result)
                sequential_guardrails = []

            if sandbox_runtime is not None:
                input_before_sandbox = copy_input_items(prepared_turn_input)
                prepared_sandbox = await sandbox_runtime.prepare_agent(
                    current_agent=current_agent,
                    current_input=prepared_turn_input,
                    context_wrapper=context_wrapper,
                    is_resumed_state=is_resumed_state,
                )
                current_bindings = prepared_sandbox.bindings
                execution_agent = current_bindings.execution_agent
                prepared_turn_input, retained_owned_refs = (
                    reconcile_nested_history_owned_input_after_rewrite(
                        input_before_sandbox,
                        prepared_sandbox.input,
                        streamed_result._nested_history_owned_session_item_refs,
                    )
                )
                streamed_result._nested_history_owned_session_item_refs = retained_owned_refs
                streamed_result.input = prepared_turn_input
                streamed_result._original_input = copy_input_items(prepared_turn_input)
                if run_state is not None:
                    run_state._original_input = copy_input_items(prepared_turn_input)
                    run_state._nested_history_owned_session_item_refs = list(retained_owned_refs)
                sandbox_runtime.apply_result_metadata(streamed_result)

            if is_resumed_state and run_state is not None and run_state._current_step is not None:
                if isinstance(run_state._current_step, NextStepInterruption):
                    if not run_state._model_responses:
                        raise UserError("No model response found in previous state")
                    if run_state._last_processed_response is None:
                        if run_state._current_step.response_accepted:
                            raise UserError(
                                "An accepted model response could not be processed; "
                                "start a new run instead of retrying it"
                            )
                        raise UserError("No processed response found in previous state")

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
                        error_handlers=error_handlers,
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

                    input_before_turn_rewrite = streamed_result.input
                    streamed_result.input = turn_result.original_input
                    streamed_result._original_input = copy_input_items(turn_result.original_input)
                    generated_items, turn_session_items = resumed_turn_items(turn_result)
                    base_session_items = (
                        list(run_state._session_items) if run_state is not None else []
                    )
                    streamed_result._model_input_items = generated_items
                    streamed_result.new_items = base_session_items + list(turn_session_items)
                    if turn_result.nested_history_owned_items is not None:
                        owned_refs = reconcile_nested_history_owned_session_item_refs(
                            streamed_result.new_items,
                            streamed_result._nested_history_owned_session_item_refs,
                            input_before_turn_rewrite,
                            turn_result.original_input,
                            turn_result.nested_history_owned_items,
                        )
                        streamed_result._nested_history_owned_session_item_refs = owned_refs
                        if run_state is not None:
                            run_state._nested_history_owned_session_item_refs = list(owned_refs)
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

                    # The non-streaming resume path extends its run-wide lists before finalizing
                    # but skips a resumed turn that loops back to the model, so a guardrail that
                    # re-runs for the same tool call on resume is not counted twice.
                    if not isinstance(turn_result.next_step, NextStepRunAgain):
                        _accumulate_tool_guardrail_results(streamed_result, turn_result)

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
                        await _save_resumed_items(
                            list(turn_session_items),
                            turn_result.model_response.response_id,
                            store_setting,
                        )
                        current_agent = turn_result.next_step.new_agent
                        if run_state is not None:
                            run_state._current_agent = current_agent
                        _publish_streamed_result_agent(streamed_result, current_agent)
                        if current_span is not None:
                            current_span.finish(reset_current=True)
                        current_span = None
                        should_run_agent_start_hooks = True
                        streamed_result._event_queue.put_nowait(
                            AgentUpdatedStreamEvent(new_agent=current_agent)
                        )
                        run_state._current_step = NextStepRunAgain()
                        if await _wait_for_streamed_turn_events_and_stop_if_cancelled(
                            streamed_result
                        ):
                            break
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
                            persist_before_output_guardrails=True,
                        )
                        run_state._current_step = None
                        break

                    if isinstance(turn_result.next_step, NextStepRunAgain):
                        await _save_resumed_items(
                            list(turn_session_items),
                            turn_result.model_response.response_id,
                            store_setting,
                        )
                        run_state._current_step = NextStepRunAgain()
                        if await _wait_for_streamed_turn_events_and_stop_if_cancelled(
                            streamed_result
                        ):
                            break
                        continue

                    run_state._current_step = None

            if streamed_result._cancel_mode == "after_turn":
                streamed_result.is_complete = True
                streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
                break

            if streamed_result.is_complete:
                break

            if run_state is not None and run_state._pending_input:
                if run_state._current_step is None:
                    run_state._current_step = NextStepRunAgain()
                pending_input = run_state.pending_input
                pending_guardrails = current_agent.input_guardrails + (
                    run_config.input_guardrails or []
                )
                previous_result_count = len(streamed_result.input_guardrail_results)
                try:
                    await run_input_guardrails_with_queue(
                        current_agent,
                        pending_guardrails,
                        pending_input,
                        context_wrapper,
                        streamed_result,
                        current_span,
                    )
                finally:
                    run_state._input_guardrail_results = list(
                        streamed_result.input_guardrail_results
                    )
                tripping_result = next(
                    (
                        result
                        for result in streamed_result.input_guardrail_results[
                            previous_result_count:
                        ]
                        if result.output.tripwire_triggered
                    ),
                    None,
                )
                if tripping_result is not None:
                    raise InputGuardrailTripwireTriggered(tripping_result)

                store_setting = current_agent.model_settings.resolve(
                    run_config.model_settings
                ).store
                admission_items = await admit_pending_input(
                    run_state=run_state,
                    agent=current_agent,
                    session=session,
                    server_conversation_tracker=server_conversation_tracker,
                    store=store_setting,
                    wrapper=context_wrapper,
                )
                streamed_result._model_input_items.extend(admission_items)
                streamed_result.new_items.extend(admission_items)
                if pending_server_items is not None:
                    pending_server_items.extend(admission_items)
                pending_input_admission_items = [
                    item for item in admission_items if isinstance(item, InputItem)
                ]
                if not run_state._pending_input:
                    run_state._generated_items = list(streamed_result._model_input_items)
                    run_state._session_items = list(streamed_result.new_items)

            all_tools = await get_all_tools(execution_agent, context_wrapper)
            all_tools = await initialize_computer_tools(
                tools=all_tools, context_wrapper=context_wrapper
            )

            if current_span is None:
                if (output_schema := get_output_schema(execution_agent)) is not None:
                    output_type_name = output_schema.name()
                else:
                    output_type_name = "str"

                current_span = agent_span(
                    name=current_agent.name,
                    handoffs=[],
                    tools=[],
                    output_type=output_type_name,
                )
                current_span.start(mark_as_current=True)

            current_turn += 1
            streamed_result.current_turn = current_turn
            streamed_result._current_turn_persisted_item_count = 0
            if run_state is not None:
                run_state._current_turn_persisted_item_count = 0

            if max_turns is not None and current_turn > max_turns:
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
                    error_kind="max_turns",
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
                                    wrapper=context_wrapper,
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
                current_turn_span = (
                    turn_span(
                        turn=current_turn,
                        agent_name=current_agent.name,
                    )
                    if use_task_and_turn_spans
                    else None
                )
                if current_turn_span is not None:
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
                        reasoning_item_id_policy=resolved_reasoning_item_id_policy,
                        prompt_cache_key_resolver=prompt_cache_key_resolver,
                        error_handlers=error_handlers,
                        agent_span=current_span,
                        on_response_accepted=_commit_pending_server_response,
                        on_response_hooks_started=_mark_response_hooks_started,
                        run_state=run_state,
                    )
                finally:
                    if current_turn_span is not None:
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
                _accumulate_tool_guardrail_results(streamed_result, turn_result)
                input_before_turn_rewrite = streamed_result.input
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
                if pending_input_admission_items and run_state is not None:
                    run_state._generated_items = list(streamed_result._model_input_items)
                    run_state._session_items = list(streamed_result.new_items)
                    run_state._model_responses = list(streamed_result.raw_responses)
                    run_state._last_processed_response = turn_result.processed_response
                    run_state._current_turn = current_turn
                    run_state._mark_generated_items_merged_with_last_processed()
                pending_input_admission_items = []
                if turn_result.nested_history_owned_items is not None:
                    owned_refs = reconcile_nested_history_owned_session_item_refs(
                        streamed_result.new_items,
                        streamed_result._nested_history_owned_session_item_refs,
                        input_before_turn_rewrite,
                        turn_result.original_input,
                        turn_result.nested_history_owned_items,
                    )
                    streamed_result._nested_history_owned_session_item_refs = owned_refs
                    if run_state is not None:
                        run_state._nested_history_owned_session_item_refs = list(owned_refs)
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
                    if run_state is not None:
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
                    _publish_streamed_result_agent(streamed_result, current_agent)
                    current_span.finish(reset_current=True)
                    current_span = None
                    should_run_agent_start_hooks = True
                    streamed_result._event_queue.put_nowait(
                        AgentUpdatedStreamEvent(new_agent=current_agent)
                    )
                    if streamed_result._state is not None:
                        streamed_result._state._current_step = NextStepRunAgain()

                    if await _wait_for_streamed_turn_events_and_stop_if_cancelled(streamed_result):
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
                        persist_before_output_guardrails=False,
                    )
                    if run_state is not None:
                        run_state._current_step = None
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

                    if await _wait_for_streamed_turn_events_and_stop_if_cancelled(streamed_result):
                        break
            except Exception as e:
                attach_generic_agent_error(
                    current_span,
                    e,
                    trace_include_sensitive_data=run_config.trace_include_sensitive_data,
                )
                raise
    except AgentsException as exc:
        streamed_result.is_complete = True
        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
        if _is_error_data_redacted(exc):
            _detach_data_redacted_error_traceback(exc)
        else:
            _clear_data_redacted_error_traceback(exc)
            exc.run_data = RunErrorDetails(
                input=streamed_result.input,
                new_items=streamed_result.new_items,
                raw_responses=streamed_result.raw_responses,
                last_agent=current_agent,
                context_wrapper=context_wrapper,
                input_guardrail_results=streamed_result.input_guardrail_results,
                output_guardrail_results=streamed_result.output_guardrail_results,
                tool_input_guardrail_results=streamed_result.tool_input_guardrail_results,
                tool_output_guardrail_results=streamed_result.tool_output_guardrail_results,
            )
        raise
    except Exception as e:
        attach_generic_agent_error(
            current_span,
            e,
            trace_include_sensitive_data=run_config.trace_include_sensitive_data,
        )
        streamed_result.is_complete = True
        streamed_result._event_queue.put_nowait(QueueCompleteSentinel())
        raise
    else:
        streamed_result.is_complete = True
    finally:
        await cleanup_models_after_run(tool_use_tracker)
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
                log_model_and_tool_action_debug(
                    logger,
                    "Error finalizing streamed result",
                    e,
                    diagnostic_extra=partial(_agent_diagnostic_extra, current_agent),
                )
        try:
            await dispose_resolved_computers(run_context=context_wrapper)
        except Exception as error:
            log_tool_action_warning(logger, "Failed to dispose computers after streamed run", error)
        if current_span is not None:
            current_span.finish(reset_current=True)
        if current_task_span is not None:
            attach_usage_to_span(
                current_task_span,
                usage_delta(task_usage_start, context_wrapper.usage),
            )
            current_task_span.finish(reset_current=True)
        if streamed_result.trace is not None:
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
    pending_server_items: list[RunItem] | None = None,
    reasoning_item_id_policy: ReasoningItemIdPolicy | None = None,
    prompt_cache_key_resolver: PromptCacheKeyResolver | None = None,
    error_handlers: RunErrorHandlers[TContext] | None = None,
    agent_span: Span[AgentSpanData] | None = None,
    on_response_accepted: Callable[[ModelResponse, ProcessedResponse | None], bool] | None = None,
    on_response_hooks_started: Callable[[], None] | None = None,
    run_state: RunState[Any] | None = None,
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

    try:
        turn_input = ItemHelpers.input_to_new_input_list(streamed_result.input)
    except Exception:
        turn_input = []
    context_wrapper.turn_input = list(turn_input)

    if should_run_agent_start_hooks:
        agent_hook_context = AgentHookContext(
            context=context_wrapper.context,
            usage=context_wrapper.usage,
            turn_input=turn_input,
        )
        context_wrapper._share_tool_state_with(agent_hook_context)
        await gather_with_cancel(
            hooks.on_agent_start(agent_hook_context, public_agent),
            (
                public_agent.hooks.on_start(agent_hook_context, public_agent)
                if public_agent.hooks is not None
                else _coro.noop_coroutine()
            ),
        )

    output_schema = get_output_schema(execution_agent)

    streamed_result.current_agent = public_agent
    streamed_result._current_agent_output_schema = get_output_schema(public_agent)

    system_prompt, prompt_config = await gather_with_cancel(
        execution_agent.get_system_prompt(context_wrapper),
        execution_agent.get_prompt(context_wrapper),
    )

    handoffs = await get_handoffs(execution_agent, context_wrapper)
    all_tools, handoffs = resolve_tool_name_collisions(
        all_tools,
        handoffs,
        collision_policy=run_config.tool_name_collision_policy,
    )
    if agent_span is not None:
        agent_span.span_data.handoffs = [handoff.agent_name for handoff in handoffs]
        agent_span.span_data.tools = [
            tool_name
            for tool in all_tools
            if (tool_name := get_tool_trace_name_for_tool(tool)) is not None
        ]

    model = get_model(execution_agent, run_config)
    tool_use_tracker.record_model(model)
    model_settings = get_model_settings(execution_agent, run_config)
    model_settings = maybe_reset_tool_choice(public_agent, tool_use_tracker, model_settings)

    final_response: ModelResponse | None = None
    streamed_response_output: list[ResponseOutputItem] = []
    emitted_model_item_occurrence_keys: set[str] = set()

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
    if server_conversation_tracker is not None:
        server_conversation_tracker.validate_pending_input_filter(filtered.input)
        logger.debug(
            "filtered.input has %s items; ids=%s",
            len(filtered.input),
            [id(i) for i in filtered.input],
        )
        # Track only the items actually sent after call_model_input_filter runs. Retry helpers
        # explicitly rewind this state before replaying a failed request.
        server_conversation_tracker.mark_input_as_sent(filtered.input)

    await gather_with_cancel(
        hooks.on_llm_start(context_wrapper, public_agent, filtered.instructions, filtered.input),
        (
            public_agent.hooks.on_llm_start(
                context_wrapper,
                public_agent,
                filtered.instructions,
                filtered.input,
            )
            if public_agent.hooks is not None
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
            await save_result_to_session(
                session,
                input_items_to_save,
                [],
                streamed_result._state,
                wrapper=context_wrapper,
            )

    previous_response_id = (
        server_conversation_tracker.previous_response_id
        if server_conversation_tracker is not None
        and server_conversation_tracker.previous_response_id is not None
        else None
    )
    conversation_id = (
        server_conversation_tracker.conversation_id
        if server_conversation_tracker is not None
        else None
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
        replay_unsafe_request=any(
            isinstance(tool, ProgrammaticToolCallingTool) for tool in all_tools
        ),
    )

    async for event in model_run_context_stream(retry_stream, tool_use_tracker):
        streamed_result._event_queue.put_nowait(RawResponsesStreamEvent(data=event))

        terminal_response: Response | None = None
        is_completed_event = False
        if isinstance(event, ResponseCompletedEvent):
            is_completed_event = True
            terminal_response = event.response
        elif getattr(event, "type", None) in {"response.incomplete", "response.failed"}:
            event_type = cast(str, event.type)
            maybe_response = getattr(event, "response", None)
            raise response_terminal_failure_error(
                event_type,
                maybe_response if isinstance(maybe_response, Response) else None,
            )
        elif getattr(event, "type", None) in {"error", "response.error"}:
            raise response_error_event_failure_error(cast(str, event.type), event)

        if terminal_response is not None:
            if is_completed_event and not terminal_response.output and streamed_response_output:
                # Some streaming backends emit output items during item.done events while leaving
                # the terminal response output empty. Preserve those items so the runner can
                # resolve the completed step correctly.
                terminal_response.output = list(streamed_response_output)
            # Always fold retry attempts into usage, even when the terminal response omits
            # provider usage (common for some Chat Completions / LiteLLM streams). Skipping
            # apply_retry_attempt_usage here would drop failed-attempt accounting and diverge
            # from the non-streaming get_response_with_retry path.
            usage = apply_retry_attempt_usage(
                (
                    _response_usage_to_usage(terminal_response.usage)
                    if terminal_response.usage
                    # Defaults to zero requests, so adapters that fold several provider
                    # responses into one and report counts separately are not double-counted.
                    else Usage(requests=_requests_for_response_without_usage(terminal_response))
                ),
                stream_failed_retry_attempts[0],
            )
            final_response = ModelResponse(
                output=terminal_response.output,
                usage=usage,
                response_id=terminal_response.id,
                request_id=getattr(terminal_response, "_request_id", None),
                raw_usage=(
                    _extract_raw_usage_snapshot(terminal_response)
                    if model_settings.preserve_raw_usage is True
                    else None
                ),
            )

        if isinstance(event, ResponseOutputItemDoneEvent):
            streamed_response_output.append(event.item)

    if final_response is None:
        raise ModelBehaviorError("Model did not produce a final response!")

    context_wrapper.usage.add(final_response.usage)

    if server_conversation_tracker is not None:
        # Streaming uses the same rewind helper, so a successful retry must restore delivered
        # input tracking before the next turn computes server-managed deltas.
        server_conversation_tracker.mark_input_as_sent(filtered.input)
        server_conversation_tracker.mark_input_as_accepted(filtered.input)
        server_conversation_tracker.track_server_items(final_response)

    response_accepted = False
    if on_response_accepted is not None:
        response_accepted = on_response_accepted(final_response, None)

    async def after_invocation_validation(
        processed_response: ProcessedResponse | None,
    ) -> bool:
        if response_accepted and on_response_accepted is not None:
            on_response_accepted(final_response, processed_response)
        if response_accepted and on_response_hooks_started is not None:
            on_response_hooks_started()
        if processed_response is not None:
            model_items = processed_response.new_items
            emitted_model_item_occurrence_keys.update(
                _ensure_stream_event_item_occurrence_key(item) for item in model_items
            )
            stream_step_items_to_queue(model_items, streamed_result._event_queue)
        await gather_with_cancel(
            (
                public_agent.hooks.on_llm_end(context_wrapper, public_agent, final_response)
                if public_agent.hooks is not None
                else _coro.noop_coroutine()
            ),
            hooks.on_llm_end(context_wrapper, public_agent, final_response),
        )
        return response_accepted

    async def check_input_guardrails_before_side_effects() -> None:
        await raise_if_input_guardrail_tripwire_known()

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
        error_handlers=error_handlers,
        tool_use_tracker=tool_use_tracker,
        server_manages_conversation=server_conversation_tracker is not None,
        after_invocation_validation=after_invocation_validation,
        before_side_effects=check_input_guardrails_before_side_effects,
        run_state=run_state,
    )

    items_to_filter = session_items_for_turn(single_step_result)

    items_to_filter = [
        item
        for item in items_to_filter
        if _stream_event_item_occurrence_key(item) not in emitted_model_item_occurrence_keys
    ]

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
    error_handlers: RunErrorHandlers[TContext] | None = None,
    agent_span: Span[AgentSpanData] | None = None,
    on_response_accepted: Callable[[ModelResponse, ProcessedResponse | None], bool] | None = None,
    on_response_hooks_started: Callable[[], None] | None = None,
    run_state: RunState[Any] | None = None,
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
            turn_input=turn_input,
        )
        context_wrapper._share_tool_state_with(agent_hook_context)
        await gather_with_cancel(
            hooks.on_agent_start(agent_hook_context, public_agent),
            (
                public_agent.hooks.on_start(agent_hook_context, public_agent)
                if public_agent.hooks is not None
                else _coro.noop_coroutine()
            ),
        )

    system_prompt, prompt_config = await gather_with_cancel(
        execution_agent.get_system_prompt(context_wrapper),
        execution_agent.get_prompt(context_wrapper),
    )

    handoffs = await get_handoffs(execution_agent, context_wrapper)
    all_tools, handoffs = resolve_tool_name_collisions(
        all_tools,
        handoffs,
        collision_policy=run_config.tool_name_collision_policy,
    )
    if agent_span is not None:
        agent_span.span_data.handoffs = [handoff.agent_name for handoff in handoffs]
        agent_span.span_data.tools = [
            tool_name
            for tool in all_tools
            if (tool_name := get_tool_trace_name_for_tool(tool)) is not None
        ]

    output_schema = get_output_schema(execution_agent)
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
        defer_llm_end_hooks=True,
    )

    response_accepted = False
    if on_response_accepted is not None:
        response_accepted = on_response_accepted(new_response, None)

    async def after_invocation_validation(
        _processed_response: ProcessedResponse | None,
    ) -> bool:
        if response_accepted and on_response_accepted is not None:
            on_response_accepted(new_response, _processed_response)
        if response_accepted and on_response_hooks_started is not None:
            on_response_hooks_started()
        await gather_with_cancel(
            (
                public_agent.hooks.on_llm_end(context_wrapper, public_agent, new_response)
                if public_agent.hooks is not None
                else _coro.noop_coroutine()
            ),
            hooks.on_llm_end(context_wrapper, public_agent, new_response),
        )
        return response_accepted

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
        error_handlers=error_handlers,
        tool_use_tracker=tool_use_tracker,
        server_manages_conversation=server_conversation_tracker is not None,
        after_invocation_validation=after_invocation_validation,
        run_state=run_state,
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
    defer_llm_end_hooks: bool = False,
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
    tool_use_tracker.record_model(model)
    model_settings = get_model_settings(execution_agent, run_config)
    model_settings = maybe_reset_tool_choice(public_agent, tool_use_tracker, model_settings)

    if server_conversation_tracker is not None:
        server_conversation_tracker.validate_pending_input_filter(filtered.input)
        server_conversation_tracker.mark_input_as_sent(filtered.input)

    await gather_with_cancel(
        hooks.on_llm_start(context_wrapper, public_agent, filtered.instructions, filtered.input),
        (
            public_agent.hooks.on_llm_start(
                context_wrapper,
                public_agent,
                filtered.instructions,
                filtered.input,
            )
            if public_agent.hooks is not None
            else _coro.noop_coroutine()
        ),
    )

    previous_response_id = (
        server_conversation_tracker.previous_response_id
        if server_conversation_tracker is not None
        and server_conversation_tracker.previous_response_id is not None
        else None
    )
    conversation_id = (
        server_conversation_tracker.conversation_id
        if server_conversation_tracker is not None
        else None
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
        if server_conversation_tracker is not None:
            items_to_rewind = session_items_to_rewind if session_items_to_rewind is not None else []
            await rewind_session_items(
                session,
                items_to_rewind,
                server_conversation_tracker,
                wrapper=context_wrapper,
            )
            server_conversation_tracker.rewind_input(filtered.input)

    with model_run_context(tool_use_tracker):
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
            replay_unsafe_request=any(
                isinstance(tool, ProgrammaticToolCallingTool) for tool in all_tools
            ),
        )
    if server_conversation_tracker is not None:
        # Retry helpers rewind sent-input tracking before replaying a failed request. Mark the
        # filtered input as delivered again once a retry succeeds so subsequent turns only send
        # new deltas.
        server_conversation_tracker.mark_input_as_sent(filtered.input)
        server_conversation_tracker.mark_input_as_accepted(filtered.input)
        server_conversation_tracker.track_server_items(new_response)

    context_wrapper.usage.add(new_response.usage)

    if not defer_llm_end_hooks:
        await gather_with_cancel(
            (
                public_agent.hooks.on_llm_end(context_wrapper, public_agent, new_response)
                if public_agent.hooks is not None
                else _coro.noop_coroutine()
            ),
            hooks.on_llm_end(context_wrapper, public_agent, new_response),
        )

    return new_response

"""Internal helpers for AgentRunner.run."""

from __future__ import annotations

from typing import Any, cast

from ..agent import Agent
from ..exceptions import UserError
from ..guardrail import InputGuardrailResult
from ..items import ModelResponse, RunItem, ToolApprovalItem, TResponseInputItem
from ..memory import Session
from ..result import RunResult
from ..run_config import RunConfig
from ..run_context import RunContextWrapper, TContext
from ..run_state import RunState
from ..tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult
from ..tracing.config import TracingConfig
from ..tracing.traces import TraceState
from .items import copy_input_items
from .oai_conversation import OpenAIServerConversationTracker
from .run_steps import (
    NextStepFinalOutput,
    NextStepHandoff,
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
)
from .session_persistence import save_result_to_session
from .tool_use_tracker import AgentToolUseTracker, serialize_tool_use_tracker

__all__ = [
    "apply_resumed_conversation_settings",
    "append_model_response_if_new",
    "build_generated_items_details",
    "build_interruption_result",
    "build_resumed_stream_debug_extra",
    "describe_run_state_step",
    "ensure_context_wrapper",
    "finalize_conversation_tracking",
    "input_guardrails_triggered",
    "validate_session_conversation_settings",
    "resolve_trace_settings",
    "resolve_processed_response",
    "resolve_resumed_context",
    "save_turn_items_if_needed",
    "should_cancel_parallel_model_task_on_input_guardrail_trip",
    "update_run_state_for_interruption",
]

_PARALLEL_INPUT_GUARDRAIL_CANCEL_PATCH_ID = (
    "openai_agents.cancel_parallel_model_task_on_input_guardrail_trip.v1"
)


def should_cancel_parallel_model_task_on_input_guardrail_trip() -> bool:
    """Return whether an in-flight model task should be cancelled on guardrail trip."""
    try:
        from temporalio import workflow as temporal_workflow  # type: ignore[import-not-found]
    except Exception:
        return True

    try:
        if not temporal_workflow.in_workflow():
            return True
        # Preserve replay compatibility for histories created before cancellation.
        return bool(temporal_workflow.patched(_PARALLEL_INPUT_GUARDRAIL_CANCEL_PATCH_ID))
    except Exception:
        return True


def apply_resumed_conversation_settings(
    *,
    run_state: RunState[TContext],
    conversation_id: str | None,
    previous_response_id: str | None,
    auto_previous_response_id: bool,
) -> tuple[str | None, str | None, bool]:
    """Apply RunState conversation identifiers and return the resolved values."""
    conversation_id = conversation_id or run_state._conversation_id
    previous_response_id = previous_response_id or run_state._previous_response_id
    if auto_previous_response_id is False and run_state._auto_previous_response_id:
        auto_previous_response_id = True
    run_state._conversation_id = conversation_id
    run_state._previous_response_id = previous_response_id
    run_state._auto_previous_response_id = auto_previous_response_id
    return conversation_id, previous_response_id, auto_previous_response_id


def validate_session_conversation_settings(
    session: Session | None,
    *,
    conversation_id: str | None,
    previous_response_id: str | None,
    auto_previous_response_id: bool,
) -> None:
    if session is None:
        return
    if conversation_id is None and previous_response_id is None and not auto_previous_response_id:
        return
    raise UserError(
        "Session persistence cannot be combined with conversation_id, "
        "previous_response_id, or auto_previous_response_id."
    )


def resolve_trace_settings(
    *,
    run_state: RunState[TContext] | None,
    run_config: RunConfig,
) -> tuple[str, str | None, str | None, dict[str, Any] | None, TracingConfig | None]:
    """Resolve tracing settings, preferring explicit run_config overrides."""
    trace_state: TraceState | None = run_state._trace_state if run_state is not None else None
    default_workflow_name = RunConfig().workflow_name
    workflow_name = run_config.workflow_name

    trace_id: str | None = run_config.trace_id
    group_id: str | None = run_config.group_id
    metadata: dict[str, Any] | None = run_config.trace_metadata
    tracing: TracingConfig | None = run_config.tracing

    if trace_state:
        if workflow_name == default_workflow_name and trace_state.workflow_name:
            workflow_name = trace_state.workflow_name
        if trace_id is None:
            trace_id = trace_state.trace_id
        if group_id is None:
            group_id = trace_state.group_id
        if metadata is None and trace_state.metadata is not None:
            metadata = dict(trace_state.metadata)
        if tracing is None and trace_state.tracing_api_key:
            tracing = {"api_key": trace_state.tracing_api_key}

    return workflow_name, trace_id, group_id, metadata, tracing


def resolve_resumed_context(
    *,
    run_state: RunState[TContext],
    context: RunContextWrapper[TContext] | TContext | None,
) -> RunContextWrapper[TContext]:
    """Return the context wrapper for a resumed run, overriding when provided."""
    if context is not None:
        context_wrapper = ensure_context_wrapper(context)
        run_state._context = context_wrapper
        return context_wrapper
    if run_state._context is None:
        run_state._context = ensure_context_wrapper(context)
    return run_state._context


def ensure_context_wrapper(
    context: RunContextWrapper[TContext] | TContext | None,
) -> RunContextWrapper[TContext]:
    """Normalize a context value into a RunContextWrapper."""
    if isinstance(context, RunContextWrapper):
        return context
    return RunContextWrapper(context=cast(TContext, context))


def describe_run_state_step(step: object | None) -> str | int | None:
    """Return a debug-friendly label for the current run state step."""
    if step is None:
        return None
    if isinstance(step, NextStepInterruption):
        return "next_step_interruption"
    if isinstance(step, NextStepHandoff):
        return "next_step_handoff"
    if isinstance(step, NextStepFinalOutput):
        return "next_step_final_output"
    if isinstance(step, NextStepRunAgain):
        return "next_step_run_again"
    return type(step).__name__


def build_generated_items_details(
    items: list[RunItem],
    *,
    include_tool_output: bool,
) -> list[dict[str, object]]:
    """Return debug-friendly metadata for generated items."""
    details: list[dict[str, object]] = []
    for idx, item in enumerate(items):
        item_info: dict[str, object] = {"index": idx, "type": item.type}
        if hasattr(item, "raw_item") and isinstance(item.raw_item, dict):
            item_info["raw_type"] = item.raw_item.get("type")
            item_info["name"] = item.raw_item.get("name")
            item_info["call_id"] = item.raw_item.get("call_id")
            if item.type == "tool_call_output_item" and include_tool_output:
                output_str = str(item.raw_item.get("output", ""))[:100]
                item_info["output"] = output_str
        details.append(item_info)
    return details


def build_resumed_stream_debug_extra(
    run_state: RunState[TContext],
    *,
    include_tool_output: bool,
) -> dict[str, object]:
    """Build the logger extra payload when resuming a streamed run."""
    return {
        "current_turn": run_state._current_turn,
        "current_agent": run_state._current_agent.name if run_state._current_agent else None,
        "generated_items_count": len(run_state._generated_items),
        "generated_items_types": [item.type for item in run_state._generated_items],
        "generated_items_details": build_generated_items_details(
            run_state._generated_items,
            include_tool_output=include_tool_output,
        ),
        "current_step_type": describe_run_state_step(run_state._current_step),
    }


def finalize_conversation_tracking(
    result: RunResult,
    *,
    server_conversation_tracker: OpenAIServerConversationTracker | None,
    run_state: RunState | None,
) -> RunResult:
    """Propagate conversation metadata to the result and run state."""
    if server_conversation_tracker is None:
        return result
    result._conversation_id = server_conversation_tracker.conversation_id
    result._previous_response_id = server_conversation_tracker.previous_response_id
    result._auto_previous_response_id = server_conversation_tracker.auto_previous_response_id
    if run_state is not None:
        run_state._conversation_id = server_conversation_tracker.conversation_id
        run_state._previous_response_id = server_conversation_tracker.previous_response_id
        run_state._auto_previous_response_id = server_conversation_tracker.auto_previous_response_id
    return result


def build_interruption_result(
    *,
    result_input: str | list[TResponseInputItem],
    session_items: list[RunItem],
    model_responses: list[ModelResponse],
    current_agent: Agent[Any],
    input_guardrail_results: list[InputGuardrailResult],
    tool_input_guardrail_results: list[ToolInputGuardrailResult],
    tool_output_guardrail_results: list[ToolOutputGuardrailResult],
    context_wrapper: RunContextWrapper[TContext],
    interruptions: list[ToolApprovalItem],
    processed_response: ProcessedResponse | None,
    tool_use_tracker: AgentToolUseTracker,
    max_turns: int,
    current_turn: int,
    generated_items: list[RunItem],
    run_state: RunState | None,
    original_input: str | list[TResponseInputItem],
) -> RunResult:
    """Create a RunResult for an interruption path."""
    result = RunResult(
        input=result_input,
        new_items=session_items,
        raw_responses=model_responses,
        final_output=None,
        _last_agent=current_agent,
        input_guardrail_results=input_guardrail_results,
        output_guardrail_results=[],
        tool_input_guardrail_results=tool_input_guardrail_results,
        tool_output_guardrail_results=tool_output_guardrail_results,
        context_wrapper=context_wrapper,
        interruptions=interruptions,
        _last_processed_response=processed_response,
        _tool_use_tracker_snapshot=serialize_tool_use_tracker(tool_use_tracker),
        max_turns=max_turns,
    )
    result._current_turn = current_turn
    result._model_input_items = list(generated_items)
    if run_state is not None:
        result._current_turn_persisted_item_count = run_state._current_turn_persisted_item_count
        result._trace_state = run_state._trace_state
    result._original_input = copy_input_items(original_input)
    return result


def append_model_response_if_new(
    model_responses: list[ModelResponse],
    response: ModelResponse,
) -> None:
    """Append a model response only when it is not already in the list tail."""
    if not model_responses or model_responses[-1] is not response:
        model_responses.append(response)


def input_guardrails_triggered(results: list[InputGuardrailResult]) -> bool:
    """Return True when any guardrail tripwire has fired."""
    return any(result.output.tripwire_triggered for result in results)


def update_run_state_for_interruption(
    *,
    run_state: RunState[TContext],
    model_responses: list[ModelResponse],
    processed_response: ProcessedResponse | None,
    generated_items: list[RunItem],
    session_items: list[RunItem] | None,
    current_turn: int,
    next_step: NextStepInterruption,
) -> None:
    """Sync run-state fields needed to resume after an interruption."""
    run_state._model_responses = model_responses
    run_state._last_processed_response = processed_response
    run_state._generated_items = generated_items
    if session_items is not None:
        run_state._session_items = list(session_items)
    run_state._current_step = next_step
    run_state._current_turn = current_turn


async def save_turn_items_if_needed(
    *,
    session: Session | None,
    run_state: RunState | None,
    session_persistence_enabled: bool,
    input_guardrail_results: list[InputGuardrailResult],
    items: list[RunItem],
    response_id: str | None,
    store: bool | None = None,
) -> None:
    """Persist turn items when persistence is enabled and guardrails allow it."""
    if not session_persistence_enabled:
        return
    if input_guardrails_triggered(input_guardrail_results):
        return
    if run_state is not None and run_state._current_turn_persisted_item_count > 0:
        return
    await save_result_to_session(
        session,
        [],
        list(items),
        run_state,
        response_id=response_id,
        store=store,
    )


def resolve_processed_response(
    *,
    run_state: RunState | None,
    processed_response: ProcessedResponse | None,
) -> ProcessedResponse | None:
    """Return a processed response, falling back to the run state when missing."""
    if processed_response is None and run_state is not None:
        return run_state._last_processed_response
    return processed_response

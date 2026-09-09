"""Canonical data-free function-tool payloads rejected by an output guardrail."""

from __future__ import annotations

import dataclasses as _dc
import inspect
from collections.abc import Sequence
from typing import Any, TypeVar, cast

from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_function_tool_call import CallerDirect

from ..agent import Agent
from ..exceptions import (
    AgentsException,
    OutputGuardrailTripwireTriggered,
    UserError,
    _detach_data_redacted_error_traceback,
    _mark_error_data_redacted,
    _prepare_data_redacted_error,
)
from ..guardrail import GuardrailFunctionOutput, OutputGuardrailResult
from ..items import ModelResponse, RunItem, ToolCallItem, ToolCallOutputItem
from ..memory import Session
from ..result import RunResultStreaming
from ..run_config import (
    OutputGuardrailBlockedMessageArgs,
    OutputGuardrailBlockedMessageFormatter,
    RunConfig,
)
from ..run_context import RunContextWrapper
from ..run_state import RunState
from ..tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailResult,
    ToolOutputGuardrailResult,
)
from .run_steps import NextStepInterruption, ProcessedResponse

OUTPUT_GUARDRAIL_BLOCKED_TOOL_OUTPUT = "Output withheld by an output guardrail."

_RESPONSE_OUTPUT_STATUSES = frozenset({"in_progress", "completed", "incomplete"})


def _exact_dict_field(values: dict[Any, Any], field: str) -> Any:
    """Read one exact string key without invoking stored-key equality hooks."""
    for key, value in dict.items(values):
        if type(key) is str and str.__eq__(key, field) is True:
            return value
    return None


def _payload_field(raw_item: Any, field: str) -> Any:
    """Read an allowlisted field without copying extras or invoking instance hooks."""
    if type(raw_item) is dict:
        values = raw_item
    elif type(raw_item) is ResponseFunctionToolCall:
        values = object.__getattribute__(raw_item, "__dict__")
    else:
        raise AgentsException("Cannot sanitize an unsupported tool item variant.")
    if type(values) is not dict:
        raise AgentsException("Cannot sanitize an unsupported tool item representation.")
    return _exact_dict_field(values, field)


def _required_string(raw_item: Any, field: str) -> str:
    value = _payload_field(raw_item, field)
    if type(value) is not str or not value:
        raise AgentsException(f"Cannot sanitize a function tool item without {field}.")
    return value


def _copy_optional_string(
    sanitized: dict[str, Any],
    raw_item: Any,
    field: str,
) -> None:
    value = _payload_field(raw_item, field)
    if value is None:
        return
    if type(value) is not str or not value:
        raise AgentsException(f"Cannot sanitize a function tool item with an invalid {field}.")
    sanitized[field] = value


def _copy_optional_status(sanitized: dict[str, Any], raw_item: Any) -> None:
    status = _payload_field(raw_item, "status")
    if status is None:
        return
    if type(status) is not str or status not in _RESPONSE_OUTPUT_STATUSES:
        raise AgentsException("Cannot sanitize a function tool item with an invalid status.")
    sanitized["status"] = status


def _copy_optional_direct_caller(sanitized: dict[str, Any], raw_item: Any) -> None:
    caller = _payload_field(raw_item, "caller")
    if caller is None:
        return
    if type(caller) is CallerDirect:
        values = object.__getattribute__(caller, "__dict__")
        caller_type = _exact_dict_field(values, "type") if type(values) is dict else None
    elif type(caller) is dict:
        caller_type = _exact_dict_field(caller, "type")
    else:
        caller_type = None
    if type(caller_type) is str and str.__eq__(caller_type, "direct") is True:
        sanitized["caller"] = {"type": "direct"}
        return
    raise AgentsException("Cannot sanitize a function tool item with a non-direct caller.")


def blocked_function_call_payload(raw_item: Any) -> dict[str, Any]:
    """Build a provider-valid function call from explicitly allowlisted fields."""
    item_type = _payload_field(raw_item, "type")
    if type(item_type) is not str or str.__eq__(item_type, "function_call") is not True:
        raise AgentsException("Cannot sanitize an unsupported tool call variant.")
    arguments = _payload_field(raw_item, "arguments")
    if type(arguments) is not str:
        raise AgentsException("Cannot sanitize a function tool item without arguments.")
    sanitized: dict[str, Any] = {
        "type": "function_call",
        "name": _required_string(raw_item, "name"),
        "arguments": arguments,
        "call_id": _required_string(raw_item, "call_id"),
    }
    _copy_optional_string(sanitized, raw_item, "id")
    _copy_optional_string(sanitized, raw_item, "namespace")
    _copy_optional_status(sanitized, raw_item)
    _copy_optional_direct_caller(sanitized, raw_item)
    try:
        validated = ResponseFunctionToolCall(**sanitized)
    except Exception:
        raise AgentsException("Sanitized function_call is not valid for replay.") from None
    return validated.model_dump(exclude_unset=True)


def blocked_function_output_payload(raw_item: Any) -> dict[str, Any]:
    """Build a replay-valid function output from explicitly allowlisted fields."""
    item_type = _payload_field(raw_item, "type")
    if type(item_type) is not str or str.__eq__(item_type, "function_call_output") is not True:
        raise AgentsException("Cannot sanitize an unsupported tool output variant.")
    sanitized: dict[str, Any] = {
        "type": "function_call_output",
        "call_id": _required_string(raw_item, "call_id"),
        "output": OUTPUT_GUARDRAIL_BLOCKED_TOOL_OUTPUT,
    }
    _copy_optional_string(sanitized, raw_item, "id")
    _copy_optional_status(sanitized, raw_item)
    _copy_optional_direct_caller(sanitized, raw_item)
    try:
        from ..run_state import _deserialize_tool_call_output_raw_item

        restored = _deserialize_tool_call_output_raw_item(sanitized)
    except Exception:
        raise AgentsException("Sanitized function_call_output is not valid for replay.") from None
    if restored is None:
        raise AgentsException("Sanitized function_call_output is not valid for replay.")
    return sanitized


_SIDE_EFFECT_ITEM_TYPES = frozenset({"tool_call_item", "tool_call_output_item"})


def _sanitize_blocked_output_guardrail_results(
    results: Sequence[OutputGuardrailResult],
    tripwire: OutputGuardrailTripwireTriggered,
    blocked_message: str = OUTPUT_GUARDRAIL_BLOCKED_TOOL_OUTPUT,
) -> list[OutputGuardrailResult]:
    """Build data-free guardrail results and detach the tripwire from raw output."""
    sanitized_by_id: dict[int, OutputGuardrailResult] = {}

    def sanitize(result: OutputGuardrailResult) -> OutputGuardrailResult:
        existing = sanitized_by_id.get(id(result))
        if existing is not None:
            return existing
        sanitized = OutputGuardrailResult(
            guardrail=result.guardrail,
            agent_output=blocked_message,
            agent=result.agent,
            output=GuardrailFunctionOutput(
                output_info=None,
                tripwire_triggered=result.output.tripwire_triggered,
            ),
        )
        sanitized_by_id[id(result)] = sanitized
        return sanitized

    sanitized_results = [sanitize(result) for result in results]
    object.__setattr__(tripwire, "guardrail_result", sanitize(tripwire.guardrail_result))
    _mark_error_data_redacted(tripwire)
    _detach_data_redacted_error_traceback(tripwire)
    return sanitized_results


def _resolve_output_guardrail_blocked_message(
    tripwire: OutputGuardrailTripwireTriggered,
    *,
    agent: Agent[Any],
    run_config: RunConfig,
    context_wrapper: RunContextWrapper[Any],
) -> str:
    """Resolve a custom placeholder without suspending the redaction pipeline."""
    blocked_message = OUTPUT_GUARDRAIL_BLOCKED_TOOL_OUTPUT
    configured = run_config.output_guardrail_blocked_message
    if configured is None:
        return blocked_message
    if type(configured) is str:
        resolved: Any = configured
    else:
        try:
            formatter = cast(OutputGuardrailBlockedMessageFormatter, configured)
            resolved = formatter(
                OutputGuardrailBlockedMessageArgs(
                    default_message=blocked_message,
                    guardrail_name=tripwire.guardrail_result.guardrail.get_name(),
                    agent=agent,
                    run_context=context_wrapper,
                )
            )
        except BaseException:
            return blocked_message
    if inspect.iscoroutine(resolved):
        try:
            resolved.close()
        except BaseException:
            pass
        return blocked_message
    if type(resolved) is not str or not resolved:
        return blocked_message
    return resolved


@_dc.dataclass(frozen=True)
class _CurrentResponseBoundary:
    """A current-response suffix proven only by lifecycle position or object identity."""

    items: tuple[RunItem, ...]
    processed_items: tuple[RunItem, ...]
    generated_start: int | None
    session_start: int | None
    proven: bool


@_dc.dataclass(frozen=True)
class _BlockedOutputSnapshot:
    """Prepared data-free replacements for one complete current response."""

    items: tuple[RunItem, ...]
    processed_items: tuple[RunItem, ...]
    model_response: ModelResponse | None


@_dc.dataclass(frozen=True)
class _BlockedOutputOwnerPlan:
    """Prebuilt trusted-owner assignments for application or emergency cleanup."""

    assignments: tuple[tuple[Any, str, Any], ...]


@_dc.dataclass(frozen=True)
class _BlockedOutputOwnerStarts:
    """Owner-specific current-response starts captured at trusted lifecycle boundaries."""

    nonstreamed_session_items: int | None = None
    run_state_generated_items: int | None = None
    run_state_session_items: int | None = None
    run_state_model_responses: int | None = None
    run_state_tool_output_guardrail_results: int | None = None
    streamed_new_items: int | None = None
    streamed_model_input_items: int | None = None
    streamed_raw_responses: int | None = None
    streamed_tool_output_guardrail_results: int | None = None


@_dc.dataclass(frozen=True)
class _BlockedOutputOwnerPrefixes:
    """Accepted owner prefixes allocated before any blocked-output replacement begins."""

    run_state_generated_items: list[RunItem]
    run_state_session_items: list[RunItem]
    run_state_model_responses: list[ModelResponse]
    run_state_tool_output_guardrail_results: list[ToolOutputGuardrailResult]
    streamed_new_items: list[RunItem]
    streamed_model_input_items: list[RunItem]
    streamed_raw_responses: list[ModelResponse]
    streamed_tool_output_guardrail_results: list[ToolOutputGuardrailResult]


_OwnerItemT = TypeVar("_OwnerItemT")


def _has_output_guardrails(agent: Agent[Any], run_config: RunConfig) -> bool:
    return bool(agent.output_guardrails or run_config.output_guardrails)


def _synchronize_accepted_run_state(
    run_state: RunState[Any],
    *,
    generated_items: Sequence[RunItem],
    session_items: Sequence[RunItem],
    model_responses: Sequence[ModelResponse],
    tool_input_guardrail_results: Sequence[ToolInputGuardrailResult],
    tool_output_guardrail_results: Sequence[ToolOutputGuardrailResult],
    current_turn: int,
) -> None:
    """Capture accepted run history before a guardrail-owned model response begins."""
    run_state._generated_items = list(generated_items)
    run_state._session_items = list(session_items)
    run_state._model_responses = list(model_responses)
    run_state._tool_input_guardrail_results = list(tool_input_guardrail_results)
    run_state._tool_output_guardrail_results = list(tool_output_guardrail_results)
    run_state._current_turn = current_turn


def _should_defer_interrupted_session_items(
    agent: Agent[Any],
    run_config: RunConfig,
) -> bool:
    """Defer only approval state that could still become guarded terminal tool output."""
    return _has_output_guardrails(agent, run_config) and agent.tool_use_behavior != "run_llm_again"


def _validate_resumed_session_output_guardrail_safety(
    *,
    agent: Agent[Any],
    run_config: RunConfig,
    session: Session | None,
    run_state: RunState[Any] | None,
) -> None:
    """Reject approval resumes whose current-response boundary is not structurally provable."""
    if run_state is None or not _has_output_guardrails(agent, run_config):
        return
    if not isinstance(run_state._current_step, NextStepInterruption):
        return
    boundary = _current_response_boundary(
        (),
        run_state._last_processed_response,
        run_state,
    )
    if not boundary.proven:
        raise UserError(
            "Cannot resume a serialized approval checkpoint with output guardrails because the "
            "current response boundary cannot be proven. Start a new run from safe input."
        )
    if run_state._current_turn_persisted_item_count > 0 and (
        _should_defer_interrupted_session_items(agent, run_config)
    ):
        if session is not None:
            raise UserError(
                "Cannot resume an approval checkpoint with output guardrails after current-turn "
                "items were persisted. Start a new run from safe input."
            )
        # A detached Session cannot contribute its old persisted prefix to this run.
        run_state._current_turn_persisted_item_count = 0


def _identity_sequence_start(
    container: Sequence[RunItem],
    sequence: Sequence[RunItem],
) -> int | None:
    if not sequence or len(sequence) > len(container):
        return None
    for start in range(len(container) - len(sequence) + 1):
        if all(container[start + offset] is item for offset, item in enumerate(sequence)):
            return start
    return None


def _current_response_boundary(
    new_items: Sequence[RunItem],
    processed_response: ProcessedResponse | None,
    run_state: RunState[Any] | None,
) -> _CurrentResponseBoundary:
    """Collect one response using only SDK lifecycle position and exact object identity."""
    processed_items = tuple(processed_response.new_items) if processed_response is not None else ()
    supplied_items = tuple(new_items)
    supplied_start = _identity_sequence_start(supplied_items, processed_items)
    response_items = (
        supplied_items[supplied_start:] if supplied_start is not None else supplied_items
    )
    generated_start = None
    session_start = None
    proven = run_state is None or not processed_items or supplied_start is not None
    suffixes: list[RunItem] = []
    if run_state is not None:
        anchor_items = processed_items or response_items
        if anchor_items:
            generated_start = _identity_sequence_start(run_state._generated_items, anchor_items)
            session_start = _identity_sequence_start(run_state._session_items, anchor_items)
            if generated_start is not None:
                suffixes.extend(run_state._generated_items[generated_start:])
                proven = True
            if session_start is not None:
                suffixes.extend(run_state._session_items[session_start:])
                proven = True
        if generated_start is None and session_start is None and run_state._current_turn == 1:
            current_response_prefix = tuple(run_state._generated_items[: len(processed_items)])
            if len(current_response_prefix) == len(processed_items) and all(
                type(actual) is type(expected)
                for actual, expected in zip(current_response_prefix, processed_items, strict=False)
            ):
                # Serialization rebuilds item identities, but turn one has no accepted prefix.
                processed_items = current_response_prefix
                generated_start = 0
                session_start = 0
                suffixes.extend(run_state._generated_items)
                suffixes.extend(run_state._session_items)
                proven = True

    current_items: list[RunItem] = []
    seen: set[int] = set()
    for item in (*processed_items, *suffixes, *response_items):
        if id(item) in seen:
            continue
        seen.add(id(item))
        current_items.append(item)
    return _CurrentResponseBoundary(
        items=tuple(current_items),
        processed_items=processed_items,
        generated_start=generated_start,
        session_start=session_start,
        proven=proven,
    )


def _current_response_items_for_persistence(
    items: Sequence[RunItem],
    processed_response: ProcessedResponse | None,
    run_state: RunState[Any] | None = None,
) -> list[RunItem]:
    """Return the complete current response or fail before using an ambiguous boundary."""
    boundary = _current_response_boundary(items, processed_response, run_state)
    if not boundary.proven:
        raise UserError(
            "Cannot persist an ambiguous resumed response with output guardrails. "
            "Start a new run from safe input."
        )
    return list(boundary.items)


def _final_turn_items_for_persistence(
    items: Sequence[RunItem],
    processed_response: ProcessedResponse | None,
    run_state: RunState[Any] | None,
    agent: Agent[Any],
    run_config: RunConfig,
) -> list[RunItem]:
    """Use released resumed suffix persistence unless output guardrails defer the response."""
    if not _has_output_guardrails(agent, run_config):
        return list(items)
    return _current_response_items_for_persistence(items, processed_response, run_state)


def _is_terminal_tool_output_response(
    items: Sequence[RunItem],
    processed_response: ProcessedResponse | None,
    run_state: RunState[Any] | None = None,
) -> bool:
    """Return whether the structurally owned current response produced a tool final output."""
    boundary = _current_response_boundary(items, processed_response, run_state)
    return boundary.proven and any(isinstance(item, ToolCallOutputItem) for item in boundary.items)


def _prepare_blocked_output_snapshot(
    boundary: _CurrentResponseBoundary,
    model_response: ModelResponse | None,
    blocked_message: str,
) -> _BlockedOutputSnapshot:
    """Build an allowlist-only function call/output snapshot before changing live state."""
    current_items = list(boundary.items)
    if any(item.type == "reasoning_item" for item in current_items):
        raise AgentsException("Cannot sanitize a response containing reasoning items.")
    retained_indexes = {
        index for index, item in enumerate(current_items) if item.type in _SIDE_EFFECT_ITEM_TYPES
    }
    replacements: dict[int, RunItem] = {}
    calls_by_id: dict[str, int] = {}
    outputs_by_id: dict[str, int] = {}
    for index in sorted(retained_indexes):
        item = current_items[index]
        if isinstance(item, ToolCallItem):
            payload = blocked_function_call_payload(item.raw_item)
            call_id = cast(str, payload["call_id"])
            if call_id in calls_by_id:
                raise AgentsException("Cannot sanitize duplicate function calls.")
            calls_by_id[call_id] = index
            replacements[index] = ToolCallItem(
                agent=item.agent,
                raw_item=cast(Any, payload),
                description=item.description,
                title=item.title,
                tool_origin=item.tool_origin,
                _resolved_tool_name=item._resolved_tool_name,
            )
        elif isinstance(item, ToolCallOutputItem):
            payload = blocked_function_output_payload(item.raw_item)
            payload["output"] = blocked_message
            call_id = cast(str, payload["call_id"])
            if call_id in outputs_by_id:
                raise AgentsException("Cannot sanitize duplicate function outputs.")
            outputs_by_id[call_id] = index
            replacements[index] = ToolCallOutputItem(
                agent=item.agent,
                raw_item=cast(Any, payload),
                output=blocked_message,
                tool_origin=item.tool_origin,
                custom_data=None,
            )
        else:
            raise AgentsException("Cannot sanitize an unsupported side-effect item.")

    if not outputs_by_id or set(outputs_by_id) - set(calls_by_id):
        raise AgentsException("Cannot sanitize an incomplete function call/output batch.")
    retained_indexes = {
        index
        for call_id in outputs_by_id
        for index in (calls_by_id[call_id], outputs_by_id[call_id])
    }
    retained_items = tuple(replacements[index] for index in sorted(retained_indexes))
    processed_indexes = {id(item): index for index, item in enumerate(current_items)}
    retained_processed_items = tuple(
        replacements.get(processed_indexes[id(item)], item)
        for item in boundary.processed_items
        if processed_indexes.get(id(item)) in retained_indexes
    )
    sanitized_response = None
    if model_response is not None:
        sanitized_response = ModelResponse(
            output=cast(Any, [item.raw_item for item in retained_processed_items]),
            usage=model_response.usage,
            response_id=model_response.response_id,
            request_id=model_response.request_id,
            raw_usage=model_response.raw_usage,
        )
    return _BlockedOutputSnapshot(
        items=retained_items,
        processed_items=retained_processed_items,
        model_response=sanitized_response,
    )


def _blocked_output_owner_prefix(items: list[_OwnerItemT], start: int | None) -> list[_OwnerItemT]:
    """Copy a structurally captured prefix without consulting item values or identities."""
    if start is None or start < 0 or start > len(items):
        return []
    return list.__getitem__(items, slice(0, start))


def _blocked_output_failure_items(
    items: list[RunItem],
    retained_items: Sequence[RunItem],
    owner_starts: _BlockedOutputOwnerStarts,
) -> list[RunItem]:
    """Build the non-streamed accepted prefix plus the data-free current response."""
    return [
        *_blocked_output_owner_prefix(items, owner_starts.nonstreamed_session_items),
        *retained_items,
    ]


def _prepare_blocked_output_owner_prefixes(
    run_state: RunState[Any] | None,
    streamed_result: RunResultStreaming | None,
    owner_starts: _BlockedOutputOwnerStarts,
) -> _BlockedOutputOwnerPrefixes:
    """Allocate every accepted owner prefix before snapshot application begins."""
    return _BlockedOutputOwnerPrefixes(
        run_state_generated_items=(
            _blocked_output_owner_prefix(
                run_state._generated_items,
                owner_starts.run_state_generated_items,
            )
            if run_state is not None
            else []
        ),
        run_state_session_items=(
            _blocked_output_owner_prefix(
                run_state._session_items,
                owner_starts.run_state_session_items,
            )
            if run_state is not None
            else []
        ),
        run_state_model_responses=(
            _blocked_output_owner_prefix(
                run_state._model_responses,
                owner_starts.run_state_model_responses,
            )
            if run_state is not None
            else []
        ),
        run_state_tool_output_guardrail_results=(
            _blocked_output_owner_prefix(
                run_state._tool_output_guardrail_results,
                owner_starts.run_state_tool_output_guardrail_results,
            )
            if run_state is not None
            else []
        ),
        streamed_new_items=(
            _blocked_output_owner_prefix(
                streamed_result.new_items,
                owner_starts.streamed_new_items,
            )
            if streamed_result is not None
            else []
        ),
        streamed_model_input_items=(
            _blocked_output_owner_prefix(
                streamed_result._model_input_items,
                owner_starts.streamed_model_input_items,
            )
            if streamed_result is not None
            else []
        ),
        streamed_raw_responses=(
            _blocked_output_owner_prefix(
                streamed_result.raw_responses,
                owner_starts.streamed_raw_responses,
            )
            if streamed_result is not None
            else []
        ),
        streamed_tool_output_guardrail_results=(
            _blocked_output_owner_prefix(
                streamed_result.tool_output_guardrail_results,
                owner_starts.streamed_tool_output_guardrail_results,
            )
            if streamed_result is not None
            else []
        ),
    )


def _prepare_blocked_output_cleanup_plan(
    run_state: RunState[Any] | None,
    streamed_result: RunResultStreaming | None,
    prefixes: _BlockedOutputOwnerPrefixes,
) -> _BlockedOutputOwnerPlan:
    """Prepare accepted-prefix cleanup containers before snapshot application begins."""
    assignments: list[tuple[Any, str, Any]] = []
    if run_state is not None:
        assignments.extend(
            [
                (run_state, "_generated_items", prefixes.run_state_generated_items),
                (run_state, "_session_items", prefixes.run_state_session_items),
                (run_state, "_model_responses", prefixes.run_state_model_responses),
                (run_state, "_last_processed_response", None),
                (run_state, "_current_step", None),
                (run_state, "_generated_items_last_processed_marker", None),
                (
                    run_state,
                    "_tool_output_guardrail_results",
                    prefixes.run_state_tool_output_guardrail_results,
                ),
            ]
        )
    if streamed_result is not None:
        assignments.extend(
            [
                (streamed_result, "new_items", prefixes.streamed_new_items),
                (streamed_result, "raw_responses", prefixes.streamed_raw_responses),
                (
                    streamed_result,
                    "_model_input_items",
                    prefixes.streamed_model_input_items,
                ),
                (streamed_result, "_last_processed_response", None),
                (
                    streamed_result,
                    "tool_output_guardrail_results",
                    prefixes.streamed_tool_output_guardrail_results,
                ),
            ]
        )
    return _BlockedOutputOwnerPlan(assignments=tuple(assignments))


def _sever_blocked_output_replay_graph(cleanup_plan: _BlockedOutputOwnerPlan) -> None:
    """Best-effort leaf cleanup using only containers allocated before application."""
    for owner, field, value in cleanup_plan.assignments:
        try:
            object.__setattr__(owner, field, value)
        except BaseException:
            continue


def _data_free_tool_output_guardrail_results(
    results: Sequence[ToolOutputGuardrailResult],
    blocked_message: str,
) -> tuple[ToolOutputGuardrailResult, ...]:
    """Rebuild current-turn tool guardrail results without retaining caller output data."""
    replacements: list[ToolOutputGuardrailResult] = []
    try:
        for result in results:
            if not isinstance(result, ToolOutputGuardrailResult):
                return ()
            original_output = object.__getattribute__(result, "output")
            behavior = object.__getattribute__(original_output, "behavior")
            if type(behavior) is not dict:
                return ()
            behavior_type = _exact_dict_field(behavior, "type")
            if type(behavior_type) is not str:
                return ()
            if str.__eq__(behavior_type, "allow") is True:
                sanitized_output = ToolGuardrailFunctionOutput.allow(
                    output_info=blocked_message,
                )
            elif str.__eq__(behavior_type, "reject_content") is True:
                sanitized_output = ToolGuardrailFunctionOutput.reject_content(
                    message=blocked_message,
                    output_info=blocked_message,
                )
            elif str.__eq__(behavior_type, "raise_exception") is True:
                sanitized_output = ToolGuardrailFunctionOutput.raise_exception(
                    output_info=blocked_message,
                )
            else:
                return ()
            replacements.append(
                ToolOutputGuardrailResult(
                    guardrail=object.__getattribute__(result, "guardrail"),
                    output=sanitized_output,
                )
            )
    except Exception:
        return ()
    return tuple(replacements)


def _prepare_blocked_output_owner_plan(
    boundary: _CurrentResponseBoundary,
    snapshot: _BlockedOutputSnapshot | None,
    model_response: ModelResponse | None,
    run_state: RunState[Any] | None,
    streamed_result: RunResultStreaming | None,
    prefixes: _BlockedOutputOwnerPrefixes,
    cleanup_plan: _BlockedOutputOwnerPlan,
    blocked_message: str,
) -> _BlockedOutputOwnerPlan:
    """Build every owner replacement before applying any of them."""
    safe_items = list(snapshot.items) if snapshot is not None else []
    safe_response = snapshot.model_response if snapshot is not None else None
    assignments: list[tuple[Any, str, Any]] = []
    if streamed_result is not None:
        public_results = streamed_result.tool_output_guardrail_results
        current_results = list.__getitem__(
            public_results,
            slice(len(prefixes.streamed_tool_output_guardrail_results), None),
        )
    elif run_state is not None:
        current_results = list.__getitem__(
            run_state._tool_output_guardrail_results,
            slice(len(prefixes.run_state_tool_output_guardrail_results), None),
        )
    else:
        current_results = []
    safe_tool_output_guardrail_results = _data_free_tool_output_guardrail_results(
        current_results,
        blocked_message,
    )
    if streamed_result is not None:
        public_safe_results = [
            *prefixes.streamed_tool_output_guardrail_results,
            *safe_tool_output_guardrail_results,
        ]
    else:
        public_safe_results = []

    if run_state is not None:
        if boundary.proven:
            responses = [
                *prefixes.run_state_model_responses,
                *(
                    [safe_response]
                    if model_response is not None and safe_response is not None
                    else []
                ),
            ]
            run_state_safe_results = [
                *prefixes.run_state_tool_output_guardrail_results,
                *safe_tool_output_guardrail_results,
            ]
            assignments.extend(
                [
                    (
                        run_state,
                        "_generated_items",
                        [*prefixes.run_state_generated_items, *safe_items],
                    ),
                    (
                        run_state,
                        "_session_items",
                        [*prefixes.run_state_session_items, *safe_items],
                    ),
                    (run_state, "_model_responses", responses),
                    (run_state, "_last_processed_response", None),
                    (run_state, "_current_step", None),
                    (run_state, "_generated_items_last_processed_marker", None),
                    (run_state, "_tool_output_guardrail_results", run_state_safe_results),
                ]
            )
        else:
            return cleanup_plan

    if streamed_result is not None:
        responses = [
            *prefixes.streamed_raw_responses,
            *([safe_response] if model_response is not None and safe_response is not None else []),
        ]
        assignments.extend(
            [
                (
                    streamed_result,
                    "new_items",
                    [*prefixes.streamed_new_items, *safe_items],
                ),
                (
                    streamed_result,
                    "_model_input_items",
                    [*prefixes.streamed_model_input_items, *safe_items],
                ),
                (streamed_result, "raw_responses", responses),
                (streamed_result, "_last_processed_response", None),
                (
                    streamed_result,
                    "tool_output_guardrail_results",
                    public_safe_results,
                ),
            ]
        )
    return _BlockedOutputOwnerPlan(assignments=tuple(assignments))


def _apply_blocked_output_owner_plan(plan: _BlockedOutputOwnerPlan) -> None:
    """Apply only values that were fully constructed before the first owner swap."""
    for owner, field, value in plan.assignments:
        object.__setattr__(owner, field, value)


def _retained_items_for_blocked_response(
    items: list[RunItem],
    model_response: ModelResponse | None,
    run_state: RunState[Any] | None = None,
    processed_response: ProcessedResponse | None = None,
    streamed_result: RunResultStreaming | None = None,
    owner_starts: _BlockedOutputOwnerStarts | None = None,
    blocked_message: str = OUTPUT_GUARDRAIL_BLOCKED_TOOL_OUTPUT,
) -> list[RunItem]:
    """Return a complete data-free response or discard the entire unsupported suffix."""
    boundary = _current_response_boundary(items, processed_response, run_state)
    prefixes = _prepare_blocked_output_owner_prefixes(
        run_state,
        streamed_result,
        owner_starts if owner_starts is not None else _BlockedOutputOwnerStarts(),
    )
    cleanup_plan = _prepare_blocked_output_cleanup_plan(run_state, streamed_result, prefixes)
    snapshot: _BlockedOutputSnapshot | None = None
    try:
        if boundary.proven:
            snapshot = _prepare_blocked_output_snapshot(
                boundary,
                model_response,
                blocked_message,
            )
    except Exception:
        snapshot = None
    except BaseException:
        _sever_blocked_output_replay_graph(cleanup_plan)
        raise
    try:
        owner_plan = _prepare_blocked_output_owner_plan(
            boundary,
            snapshot,
            model_response,
            run_state,
            streamed_result,
            prefixes,
            cleanup_plan,
            blocked_message,
        )
        _apply_blocked_output_owner_plan(owner_plan)
    except Exception as error:
        _sever_blocked_output_replay_graph(cleanup_plan)
        raise _prepare_data_redacted_error(error) from None
    except BaseException:
        _sever_blocked_output_replay_graph(cleanup_plan)
        raise
    return list(snapshot.items) if snapshot is not None else []

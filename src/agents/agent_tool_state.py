from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

    from .result import RunResult, RunResultStreaming

# Ephemeral maps linking tool call objects to nested agent results within the same run.
# Store by object identity to avoid collisions when call IDs repeat across tool calls.
_agent_tool_run_results_by_obj: dict[int, RunResult | RunResultStreaming] = {}
_agent_tool_run_results_by_call_id: dict[str, set[int]] = {}


def _index_agent_tool_run_result(tool_call_id: str, tool_call_obj_id: int) -> None:
    """Track tool call objects by call ID for fallback lookup."""
    _agent_tool_run_results_by_call_id.setdefault(tool_call_id, set()).add(tool_call_obj_id)


def _drop_agent_tool_run_result(tool_call_id: str | None, tool_call_obj_id: int) -> None:
    """Remove a tool call object from the fallback index."""
    if tool_call_id is None:
        return
    call_ids = _agent_tool_run_results_by_call_id.get(tool_call_id)
    if not call_ids:
        return
    call_ids.discard(tool_call_obj_id)
    if not call_ids:
        _agent_tool_run_results_by_call_id.pop(tool_call_id, None)


def record_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall, run_result: RunResult | RunResultStreaming
) -> None:
    """Store the nested agent run result by tool call identity."""
    tool_call_obj_id = id(tool_call)
    _agent_tool_run_results_by_obj[tool_call_obj_id] = run_result
    if isinstance(tool_call.call_id, str):
        _index_agent_tool_run_result(tool_call.call_id, tool_call_obj_id)


def consume_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall,
) -> RunResult | RunResultStreaming | None:
    """Return and drop the stored nested agent run result for the given tool call ID."""
    obj_id = id(tool_call)
    run_result = _agent_tool_run_results_by_obj.pop(obj_id, None)
    if run_result is not None:
        _drop_agent_tool_run_result(tool_call.call_id, obj_id)
        return run_result

    call_id = tool_call.call_id
    if not call_id:
        return None

    candidate_ids = _agent_tool_run_results_by_call_id.get(call_id)
    if not candidate_ids:
        return None
    if len(candidate_ids) != 1:
        return None

    candidate_id = next(iter(candidate_ids))
    _agent_tool_run_results_by_call_id.pop(call_id, None)
    return _agent_tool_run_results_by_obj.pop(candidate_id, None)


def peek_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall,
) -> RunResult | RunResultStreaming | None:
    """Return the stored nested agent run result without removing it."""
    obj_id = id(tool_call)
    run_result = _agent_tool_run_results_by_obj.get(obj_id)
    if run_result is not None:
        return run_result

    call_id = tool_call.call_id
    if not call_id:
        return None

    candidate_ids = _agent_tool_run_results_by_call_id.get(call_id)
    if not candidate_ids:
        return None
    if len(candidate_ids) != 1:
        return None

    candidate_id = next(iter(candidate_ids))
    return _agent_tool_run_results_by_obj.get(candidate_id)


def drop_agent_tool_run_result(tool_call: ResponseFunctionToolCall) -> None:
    """Drop the stored nested agent run result, if present."""
    obj_id = id(tool_call)
    run_result = _agent_tool_run_results_by_obj.pop(obj_id, None)
    if run_result is not None:
        _drop_agent_tool_run_result(tool_call.call_id, obj_id)
        return

    call_id = tool_call.call_id
    if not call_id:
        return
    candidate_ids = _agent_tool_run_results_by_call_id.get(call_id)
    if not candidate_ids:
        return
    if len(candidate_ids) != 1:
        return

    candidate_id = next(iter(candidate_ids))
    _agent_tool_run_results_by_call_id.pop(call_id, None)
    _agent_tool_run_results_by_obj.pop(candidate_id, None)

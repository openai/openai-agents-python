from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._tool_invocation import tool_invocation_identity_and_scope

if TYPE_CHECKING:
    from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

    from .result import RunResult, RunResultStreaming

ToolCallSignature = tuple[str, str, str, str, str | None, str | None]
ScopedToolCallSignature = tuple[str | None, ToolCallSignature]


@dataclass
class _AgentToolResumeCheckpoint:
    state: Any
    approval_identities: frozenset[tuple[str, str, str, str]]

    @property
    def interruptions(self) -> list[Any]:
        interruptions = self.state.get_interruptions()
        return interruptions if isinstance(interruptions, list) else []

    def to_state(self) -> Any:
        return self.state


_AGENT_TOOL_STATE_SCOPE_ATTR = "_agent_tool_state_scope_id"

# Ephemeral maps linking tool call objects to nested agent results within the same run.
# Store by object identity, and index by a stable signature to avoid call ID collisions.
_agent_tool_run_results_by_obj: dict[
    int, RunResult | RunResultStreaming | _AgentToolResumeCheckpoint
] = {}
_agent_tool_run_results_by_signature: dict[
    ScopedToolCallSignature,
    set[int],
] = {}
_agent_tool_run_result_signature_by_obj: dict[
    int,
    ScopedToolCallSignature,
] = {}
_agent_tool_call_refs_by_obj: dict[int, weakref.ReferenceType[ResponseFunctionToolCall]] = {}


def get_agent_tool_state_scope(context: Any) -> str | None:
    """Read the private agent-tool cache scope id from a context wrapper."""
    scope_id = getattr(context, _AGENT_TOOL_STATE_SCOPE_ATTR, None)
    return scope_id if isinstance(scope_id, str) else None


def set_agent_tool_state_scope(context: Any, scope_id: str | None) -> None:
    """Attach or clear the private agent-tool cache scope id on a context wrapper."""
    if context is None:
        return
    if scope_id is None:
        try:
            delattr(context, _AGENT_TOOL_STATE_SCOPE_ATTR)
        except Exception:
            return
        return
    try:
        setattr(context, _AGENT_TOOL_STATE_SCOPE_ATTR, scope_id)
    except Exception:
        return


def _tool_call_signature(
    tool_call: ResponseFunctionToolCall,
) -> ToolCallSignature:
    """Build a stable signature for fallback lookup across tool call instances."""
    return (
        tool_call.call_id,
        tool_call.name,
        tool_call.arguments,
        tool_call.type,
        tool_call.id,
        tool_call.status,
    )


def _scoped_tool_call_signature(
    tool_call: ResponseFunctionToolCall, *, scope_id: str | None
) -> ScopedToolCallSignature:
    """Build a scope-qualified signature so independently restored states do not collide."""
    return (scope_id, _tool_call_signature(tool_call))


def _index_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall,
    tool_call_obj_id: int,
    *,
    scope_id: str | None,
) -> None:
    """Track tool call objects by signature for fallback lookup."""
    signature = _scoped_tool_call_signature(tool_call, scope_id=scope_id)
    _agent_tool_run_result_signature_by_obj[tool_call_obj_id] = signature
    _agent_tool_run_results_by_signature.setdefault(signature, set()).add(tool_call_obj_id)


def _drop_agent_tool_run_result(tool_call_obj_id: int) -> None:
    """Remove a tool call object from the fallback index."""
    tool_call_refs = _agent_tool_call_refs_by_obj
    if isinstance(tool_call_refs, dict):
        tool_call_refs.pop(tool_call_obj_id, None)
    signature_by_obj = _agent_tool_run_result_signature_by_obj
    if not isinstance(signature_by_obj, dict):
        return
    signature = signature_by_obj.pop(tool_call_obj_id, None)
    if signature is None:
        return
    results_by_signature = _agent_tool_run_results_by_signature
    if not isinstance(results_by_signature, dict):
        return
    candidate_ids = results_by_signature.get(signature)
    if not candidate_ids:
        return
    candidate_ids.discard(tool_call_obj_id)
    if not candidate_ids:
        results_by_signature.pop(signature, None)


def _register_tool_call_ref(tool_call: ResponseFunctionToolCall, tool_call_obj_id: int) -> None:
    """Tie cached nested run results to the tool call lifetime to avoid leaks."""

    def _on_tool_call_gc(_ref: weakref.ReferenceType[ResponseFunctionToolCall]) -> None:
        run_results = _agent_tool_run_results_by_obj
        if isinstance(run_results, dict):
            run_results.pop(tool_call_obj_id, None)
        _drop_agent_tool_run_result(tool_call_obj_id)

    _agent_tool_call_refs_by_obj[tool_call_obj_id] = weakref.ref(tool_call, _on_tool_call_gc)


def record_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall,
    run_result: RunResult | RunResultStreaming | _AgentToolResumeCheckpoint,
    *,
    scope_id: str | None = None,
) -> None:
    """Store the nested agent run result by tool call identity."""
    tool_call_obj_id = id(tool_call)
    _agent_tool_run_results_by_obj[tool_call_obj_id] = run_result
    _index_agent_tool_run_result(tool_call, tool_call_obj_id, scope_id=scope_id)
    _register_tool_call_ref(tool_call, tool_call_obj_id)


def record_agent_tool_resume_state(
    tool_call: ResponseFunctionToolCall,
    state: Any,
    *,
    scope_id: str | None = None,
    approval_items: list[Any] | None = None,
) -> None:
    """Keep a live nested RunState checkpoint while an approved resume is in flight."""
    resolved_approval_items = approval_items
    if resolved_approval_items is None:
        get_interruptions = getattr(state, "get_interruptions", None)
        interruptions = get_interruptions() if callable(get_interruptions) else []
        resolved_approval_items = interruptions if isinstance(interruptions, list) else []
    approval_identities = frozenset(
        identity
        for item in resolved_approval_items
        if (
            identity := tool_invocation_identity_and_scope(
                item.raw_item,
                tool_lookup_key=getattr(item, "tool_lookup_key", None),
                tool_name=getattr(item, "tool_name", None),
            )
        )
        is not None
    )
    record_agent_tool_run_result(
        tool_call,
        _AgentToolResumeCheckpoint(state, approval_identities),
        scope_id=scope_id,
    )


def get_agent_tool_resume_state(run_result: Any) -> Any | None:
    """Return the live nested RunState stored in an in-flight resume checkpoint."""
    return run_result.state if isinstance(run_result, _AgentToolResumeCheckpoint) else None


def agent_tool_resume_checkpoint_owns_approval(run_result: Any, approval_item: Any) -> bool:
    """Return whether an in-flight nested resume accepted the approval item."""
    if not isinstance(run_result, _AgentToolResumeCheckpoint):
        return False
    identity = tool_invocation_identity_and_scope(
        approval_item.raw_item,
        tool_lookup_key=getattr(approval_item, "tool_lookup_key", None),
        tool_name=getattr(approval_item, "tool_name", None),
    )
    return identity is not None and identity in run_result.approval_identities


def _tool_call_obj_matches_scope(tool_call_obj_id: int, *, scope_id: str | None) -> bool:
    scoped_signature = _agent_tool_run_result_signature_by_obj.get(tool_call_obj_id)
    if scoped_signature is None:
        # Fallback for unindexed entries.
        return scope_id is None
    return scoped_signature[0] == scope_id


def consume_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall,
    *,
    scope_id: str | None = None,
) -> RunResult | RunResultStreaming | _AgentToolResumeCheckpoint | None:
    """Return and drop the stored nested agent run result for the given tool call."""
    obj_id = id(tool_call)
    if _tool_call_obj_matches_scope(obj_id, scope_id=scope_id):
        run_result = _agent_tool_run_results_by_obj.pop(obj_id, None)
        if run_result is not None:
            _drop_agent_tool_run_result(obj_id)
            return run_result

    signature = _scoped_tool_call_signature(tool_call, scope_id=scope_id)
    candidate_ids = _agent_tool_run_results_by_signature.get(signature)
    if not candidate_ids:
        return None
    if len(candidate_ids) != 1:
        return None

    candidate_id = next(iter(candidate_ids))
    _agent_tool_run_results_by_signature.pop(signature, None)
    _agent_tool_run_result_signature_by_obj.pop(candidate_id, None)
    _agent_tool_call_refs_by_obj.pop(candidate_id, None)
    return _agent_tool_run_results_by_obj.pop(candidate_id, None)


def peek_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall,
    *,
    scope_id: str | None = None,
) -> RunResult | RunResultStreaming | _AgentToolResumeCheckpoint | None:
    """Return the stored nested agent run result without removing it."""
    obj_id = id(tool_call)
    if _tool_call_obj_matches_scope(obj_id, scope_id=scope_id):
        run_result = _agent_tool_run_results_by_obj.get(obj_id)
        if run_result is not None:
            return run_result

    signature = _scoped_tool_call_signature(tool_call, scope_id=scope_id)
    candidate_ids = _agent_tool_run_results_by_signature.get(signature)
    if not candidate_ids:
        return None
    if len(candidate_ids) != 1:
        return None

    candidate_id = next(iter(candidate_ids))
    return _agent_tool_run_results_by_obj.get(candidate_id)


def drop_agent_tool_run_result(
    tool_call: ResponseFunctionToolCall,
    *,
    scope_id: str | None = None,
) -> None:
    """Drop the stored nested agent run result, if present."""
    obj_id = id(tool_call)
    if _tool_call_obj_matches_scope(obj_id, scope_id=scope_id):
        run_result = _agent_tool_run_results_by_obj.pop(obj_id, None)
        if run_result is not None:
            _drop_agent_tool_run_result(obj_id)
            return

    signature = _scoped_tool_call_signature(tool_call, scope_id=scope_id)
    candidate_ids = _agent_tool_run_results_by_signature.get(signature)
    if not candidate_ids:
        return
    if len(candidate_ids) != 1:
        return

    candidate_id = next(iter(candidate_ids))
    _agent_tool_run_results_by_signature.pop(signature, None)
    _agent_tool_run_result_signature_by_obj.pop(candidate_id, None)
    _agent_tool_call_refs_by_obj.pop(candidate_id, None)
    _agent_tool_run_results_by_obj.pop(candidate_id, None)

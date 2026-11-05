from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from .config import TracingConfig
from .create import custom_span, get_current_trace, trace
from .setup import get_trace_provider
from .traces import (
    Trace,
    TraceState,
    _hash_tracing_api_key,
    _trace_id_was_started,
    reattach_trace,
)


def _get_tracing_api_key(tracing: TracingConfig | None) -> str | None:
    return tracing.get("api_key") if tracing is not None else None


def _trace_state_matches_effective_settings(
    *,
    trace_state: TraceState,
    workflow_name: str,
    trace_id: str | None,
    group_id: str | None,
    metadata: dict[str, Any] | None,
    tracing: TracingConfig | None,
) -> bool:
    if trace_state.trace_id is None or trace_state.trace_id != trace_id:
        return False
    if trace_state.workflow_name != workflow_name:
        return False
    if trace_state.group_id != group_id:
        return False
    if trace_state.metadata != metadata:
        return False
    tracing_api_key = _get_tracing_api_key(tracing)
    if trace_state.tracing_api_key is not None:
        return trace_state.tracing_api_key == tracing_api_key
    if trace_state.tracing_api_key_hash is not None:
        # A fingerprint lets stripped RunState snapshots prove the caller
        # re-supplied the same explicit key.
        return trace_state.tracing_api_key_hash == _hash_tracing_api_key(tracing_api_key)
    return tracing_api_key is None


def create_trace_for_run(
    *,
    workflow_name: str,
    trace_id: str | None,
    group_id: str | None,
    metadata: dict[str, Any] | None,
    tracing: TracingConfig | None,
    disabled: bool,
    trace_state: TraceState | None = None,
    reattach_resumed_trace: bool = False,
) -> Trace | None:
    """Return a run trace when none is active, or a disabled trace to mask an active one."""
    if disabled:
        return get_trace_provider().create_trace(
            name=workflow_name,
            trace_id=trace_id,
            group_id=group_id,
            metadata=metadata,
            tracing=tracing,
            disabled=True,
        )
    current_trace = get_current_trace()
    if current_trace is not None:
        return None

    if (
        reattach_resumed_trace
        and trace_state is not None
        and _trace_id_was_started(trace_state.trace_id)
        and _trace_state_matches_effective_settings(
            trace_state=trace_state,
            workflow_name=workflow_name,
            trace_id=trace_id,
            group_id=group_id,
            metadata=metadata,
            tracing=tracing,
        )
    ):
        # Reuse the live key because secure snapshots may persist only the
        # fingerprint, not the secret itself.
        return reattach_trace(trace_state, tracing_api_key=_get_tracing_api_key(tracing))

    return trace(
        workflow_name=workflow_name,
        trace_id=trace_id,
        group_id=group_id,
        metadata=metadata,
        tracing=tracing,
        disabled=disabled,
    )


class TraceCtxManager:
    """Create a trace when none exists and manage its lifecycle for a run."""

    def __init__(
        self,
        workflow_name: str,
        trace_id: str | None,
        group_id: str | None,
        metadata: dict[str, Any] | None,
        tracing: TracingConfig | None,
        disabled: bool,
        trace_state: TraceState | None = None,
        reattach_resumed_trace: bool = False,
    ):
        self.trace: Trace | None = None
        self.workflow_name = workflow_name
        self.trace_id = trace_id
        self.group_id = group_id
        self.metadata = metadata
        self.tracing = tracing
        self.disabled = disabled
        self.trace_state = trace_state
        self.reattach_resumed_trace = reattach_resumed_trace
        self._exit_stack = ExitStack()

    def __enter__(self) -> TraceCtxManager:
        self.trace = create_trace_for_run(
            workflow_name=self.workflow_name,
            trace_id=self.trace_id,
            group_id=self.group_id,
            metadata=self.metadata,
            tracing=self.tracing,
            disabled=self.disabled,
            trace_state=self.trace_state,
            reattach_resumed_trace=self.reattach_resumed_trace,
        )
        with ExitStack() as stack:
            if self.trace is not None:
                self.trace.start(mark_as_current=True)
                stack.callback(self.trace.finish, reset_current=True)
            if self.disabled:
                # Also hide the caller's span from code that consults it directly.
                stack.enter_context(custom_span(name="Agent run", disabled=True))
            self._exit_stack = stack.pop_all()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._exit_stack.__exit__(exc_type, exc_val, exc_tb)

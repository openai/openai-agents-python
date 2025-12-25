from __future__ import annotations

from typing import Any

from .config import TracingConfig
from .create import get_current_trace, trace
from .traces import Trace


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
    ):
        self.trace: Trace | None = None
        self.workflow_name = workflow_name
        self.trace_id = trace_id
        self.group_id = group_id
        self.metadata = metadata
        self.tracing = tracing
        self.disabled = disabled

    def __enter__(self) -> TraceCtxManager:
        current_trace = get_current_trace()
        if not current_trace:
            self.trace = trace(
                workflow_name=self.workflow_name,
                trace_id=self.trace_id,
                group_id=self.group_id,
                metadata=self.metadata,
                tracing=self.tracing,
                disabled=self.disabled,
            )
            assert self.trace is not None
            self.trace.start(mark_as_current=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.trace:
            self.trace.finish(reset_current=True)

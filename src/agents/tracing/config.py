from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from typing_extensions import TypedDict

from .scope import Scope

OperationDetail = Literal["metadata", "content"]
_operation_detail: ContextVar[OperationDetail | None] = ContextVar("operation_detail", default=None)
_metadata_only_runs: ContextVar[bool] = ContextVar("metadata_only_runs", default=False)


@contextmanager
def metadata_only_run_tracing() -> Iterator[None]:
    """Limit automatic model and tool capture for Runner calls in this context.

    A child config can override a parent's operation_detail. In this scope,
    nested Runner calls and agents used as tools use metadata detail with
    trace_include_sensitive_data=False even if they request content.
    Disabled tracing stays disabled.

    Start Runner.run_streamed() and drain result.stream_events() inside the
    scope. New asyncio tasks inherit it. Independent resumes and remote
    services need their own scope.

    The scope does not sanitize raw stream events, returned data, saved state,
    exceptions, logs, names, IDs, custom spans or trace metadata.
    """
    token = _metadata_only_runs.set(True)
    try:
        yield
    finally:
        _metadata_only_runs.reset(token)


class TracingConfig(TypedDict, total=False):
    """Configuration for tracing behavior and export."""

    api_key: str
    """Optional API key used to export traces."""

    include_task_and_turn_spans: bool
    """Whether the runner creates task and turn spans. Defaults to True when omitted."""

    operation_detail: OperationDetail
    """Select the payloads captured for model calls and tools in this run.

    Omit to use existing SDK payload fields. Explicit content capture requires
    trace_include_sensitive_data in the run configuration. All settings keep
    one span per logical model call, with retries recorded on that span.

    A nested run can override this setting. Use metadata_only_run_tracing()
    to enforce metadata detail across nested Runner calls and agent tools.
    """


def include_task_and_turn_spans(config: TracingConfig | None) -> bool:
    """Resolve whether automatic runner task and turn spans are enabled."""
    return config is None or config.get("include_task_and_turn_spans", True)


def get_operation_detail(*, include_data: bool = True) -> OperationDetail | None:
    """Return the current run's effective capture setting."""
    detail = _operation_detail.get()
    if detail == "content":
        span = Scope.get_current_span()
        trace = Scope.get_current_trace()
        if (
            not include_data
            or (trace is not None and trace.trace_id == "no-op")
            or (span is not None and span.trace_id == "no-op")
        ):
            return "metadata"
    return detail


@contextmanager
def operation_tracing_scope(
    config: TracingConfig | None, include_sensitive: bool
) -> Iterator[None]:
    detail = config.get("operation_detail") if config else None
    if detail is not None and not include_sensitive:
        detail = "metadata"
    token = _operation_detail.set(detail)
    try:
        yield
    finally:
        _operation_detail.reset(token)

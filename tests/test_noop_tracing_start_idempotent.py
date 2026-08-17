from __future__ import annotations

from agents.tracing.scope import Scope
from agents.tracing.span_data import AgentSpanData
from agents.tracing.spans import NoOpSpan
from agents.tracing.traces import NoOpTrace


def test_noop_trace_repeated_start_does_not_overwrite_context_token() -> None:
    trace = NoOpTrace()
    try:
        trace.start(mark_as_current=True)
        trace.start(mark_as_current=True)
        trace.finish(reset_current=True)

        assert Scope.get_current_trace() is None
    finally:
        Scope.set_current_trace(None)


def test_noop_span_repeated_start_does_not_overwrite_context_token() -> None:
    span = NoOpSpan(AgentSpanData(name="test"))
    try:
        span.start(mark_as_current=True)
        span.start(mark_as_current=True)
        span.finish(reset_current=True)

        assert Scope.get_current_span() is None
    finally:
        Scope.set_current_span(None)

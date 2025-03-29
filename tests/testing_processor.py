from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Literal

from agents.tracing import Span, Trace, TracingProcessor

TestSpanProcessorEvent = Literal["trace_start", "trace_end", "span_start", "span_end"]


class SpanProcessorForTests(TracingProcessor):
    """
    A simple processor that stores finished spans in memory.
    This is thread-safe and suitable for tests or basic usage.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Dictionary of trace_id -> list of spans
        self._spans: list[Span[Any]] = []
        self._traces: list[Trace] = []
        self._events: list[TestSpanProcessorEvent] = []

    def on_trace_start(self, trace: Trace) -> None:
        with self._lock:
            self._traces.append(trace)
            self._events.append("trace_start")

    def on_trace_end(self, trace: Trace) -> None:
        with self._lock:
            # We don't append the trace here, we want to do that in on_trace_start
            self._events.append("trace_end")

    def on_span_start(self, span: Span[Any]) -> None:
        with self._lock:
            # Purposely not appending the span here, we want to do that in on_span_end
            self._events.append("span_start")

    def on_span_end(self, span: Span[Any]) -> None:
        with self._lock:
            self._events.append("span_end")
            self._spans.append(span)

    def get_ordered_spans(self, including_empty: bool = False) -> list[Span[Any]]:
        with self._lock:
            spans = [x for x in self._spans if including_empty or x.export()]
            return sorted(spans, key=lambda x: x.started_at or 0)

    def get_traces(self, including_empty: bool = False) -> list[Trace]:
        with self._lock:
            traces = [x for x in self._traces if including_empty or x.export()]
            return traces

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self._traces.clear()
            self._events.clear()

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


SPAN_PROCESSOR_TESTING = SpanProcessorForTests()


def fetch_ordered_spans() -> list[Span[Any]]:
    return SPAN_PROCESSOR_TESTING.get_ordered_spans()


def fetch_traces() -> list[Trace]:
    return SPAN_PROCESSOR_TESTING.get_traces()


def fetch_events() -> list[TestSpanProcessorEvent]:
    return SPAN_PROCESSOR_TESTING._events


def assert_no_spans():
    spans = fetch_ordered_spans()
    if spans:
        raise AssertionError(f"Expected 0 spans, got {len(spans)}")


def assert_no_traces():
    traces = fetch_traces()
    if traces:
        raise AssertionError(f"Expected 0 traces, got {len(traces)}")
    assert_no_spans()


def fetch_normalized_spans(
    keep_span_id: bool = False, keep_trace_id: bool = False
) -> list[dict[str, Any]]:
    nodes: dict[tuple[str, str | None], dict[str, Any]] = {}
    traces = []
    for trace_obj in fetch_traces():
        trace = trace_obj.export()
        assert trace
        assert trace.pop("object") == "trace"
        assert trace["id"].startswith("trace_")
        if not keep_trace_id:
            del trace["id"]
        trace = {k: v for k, v in trace.items() if v is not None}
        nodes[(trace_obj.trace_id, None)] = trace
        traces.append(trace)

    assert traces, "Use assert_no_traces() to check for empty traces"

    for span_obj in fetch_ordered_spans():
        span = span_obj.export()
        assert span
        assert span.pop("object") == "trace.span"
        assert span["id"].startswith("span_")
        if not keep_span_id:
            del span["id"]
        assert datetime.fromisoformat(span.pop("started_at"))
        assert datetime.fromisoformat(span.pop("ended_at"))
        parent_id = span.pop("parent_id")
        assert "type" not in span
        span_data = span.pop("span_data")
        span = {"type": span_data.pop("type")} | {k: v for k, v in span.items() if v is not None}
        span_data = {k: v for k, v in span_data.items() if v is not None}
        if span_data:
            span["data"] = span_data
        nodes[(span_obj.trace_id, span_obj.span_id)] = span
        nodes[(span.pop("trace_id"), parent_id)].setdefault("children", []).append(span)
    return traces

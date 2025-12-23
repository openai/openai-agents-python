import logging
from typing import Any

from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.scope import Scope
from agents.tracing.spans import Span
from agents.tracing.traces import NoOpTrace, Trace, TraceImpl


class DummyProcessor(TracingProcessor):
    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended: list[str] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.started.append(trace.trace_id)

    def on_trace_end(self, trace: Trace) -> None:
        self.ended.append(trace.trace_id)

    def on_span_start(self, span: Span[Any]) -> None:
        return None

    def on_span_end(self, span: Span[Any]) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None


def test_no_op_trace_double_enter_logs_error(caplog) -> None:
    Scope.set_current_trace(None)
    trace = NoOpTrace()
    with caplog.at_level(logging.ERROR):
        trace.start()
        trace.__enter__()
        trace.__enter__()  # Second entry should log missing context token error
    assert trace._started is True
    trace.__exit__(None, None, None)


def test_trace_impl_lifecycle_sets_scope() -> None:
    Scope.set_current_trace(None)
    processor = DummyProcessor()
    trace = TraceImpl(
        name="test-trace",
        trace_id="trace-123",
        group_id="group-1",
        metadata={"k": "v"},
        processor=processor,
    )

    assert Scope.get_current_trace() is None
    with trace as current:
        assert current.trace_id == "trace-123"
        assert Scope.get_current_trace() is trace
        assert processor.started == ["trace-123"]

    assert processor.ended == ["trace-123"]
    assert Scope.get_current_trace() is None
    assert trace.export() == {
        "object": "trace",
        "id": "trace-123",
        "workflow_name": "test-trace",
        "group_id": "group-1",
        "metadata": {"k": "v"},
    }


def test_trace_impl_double_start_and_finish_without_start(caplog) -> None:
    Scope.set_current_trace(None)
    processor = DummyProcessor()
    trace = TraceImpl(
        name="double-start",
        trace_id=None,
        group_id=None,
        metadata=None,
        processor=processor,
    )

    trace.start()
    trace.start()  # should no-op when already started
    trace.finish(reset_current=True)

    with caplog.at_level(logging.ERROR):
        trace._started = True
        trace._prev_context_token = None
        trace.__enter__()  # logs when started but no context token
    trace.finish(reset_current=True)

    fresh = TraceImpl(
        name="finish-no-start",
        trace_id=None,
        group_id=None,
        metadata=None,
        processor=processor,
    )
    fresh.finish(reset_current=True)  # should not raise when never started

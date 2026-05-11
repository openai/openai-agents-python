import contextvars
import logging
from typing import Any, cast

from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.scope import Scope
from agents.tracing.spans import Span
from agents.tracing.traces import NoOpTrace, Trace, TraceImpl, TraceState, reattach_trace


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


def test_no_op_trace_resets_context_on_same_task_generator_exit() -> None:
    """NoOpTrace must reset the current trace on a same-task GeneratorExit.

    A previous version skipped reset unconditionally on GeneratorExit, which
    handles the cross-task GC case but leaves NoOpTrace as the current trace
    after a normal same-context generator close, suppressing later tracing.
    Now the reset is attempted and any ValueError (from a token created in a
    different Context) is swallowed.
    """
    Scope.set_current_trace(None)
    trace = NoOpTrace()
    trace.__enter__()
    assert trace._prev_context_token is not None
    trace.__exit__(GeneratorExit, GeneratorExit(), None)
    # Same-task close: reset succeeded, token consumed, current trace cleared.
    assert trace._prev_context_token is None
    assert Scope.get_current_trace() is None


def test_no_op_trace_swallows_cross_context_reset_error() -> None:
    """A token created in a different Context raises ValueError on reset; swallow it."""
    Scope.set_current_trace(None)
    trace = NoOpTrace()

    other_context = contextvars.copy_context()
    other_context.run(trace.__enter__)
    token = trace._prev_context_token
    assert token is not None

    # Resetting from our context (not the one that set it) raises ValueError;
    # the helper must swallow that and clear the stored token.
    trace.__exit__(GeneratorExit, GeneratorExit(), None)
    assert trace._prev_context_token is None


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


def test_reattached_trace_restores_scope_without_reemitting_processor_events() -> None:
    Scope.set_current_trace(None)
    processor = DummyProcessor()
    original = TraceImpl(
        name="test-trace",
        trace_id="trace-123",
        group_id="group-1",
        metadata={"k": "v"},
        processor=processor,
    )

    with original:
        pass

    restored = reattach_trace(cast(TraceState, TraceState.from_trace(original)))
    assert restored is not None

    with restored as current:
        assert current.trace_id == "trace-123"
        assert Scope.get_current_trace() is restored

    assert processor.started == ["trace-123"]
    assert processor.ended == ["trace-123"]
    assert Scope.get_current_trace() is None

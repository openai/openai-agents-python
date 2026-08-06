import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any, cast

import pytest

from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.scope import Scope
from agents.tracing.spans import Span
from agents.tracing.traces import (
    NoOpTrace,
    ReattachedTrace,
    Trace,
    TraceImpl,
    TraceState,
    reattach_trace,
)


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


def _new_no_op_trace() -> Trace:
    return NoOpTrace()


def _new_trace_impl() -> Trace:
    return TraceImpl(
        name="generator-exit",
        trace_id="trace-generator-exit",
        group_id=None,
        metadata=None,
        processor=DummyProcessor(),
    )


def _new_reattached_trace() -> Trace:
    return ReattachedTrace(
        name="generator-exit",
        trace_id="trace-generator-exit",
        group_id=None,
        metadata=None,
        tracing_api_key=None,
    )


_TRACE_FACTORIES = [_new_no_op_trace, _new_trace_impl, _new_reattached_trace]


def _traced_stream(new_trace: Callable[[], Trace]) -> AsyncGenerator[int, None]:
    async def stream() -> AsyncGenerator[int, None]:
        with new_trace():
            yield 1
            yield 2

    return stream()


@pytest.mark.parametrize("new_trace", _TRACE_FACTORIES)
async def test_generator_close_in_the_same_task_releases_the_trace_scope(
    new_trace: Callable[[], Trace],
) -> None:
    """Closing a generator from the task that advanced it must restore the caller's trace.

    ``GeneratorExit`` unwinds the ``with`` block, but the token saved by ``start`` is still
    valid here because the body resumes in the caller's own context. Skipping the reset
    would leave the closed trace current and nest every later trace under it.
    """
    Scope.set_current_trace(None)

    generator = _traced_stream(new_trace)
    assert await generator.asend(None) == 1
    await generator.aclose()

    assert Scope.get_current_trace() is None


@pytest.mark.parametrize("new_trace", _TRACE_FACTORIES)
async def test_generator_close_from_another_task_does_not_raise(
    new_trace: Callable[[], Trace],
) -> None:
    """Abandoned async generators are finalized from whichever task runs ``aclose``.

    The body then resumes in a context that never set the token, so ``ContextVar.reset``
    raises ``ValueError``. That reset cannot succeed from there, and the caller keeps
    seeing the trace as current, so closing must at least not raise on top of it.
    Disabled tracing must behave the same as enabled tracing here.
    """
    Scope.set_current_trace(None)

    generator = _traced_stream(new_trace)
    assert await generator.asend(None) == 1
    await asyncio.create_task(generator.aclose())

    # The caller's own context still holds the trace, which is the documented residue of
    # finalizing from another task. Clear it so later tests do not inherit it.
    Scope.set_current_trace(None)


@pytest.mark.parametrize("new_trace", _TRACE_FACTORIES)
async def test_explicit_finish_from_another_context_still_raises(
    new_trace: Callable[[], Trace],
) -> None:
    """Only ``GeneratorExit`` cleanup tolerates a foreign token.

    An explicit ``finish`` from a context that never set the token is a context-ownership
    violation rather than an unavoidable one, so it must surface instead of silently
    discarding the saved token.
    """
    Scope.set_current_trace(None)

    trace = new_trace()
    trace.start(mark_as_current=True)

    async def finish_elsewhere() -> None:
        with pytest.raises(ValueError):
            trace.finish(reset_current=True)

    await asyncio.create_task(finish_elsewhere())

    Scope.set_current_trace(None)


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


async def test_generator_close_surfaces_processor_failure() -> None:
    """A processor failing during close must not be mistaken for a foreign token.

    ``finish`` calls ``on_trace_end`` before resetting the scope, so catching every
    ``ValueError`` around the whole call would swallow a processor failure, drop the saved
    token, and leave the finished trace current for everything that ran afterwards.
    """
    Scope.set_current_trace(None)

    class FailingProcessor(DummyProcessor):
        def on_trace_end(self, trace: Trace) -> None:
            raise ValueError("processor exploded")

    trace = TraceImpl(
        name="processor-failure",
        trace_id="trace-processor-failure",
        group_id=None,
        metadata=None,
        processor=cast(Any, FailingProcessor()),
    )

    async def stream() -> AsyncGenerator[int, None]:
        with trace:
            yield 1

    generator = stream()
    assert await generator.asend(None) == 1

    with pytest.raises(ValueError, match="processor exploded"):
        await generator.aclose()

    # The processor failure is the one that propagates, and the scope is still released:
    # running the reset in a finally keeps a failing finish from leaving the trace current.
    assert Scope.get_current_trace() is None
    assert trace._prev_context_token is None

    Scope.set_current_trace(None)

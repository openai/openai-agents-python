import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any, cast

import pytest

from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.scope import Scope
from agents.tracing.span_data import AgentSpanData, SpanData
from agents.tracing.spans import NoOpSpan, Span, SpanImpl
from agents.tracing.traces import Trace


class DummyProcessor(TracingProcessor):
    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended: list[str] = []

    def on_trace_start(self, trace: Trace) -> None:
        return None

    def on_trace_end(self, trace: Trace) -> None:
        return None

    def on_span_start(self, span: Span[Any]) -> None:
        self.started.append(span.span_id)

    def on_span_end(self, span: Span[Any]) -> None:
        self.ended.append(span.span_id)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None


def _new_no_op_span() -> Span[SpanData]:
    return NoOpSpan(AgentSpanData(name="generator-exit"))


def _new_span_impl() -> Span[SpanData]:
    return SpanImpl(
        trace_id="trace-generator-exit",
        span_id="span-generator-exit",
        parent_id=None,
        processor=DummyProcessor(),
        span_data=AgentSpanData(name="generator-exit"),
        tracing_api_key=None,
    )


_SPAN_FACTORIES = [_new_no_op_span, _new_span_impl]


def _spanned_stream(new_span: Callable[[], Span[SpanData]]) -> AsyncGenerator[int, None]:
    async def stream() -> AsyncGenerator[int, None]:
        with new_span():
            yield 1
            yield 2

    return stream()


@pytest.mark.parametrize("new_span", _SPAN_FACTORIES)
async def test_generator_close_in_the_same_task_releases_the_span_scope(
    new_span: Callable[[], Span[SpanData]],
) -> None:
    """Closing a generator from the task that advanced it must restore the caller's span.

    ``GeneratorExit`` unwinds the ``with`` block, but the token saved by ``start`` is still
    valid here because the body resumes in the caller's own context. Skipping the reset
    would leave the closed span current and nest every later span under it.
    """
    Scope.set_current_span(None)

    generator = _spanned_stream(new_span)
    assert await generator.asend(None) == 1
    await generator.aclose()

    assert Scope.get_current_span() is None


@pytest.mark.parametrize("new_span", _SPAN_FACTORIES)
async def test_generator_close_from_another_task_does_not_raise(
    new_span: Callable[[], Span[SpanData]],
) -> None:
    """Abandoned async generators are finalized from whichever task runs ``aclose``.

    The body then resumes in a context that never set the token, so ``ContextVar.reset``
    raises ``ValueError``. That reset cannot succeed from there, and the caller keeps
    seeing the span as current, so closing must at least not raise on top of it.
    Disabled tracing must behave the same as enabled tracing here.
    """
    Scope.set_current_span(None)

    generator = _spanned_stream(new_span)
    assert await generator.asend(None) == 1
    await asyncio.create_task(generator.aclose())

    # The caller's own context still holds the span, which is the documented residue of
    # finalizing from another task. Clear it so later tests do not inherit it.
    Scope.set_current_span(None)


@pytest.mark.parametrize("new_span", _SPAN_FACTORIES)
async def test_explicit_finish_from_another_context_still_raises(
    new_span: Callable[[], Span[SpanData]],
) -> None:
    """Only ``GeneratorExit`` cleanup tolerates a foreign token.

    An explicit ``finish`` from a context that never set the token is a context-ownership
    violation rather than an unavoidable one, so it must surface instead of silently
    discarding the saved token.
    """
    Scope.set_current_span(None)

    span = new_span()
    span.start(mark_as_current=True)

    async def finish_elsewhere() -> None:
        with pytest.raises(ValueError):
            span.finish(reset_current=True)

    await asyncio.create_task(finish_elsewhere())

    Scope.set_current_span(None)


async def test_generator_close_surfaces_processor_failure() -> None:
    """A processor failing during close must not be mistaken for a foreign token.

    ``finish`` calls ``on_span_end`` before resetting the scope, so catching every
    ``ValueError`` around the whole call would swallow a processor failure, drop the saved
    token, and leave the finished span current for everything that ran afterwards.
    """
    Scope.set_current_span(None)

    class FailingProcessor(DummyProcessor):
        def on_span_end(self, span: Span[Any]) -> None:
            raise ValueError("processor exploded")

    span = SpanImpl(
        trace_id="trace-processor-failure",
        span_id="span-processor-failure",
        parent_id=None,
        processor=cast(Any, FailingProcessor()),
        span_data=AgentSpanData(name="processor-failure"),
        tracing_api_key=None,
    )

    async def stream() -> AsyncGenerator[int, None]:
        with span:
            yield 1

    generator = stream()
    assert await generator.asend(None) == 1

    with pytest.raises(ValueError, match="processor exploded"):
        await generator.aclose()

    Scope.set_current_span(None)

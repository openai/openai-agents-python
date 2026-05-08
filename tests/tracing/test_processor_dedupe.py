"""Tests for `SynchronousMultiTracingProcessor` deduplication on re-registration.

Re-registering the same processor instance must not cause every span/trace event to
be delivered twice — that would double-export traces (and double-bill, on metered
backends) for callers that re-run setup (fork-safe re-init, hot-reload, composite
providers, test fixtures that call `add_trace_processor` more than once).
"""

from unittest.mock import MagicMock

from agents.tracing.provider import SynchronousMultiTracingProcessor


def test_add_tracing_processor_dedupes_same_instance() -> None:
    multi = SynchronousMultiTracingProcessor()
    proc = MagicMock()

    multi.add_tracing_processor(proc)
    multi.add_tracing_processor(proc)  # re-register same instance

    multi.on_trace_start(MagicMock())
    multi.on_span_end(MagicMock())

    assert proc.on_trace_start.call_count == 1
    assert proc.on_span_end.call_count == 1


def test_add_tracing_processor_keeps_distinct_instances() -> None:
    multi = SynchronousMultiTracingProcessor()
    proc_a = MagicMock()
    proc_b = MagicMock()

    multi.add_tracing_processor(proc_a)
    multi.add_tracing_processor(proc_b)
    multi.add_tracing_processor(proc_a)  # duplicate of A only

    multi.on_span_end(MagicMock())

    assert proc_a.on_span_end.call_count == 1
    assert proc_b.on_span_end.call_count == 1


def test_add_tracing_processor_dedupes_by_identity_not_equality() -> None:
    """Two distinct instances that compare equal still both register."""

    class EqProcessor:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, EqProcessor)

        def __hash__(self) -> int:
            return 0

        def on_trace_start(self, trace: object) -> None: ...
        def on_trace_end(self, trace: object) -> None: ...
        def on_span_start(self, span: object) -> None: ...
        def on_span_end(self, span: object) -> None: ...
        def shutdown(self) -> None: ...
        def force_flush(self) -> None: ...

    multi = SynchronousMultiTracingProcessor()
    proc_a = EqProcessor()
    proc_b = EqProcessor()
    assert proc_a == proc_b  # equal but distinct identities

    multi.add_tracing_processor(proc_a)
    multi.add_tracing_processor(proc_b)

    # Both should be present because dedupe is by identity (`is`), not equality.
    assert len(multi._processors) == 2

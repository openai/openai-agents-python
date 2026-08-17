from __future__ import annotations

from unittest.mock import MagicMock

from agents.tracing.scope import Scope
from agents.tracing.traces import TraceImpl


def test_trace_finish_notifies_processor_once_and_can_reset_later() -> None:
    processor = MagicMock()
    trace = TraceImpl(
        name="workflow",
        trace_id="trace_test",
        group_id=None,
        metadata=None,
        processor=processor,
    )

    trace.start(mark_as_current=True)
    trace.finish()
    trace.finish(reset_current=True)

    processor.on_trace_end.assert_called_once_with(trace)
    assert Scope.get_current_trace() is None

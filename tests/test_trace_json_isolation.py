from __future__ import annotations

from agents.tracing.provider import SynchronousMultiTracingProcessor
from agents.tracing.traces import TraceImpl


def test_trace_to_json_detaches_nested_metadata() -> None:
    trace = TraceImpl(
        name="workflow",
        trace_id="trace_test",
        group_id=None,
        metadata={"nested": {"value": "original"}},
        processor=SynchronousMultiTracingProcessor(),
    )

    payload = trace.to_json()
    assert payload is not None
    payload["metadata"]["nested"]["value"] = "changed"

    assert trace.metadata == {"nested": {"value": "original"}}

"""Native model spans retain their chosen identity in typed Responses exports."""

import pytest

from agents import telemetry
from tests.test_native_trace_exporter import collector, completed_event


@pytest.mark.parametrize("response_id", ["resp_synthetic", None, 42])
async def test_native_model_span_exports_v0_response_data(response_id):
    event = completed_event()
    event["name"] = "response"
    event["outcome"] = "ok"
    event["attributes"] = {"sdk.span.kind": "model"}
    if response_id is not None:
        event["attributes"]["sdk.response.id"] = response_id
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint)
        await exporter.on_trace_event(event)
    _trace, span = received[0]["payload"]["data"]
    assert span["id"] == "span_" + "0" * 8 + event["span_id"]
    assert span["parent_id"] is None
    assert span["span_data"] == {
        "type": "response",
        "response_id": response_id if isinstance(response_id, str) else None,
        "usage": None,
    }


async def test_generic_native_span_retains_custom_export():
    event = completed_event()
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint)
        await exporter.on_trace_event(event)
    _trace, span = received[0]["payload"]["data"]
    assert span["span_data"] == {
        "type": "custom",
        "name": event["name"],
        "data": event["attributes"],
    }

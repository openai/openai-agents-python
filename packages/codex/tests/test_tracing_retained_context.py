"""A retained native parent survives bounded exporter descriptor eviction."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents import telemetry
from tests.test_native_trace_exporter import collector


async def retained_context_case(large=False):
    observed = []
    root_name = "retained-workflow" + ("r" * 4000 if large else "")
    child_name = "late-child" + ("c" * 4000 if large else "")
    metadata = {
        "tenant": "original-tenant",
        **({f"pad_{i}": "m" * 4000 for i in range(14)} if large else {}),
    }

    class Observer:
        def on_trace_event(self, event):
            observed.append(event)

        def on_trace_events(self, events):
            observed.extend(events)

    with collector() as (endpoint, requests):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        await telemetry.configure(processors=[exporter, Observer()])
        try:
            with telemetry.trace(
                root_name,
                options={
                    "detached": True,
                    "group_id": "retained-group",
                    "metadata": metadata,
                    "include_sensitive_data": large,
                },
            ) as root:
                parent = root.context
            del root
            assert (await telemetry.force_flush(timeout_ms=15000)).status == "success"
            for index in range(1024):
                with telemetry.trace(f"churn-{index}", options={"detached": True}):
                    pass
                if index % 32 == 31:
                    result = await telemetry.force_flush(timeout_ms=15000)
                    assert result.status == "success", result
                    assert result.dropped_records == 0, result
            assert len(exporter._traces) <= 1024
            assert "trace_" + parent.trace_id not in exporter._traces
            with telemetry.span(child_name, options={"parent": parent}) as child:
                child.set_outcome(
                    status="ok",
                    data={
                        "private": "retained-context-private-sentinel"
                        + ("p" * 3900 if large else "")
                    },
                )
            result = await telemetry.force_flush(timeout_ms=15000)
            late = [event for event in observed if event["name"] == child_name]
            print(
                json.dumps(
                    {
                        "late_native_events": len(late),
                        "flush": repr(result),
                        "native_parent": parent.span_id,
                        "late_parents": [event["parent_span_id"] for event in late],
                    }
                ),
                flush=True,
            )
            assert len(late) == 2
            assert all(event["trace_id"] == parent.trace_id for event in late)
            assert all(event["parent_span_id"] == parent.span_id for event in late)
            assert result.dropped_records == 0
            if large:
                original = next(
                    event
                    for event in observed
                    if event["name"] == root_name and event["lifecycle"] == "start"
                )
                estimated = {
                    **late[-1],
                    "root_descriptor": {
                        "name": original["name"],
                        "attributes": original["attributes"],
                    },
                }
                print(
                    json.dumps(
                        {
                            "root_bytes": len(json.dumps(original, default=dict)),
                            "late_bytes": len(json.dumps(late[-1], default=dict)),
                            "inline_descriptor_bytes": len(json.dumps(estimated, default=dict)),
                        }
                    ),
                    flush=True,
                )
            assert result.status == "success", result
            records = [record for request in requests for record in request["payload"]["data"]]
            traces = [
                record
                for record in records
                if record["object"] == "trace" and record["id"] == "trace_" + parent.trace_id
            ]
            assert traces[-1]["workflow_name"] == root_name
            assert traces[-1]["group_id"] == "retained-group"
            assert traces[-1]["metadata"]["tenant"] == "original-tenant"
            assert ("retained-context-private-sentinel" in json.dumps(records)) is large
            assert len(traces) == 2
            exported = [
                record
                for record in records
                if record["object"] == "trace.span" and record["span_data"]["name"] == child_name
            ]
            assert len(exported) == 1
            assert exported[0]["trace_id"] == "trace_" + parent.trace_id
            assert exported[0]["parent_id"] == "span_" + parent.span_id.zfill(24)
            assert traces[-1]["metadata"] == {**metadata, "runtime": "native"}
            assert exporter._traces["trace_" + parent.trace_id].root_ended
            assert len(exporter._traces) <= 1024
            if not large:
                for _index in range(1600):
                    with telemetry.trace("loss-" + "x" * 4000, options={"detached": True}):
                        pass
                loss = await telemetry.force_flush(timeout_ms=15000)
                assert loss.dropped_records > 0
                with telemetry.span("retained-after-loss", options={"parent": parent}):
                    pass
                rejected = await telemetry.force_flush(timeout_ms=15000)
                assert exporter._recovery_sequence is not None
                assert exporter._recovery_sequence > 0
                stale = [event for event in observed if event["name"] == "retained-after-loss"]
                assert len(stale) == 2
                assert all(event.get("root_descriptor") for event in stale)
                assert rejected.failed_deliveries >= loss.failed_deliveries + 2
                all_records = [
                    record for request in requests for record in request["payload"]["data"]
                ]
                assert not any(
                    record.get("span_data", {}).get("name") == "retained-after-loss"
                    for record in all_records
                )
            print("PASS retained native context after descriptor eviction", flush=True)
        finally:
            await telemetry.shutdown(timeout_ms=15000)


@pytest.mark.parametrize("large", [False, True])
def test_retained_context_survives_descriptor_eviction(large):
    script = (
        f"import asyncio,runpy; m=runpy.run_path({str(Path(__file__).resolve())!r}); "
        f"asyncio.run(m['retained_context_case']({large!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr

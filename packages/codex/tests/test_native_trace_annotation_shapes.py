"""Small JSON payloads retain native aggregate budgets and nesting bounds."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agents import telemetry
from tests.test_native_trace_exporter import collector, native_trace_child_env

CASES = ["array", "object", "nested", "string", "encoded", "unicode", "top-level"]


def payload(case):
    if case == "array":
        return {"x": [0] * 65}
    if case == "object":
        return {"x": {str(i): i for i in range(65)}}
    if case == "nested":
        value = "leaf"
        for _ in range(16):
            value = {"x": value}
        return {"x": value}
    if case == "string":
        return {"x": "x" * 8192}
    if case == "encoded":
        return {"x": ["x" * 3000, "y" * 3000]}
    if case == "unicode":
        return {"x": "é" * 2100}
    if case == "top-level":
        return {f"field-{i}": i for i in range(200)}
    raise AssertionError(case)


@pytest.mark.parametrize("case", CASES)
def test_small_native_trace_payloads_round_trip(case):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; "
            "from tests.test_native_trace_annotation_shapes import native_case; "
            "asyncio.run(native_case(sys.argv[1]))",
            case,
        ],
        env=native_trace_child_env(),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(case):
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append(event)

    await telemetry.configure(processors=[Recorder()])
    try:
        value = payload(case)
        with telemetry.trace(
            "bounds.root",
            options={"metadata": value, "include_sensitive_data": True, "detached": True},
        ) as root:
            assert root.trace_metadata == value
            with telemetry.span(
                "bounds.child", options={"metadata": value, "include_sensitive_data": True}
            ) as child:
                child.set_outcome(status="ok", data=value)
        flushed = await telemetry.force_flush(timeout_ms=5000)
        assert flushed.status == "success"
        outcomes = [
            event
            for event in events
            if event["lifecycle"] == "end" and event["name"] == "bounds.child"
        ]
        assert len(outcomes) == 1
        assert json.loads(outcomes[0]["attributes"]["sdk.outcome.data"]) == value
        for name in ("bounds.root", "bounds.child"):
            snapshots = [
                event for event in events if event["lifecycle"] == "end" and event["name"] == name
            ]
            assert len(snapshots) == 1
            attributes = snapshots[0]["attributes"]
            delivered = {
                key.removeprefix("sdk.metadata."): item
                for key, item in attributes.items()
                if key.startswith("sdk.metadata.")
            }
            assert delivered.keys() == value.keys()
            for key, item in value.items():
                assert (
                    delivered[key] if isinstance(item, str) else json.loads(delivered[key])
                ) == item
        assert telemetry.current_context() is None
    finally:
        await telemetry.shutdown()


def test_native_trace_aggregate_and_nesting_bounds_preserve_cleanup():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; "
            "from tests.test_native_trace_annotation_shapes import native_boundaries; "
            "asyncio.run(native_boundaries())",
        ],
        env=native_trace_child_env(),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_boundaries():
    class Recorder:
        def on_trace_event(self, event):
            pass

    await telemetry.configure(processors=[Recorder()])
    try:
        with pytest.raises(RuntimeError, match="64 KiB"):
            with telemetry.trace("bounds.too-large", options={"metadata": {"x": "x" * 65536}}):
                pass
        with pytest.raises(RuntimeError, match="span end failed"):
            with telemetry.span(
                "bounds.completion", options={"include_sensitive_data": True}
            ) as child:
                child.set_outcome(status="ok", data={"x": "x" * 32768})
        value = "leaf"
        for _ in range(64):
            value = {"x": value}
        with pytest.raises(RuntimeError, match="maximum depth"):
            with telemetry.trace("bounds.too-deep", options={"metadata": {"x": value}}):
                pass
        with pytest.raises(RuntimeError, match="256"):
            with telemetry.trace("bounds.key", options={"metadata": {"x" * 257: 1}}):
                pass
        assert telemetry.current_context() is None
        with telemetry.trace(
            "bounds.following", options={"metadata": {"accepted": True}}
        ) as following:
            assert following.trace_metadata == {"accepted": True}
        assert telemetry.current_context() is None
    finally:
        await telemetry.shutdown()


@pytest.mark.parametrize("route", ["otlp", "generic", "wide", "wide-queue"])
def test_native_annotation_delivery_and_generic_limits(route):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; "
            "from tests.test_native_trace_annotation_shapes import native_delivery; "
            "asyncio.run(native_delivery(sys.argv[1]))",
            route,
        ],
        env=native_trace_child_env(),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_delivery(route):
    if route == "generic":
        events = []

        class Recorder:
            def on_trace_event(self, event):
                events.append(event)

        configured = {f"generic.field-{i}": "small" for i in range(200)}
        configured["generic.000-long"] = "x" * 8192
        await telemetry.configure(processors=[Recorder()], span_attributes=configured)
        try:
            with telemetry.trace("bounds.generic", options={"detached": True}):
                pass
            assert (await telemetry.force_flush(timeout_ms=5000)).status == "success"
            ended = [event for event in events if event["lifecycle"] == "end"]
            assert len(ended) == 1
            delivered = ended[0]["attributes"]
            assert delivered["generic.000-long"] == "x" * 4096
            assert len(delivered) <= 128
            assert ended[0]["dropped_attributes_count"] > 0
        finally:
            await telemetry.shutdown()
        return

    if route in {"wide", "wide-queue"}:
        events = []

        class Recorder:
            def on_trace_event(self, event):
                events.append(event)

        await telemetry.configure(
            processors=[Recorder()],
            trace_delivery={"max_queue_bytes": 60000} if route == "wide-queue" else None,
        )
        try:
            metadata = {f"f{i}": 0 for i in range(3000)}
            with telemetry.trace("bounds.wide.root", options={"metadata": metadata}) as root:
                parent = root.context
            with telemetry.use_context(parent):
                with telemetry.span("bounds.wide.late-child"):
                    pass
            flushed = await telemetry.force_flush(timeout_ms=5000)
            if route == "wide-queue":
                assert flushed.status == "partial"
                assert not events
            else:
                assert flushed.status == "success"
                ended = {event["name"]: event for event in events if event["lifecycle"] == "end"}
                assert ended.keys() == {"bounds.wide.root", "bounds.wide.late-child"}
                delivered = {
                    key.removeprefix("sdk.metadata."): json.loads(value)
                    for key, value in ended["bounds.wide.root"]["attributes"].items()
                    if key.startswith("sdk.metadata.")
                }
                assert delivered == metadata
        finally:
            await telemetry.shutdown()
        return

    with collector() as (endpoint, records):
        await telemetry.configure(traces=telemetry.OtlpHttp(endpoint=endpoint, protocol="json"))
        try:
            metadata = {"a-long": "x" * 8192, "b-encoded": ["x" * 3000, "y" * 3000]}
            metadata.update(payload("top-level"))
            with telemetry.trace("bounds.otlp.root", options={"metadata": metadata}):
                with telemetry.span("bounds.otlp.child", options={"metadata": metadata}):
                    pass
            assert (await telemetry.force_flush(timeout_ms=5000)).status == "success"
        finally:
            await telemetry.shutdown()
        spans = [
            span
            for record in records
            for resource in record["payload"]["resourceSpans"]
            for scope in resource["scopeSpans"]
            for span in scope["spans"]
            if span["name"].startswith("bounds.otlp.")
        ]
        assert {span["name"] for span in spans} == {"bounds.otlp.root", "bounds.otlp.child"}
        for span in spans:
            attributes = {item["key"]: item["value"] for item in span["attributes"]}
            assert "sdk.metadata" not in attributes
            delivered = {
                key.removeprefix("sdk.metadata."): item["stringValue"]
                for key, item in attributes.items()
                if key.startswith("sdk.metadata.")
            }
            assert delivered.keys() == metadata.keys()
            for key, value in metadata.items():
                assert (
                    delivered[key] if isinstance(value, str) else json.loads(delivered[key])
                ) == value

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
        with pytest.raises(RuntimeError, match="128 KiB"):
            with telemetry.trace("bounds.too-large", options={"metadata": {"x": "x" * 131072}}):
                pass
        with pytest.raises(RuntimeError, match="span end failed"):
            with telemetry.span(
                "bounds.completion", options={"include_sensitive_data": True}
            ) as child:
                child.set_outcome(status="ok", data={"x": "x" * 65536})
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


@pytest.mark.parametrize(
    "route",
    [
        "otlp",
        "generic",
        "wide",
        "wide-queue",
        "combined",
        "combined-escaped",
        "collision",
        "carrier-host",
        "carrier-otlp",
        "redispatch-wide",
        "redispatch-root",
        "redispatch-queue",
        "redispatch-unrelated",
        "redispatch-root-queue",
        "redispatch-root-unrelated",
        "redispatch-configured",
        "redispatch-receipt",
        "redispatch-wide-legacy",
        "redispatch-root-legacy",
    ],
)
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
    if route.startswith("redispatch-"):
        events = []

        class Recorder:
            def on_trace_event(self, event):
                events.append(event)

        await telemetry.configure(
            processors=[Recorder()],
            trace_delivery={"max_queue_bytes": 60000}
            if route in {"redispatch-queue", "redispatch-root-queue"}
            else None,
            span_attributes={f"sdk.metadata.config-{i}": "c" * 4000 for i in range(8)}
            if route == "redispatch-configured"
            else None,
        )
        try:
            metadata = {f"f{i}": 0 for i in range(9000)}
            literal = "literal"
            for _ in range(65):
                literal = {"x": literal}
            metadata["literal"] = json.dumps(literal, separators=(",", ":"))
            with telemetry.trace(
                "bounds.replay.root",
                options={
                    "metadata": {"x": "x" * 100000}
                    if route == "redispatch-configured"
                    else {"x": 0}
                    if route
                    in {
                        "redispatch-queue",
                        "redispatch-root-queue",
                        "redispatch-unrelated",
                        "redispatch-receipt",
                    }
                    else metadata
                },
            ) as root:
                parent = root.context
            if route.startswith("redispatch-root") or route == "redispatch-receipt":
                with telemetry.use_context(parent):
                    with telemetry.span("bounds.replay.child"):
                        pass
            assert (await telemetry.force_flush(timeout_ms=5000)).status == "success"
            recorded = [event for event in events if event["lifecycle"] == "end"][-1]
            replay = json.loads(json.dumps(recorded, default=dict))
            if route.startswith("redispatch-root"):
                assert not any(key.startswith("sdk.metadata.") for key in recorded["attributes"])
                assert any(
                    key.startswith("sdk.metadata.")
                    for key in replay["root_descriptor"]["attributes"]
                )
            if route == "redispatch-queue":
                replay["attributes"].update(
                    {
                        "sdk.metadata." + key: (value if isinstance(value, str) else str(value))
                        for key, value in metadata.items()
                    }
                )
            if route == "redispatch-root-queue":
                replay["root_descriptor"]["attributes"].update(
                    {
                        "sdk.metadata." + key: (value if isinstance(value, str) else str(value))
                        for key, value in metadata.items()
                    }
                )
            if route == "redispatch-root-unrelated":
                replay["root_descriptor"]["unrelated"] = "g" * 70000
            if route == "redispatch-unrelated":
                replay["attributes"].update(
                    {"sdk.metadata.": "invalid-key", "unrelated": "g" * 70000}
                )
            if route.endswith("-legacy"):
                replay.pop("__sdk_annotations", None)
                if "root_descriptor" in replay:
                    replay["root_descriptor"].pop("__sdk_annotations", None)
            if route == "redispatch-queue":
                replay["__sdk_annotations"] = json.dumps(dict(metadata, x=0), separators=(",", ":"))
            if route == "redispatch-root-queue":
                replay["root_descriptor"]["__sdk_annotations"] = json.dumps(
                    dict(metadata, x=0), separators=(",", ":")
                )
            count = len(events)
            if route == "redispatch-receipt":
                parent = json.loads(
                    json.dumps(
                        next(
                            event
                            for event in events
                            if event["lifecycle"] == "end" and event["name"].endswith(".root")
                        ),
                        default=dict,
                    )
                )
                parent["__sdk_annotations"] = '{"x":1}'
                replay["root_descriptor"]["__sdk_annotations"] = '{"x":1}'
                with pytest.raises(Exception, match="receipt"):
                    await telemetry.dispatch_trace_event(parent)
                with pytest.raises(Exception, match="receipt"):
                    await telemetry.dispatch_trace_event(replay)
                assert len(events) == count
            elif route in {
                "redispatch-queue",
                "redispatch-unrelated",
                "redispatch-root-queue",
                "redispatch-root-unrelated",
            }:
                with pytest.raises(Exception, match="queue capacity"):
                    await telemetry.dispatch_trace_event(replay)
                assert len(events) == count
            else:
                await telemetry.dispatch_trace_event(replay)
                assert len(events) == count + 1
                assert events[-1]["attributes"] == recorded["attributes"]
                assert events[-1].get("root_descriptor") == replay.get("root_descriptor")
            assert telemetry.current_context() is None
        finally:
            await telemetry.shutdown()
        return

    if route == "collision":
        events = []

        class Recorder:
            def on_trace_event(self, event):
                events.append(event)

        configured = {"sdk.metadata.x": "configured"}
        configured.update({f"generic.field-{i}": "g" * 4000 for i in range(20)})
        await telemetry.configure(processors=[Recorder()], span_attributes=configured)
        try:
            with telemetry.trace(
                "bounds.collision",
                options={"detached": True, "metadata": {"x": "x" * 40000}},
            ):
                pass
            flushed = await telemetry.force_flush(timeout_ms=5000)
            assert flushed.status == "partial"
            assert flushed.dropped_records == 2
            assert events == []
        finally:
            await telemetry.shutdown()
        return

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

    if route in {"combined", "combined-escaped"}:
        events = []

        class Recorder:
            def on_trace_event(self, event):
                events.append(event)

        await telemetry.configure(processors=[Recorder()])
        try:
            metadata = {"x": "x" * 100000}
            data = {"x": "y" * (65536 - 8) if route == "combined" else "\\" * ((65536 - 8) // 2)}
            with telemetry.trace(
                "bounds.combined",
                options={"metadata": metadata, "include_sensitive_data": True},
            ) as root:
                root.set_outcome(status="ok", data=data)
            assert (await telemetry.force_flush(timeout_ms=5000)).status == "success"
            ended = [event for event in events if event["lifecycle"] == "end"]
            assert len(ended) == 1
            assert ended[0]["attributes"]["sdk.metadata.x"] == metadata["x"]
            assert json.loads(ended[0]["attributes"]["sdk.outcome.data"]) == data
            replay = dict(ended[0], attributes=dict(ended[0]["attributes"]))
            await telemetry.dispatch_trace_event(replay)
            assert events[-1]["attributes"] == ended[0]["attributes"]
            oversized = dict(replay, attributes=dict(replay["attributes"]))
            oversized["attributes"]["sdk.outcome.data"] = json.dumps(
                {"x": "x" * 65536}, separators=(",", ":")
            )
            with pytest.raises(Exception, match="queue capacity"):
                await telemetry.dispatch_trace_event(oversized)
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
            metadata = {f"f{i}": 0 for i in range(9000)}
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
        events = []

        class Recorder:
            def on_trace_event(self, event):
                events.append(event)

        await telemetry.configure(
            traces=telemetry.OtlpHttp(endpoint=endpoint, protocol="json")
            if route != "carrier-host"
            else None,
            processors=[Recorder()] if route.startswith("carrier-") else [],
            span_attributes={"sdk.metadata": "service-label", "service.fixture": "retained"}
            if route.startswith("carrier-")
            else None,
        )
        try:
            metadata = {"a-long": "x" * 70000, "b-encoded": ["x" * 3000, "y" * 3000]}
            metadata.update(payload("top-level"))
            child_metadata = (
                dict(metadata, child="owned") if route.startswith("carrier-") else metadata
            )
            with telemetry.trace("bounds.otlp.root", options={"metadata": metadata}):
                with telemetry.span("bounds.otlp.child", options={"metadata": child_metadata}):
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
        if route.startswith("carrier-"):
            for event in events:
                expected = child_metadata if event["name"].endswith(".child") else metadata
                assert event["attributes"]["sdk.metadata"] == "service-label"
                assert event["attributes"]["service.fixture"] == "retained"
                actual = {
                    key.removeprefix("sdk.metadata."): value
                    for key, value in event["attributes"].items()
                    if key.startswith("sdk.metadata.")
                }
                assert actual.keys() == expected.keys()
                for key, value in expected.items():
                    assert (
                        actual[key] if isinstance(value, str) else json.loads(actual[key])
                    ) == value
        if route != "carrier-host":
            assert {span["name"] for span in spans} == {"bounds.otlp.root", "bounds.otlp.child"}
        for span in spans:
            attributes = {item["key"]: item["value"] for item in span["attributes"]}
            if route.startswith("carrier-"):
                assert attributes["sdk.metadata"]["stringValue"] == "service-label"
                assert attributes["service.fixture"]["stringValue"] == "retained"
            else:
                assert "sdk.metadata" not in attributes
            delivered = {
                key.removeprefix("sdk.metadata."): item["stringValue"]
                for key, item in attributes.items()
                if key.startswith("sdk.metadata.")
            }
            expected = child_metadata if span["name"].endswith(".child") else metadata
            assert delivered.keys() == expected.keys()
            for key, value in expected.items():
                assert (
                    delivered[key] if isinstance(value, str) else json.loads(delivered[key])
                ) == value

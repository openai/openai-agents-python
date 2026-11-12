"""Small JSON payloads retain native aggregate budgets and nesting bounds."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agents import telemetry

CASES = ["array", "object", "nested", "string", "encoded", "unicode"]


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
        env=os.environ.copy(),
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
            with telemetry.span("bounds.child", options={"include_sensitive_data": True}) as child:
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
        env=os.environ.copy(),
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

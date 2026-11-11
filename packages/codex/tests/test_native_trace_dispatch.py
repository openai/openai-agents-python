"""Direct processor observations retain data without importing live authority."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _agents_codex._trace_delivery import _trace_credential_route
from agents import telemetry


async def dispatch_case(mode):
    original = []

    class Original:
        def on_trace_event(self, event):
            original.append(event)

    await telemetry.configure(processors=[Original()])
    try:
        with telemetry.trace("recorded-observation"):
            pass
        assert (await telemetry.force_flush()).status == "success"
        snapshots = [event for event in original if event["name"] == "recorded-observation"]
        assert len(snapshots) == 2
        received = []
        rejected = []

        class Receiver:
            async def on_trace_event(self, event):
                received.append(event)
                if mode == "reentrant":
                    with pytest.raises(RuntimeError, match="trace processor callback"):
                        await telemetry.dispatch_trace_event(event)
                    rejected.append("reentrant")
                if mode == "credentials":
                    with pytest.raises(RuntimeError, match="credential"):
                        _trace_credential_route(event)
                    rejected.append("credential")

        telemetry.set_trace_processors([Receiver()])
        for event in snapshots:
            await telemetry.dispatch_trace_event(
                {**event, "credential_required": True} if mode == "credentials" else event
            )
        assert len(received) == 2
        for event, expected in zip(received, snapshots, strict=True):
            for field in (
                "lifecycle",
                "trace_id",
                "span_id",
                "parent_span_id",
                "name",
                "kind",
                "start_time_unix_nanos",
                "end_time_unix_nanos",
                "attributes",
                "outcome",
                "dropped_attributes_count",
            ):
                assert event[field] == expected[field], field
            assert event["sequence"] > expected["sequence"]
            assert event["processor_ids"] != expected["processor_ids"]
            with pytest.raises(TypeError):
                event["name"] = "mutated"
        if mode != "lifecycle":
            assert len(rejected) == 2
        assert telemetry.current_context() is None
        telemetry.set_tracing_disabled(True)
        await telemetry.dispatch_trace_event(snapshots[0])
        assert len(received) == 2
        telemetry.set_tracing_disabled(False)
        assert (await telemetry.force_flush()).status == "success"
    finally:
        telemetry.set_tracing_disabled(True)
        await telemetry.shutdown()
    rejected = {**original[0], "attributes": {"private": "DISPATCH_PRIVATE_SENTINEL"}}
    with pytest.raises(RuntimeError, match="shutting down"):
        await telemetry.dispatch_trace_event(rejected)
    telemetry.set_tracing_disabled(False)
    with pytest.raises(Exception, match="shutting down") as caught:
        await telemetry.dispatch_trace_event(rejected)
    frame = caught.value.__traceback__
    inspected = 0
    while frame is not None:
        if frame.tb_frame.f_code.co_name == "dispatch_trace_event":
            inspected += 1
            assert not {"event", "encoded", "current"}.intersection(frame.tb_frame.f_locals)
            assert "DISPATCH_PRIVATE_SENTINEL" not in repr(frame.tb_frame.f_locals)
        frame = frame.tb_next
    assert inspected == 1


@pytest.mark.parametrize("mode", ["lifecycle", "credentials", "reentrant"])
def test_native_dispatch_without_live_authority(mode):
    script = (
        f"import asyncio,runpy; m=runpy.run_path({str(Path(__file__).resolve())!r}); "
        f"asyncio.run(m['dispatch_case']({mode!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""Rejected completion data must still end the real native recording span."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents import telemetry


async def outcome_cleanup_case(primary_kind: str):
    events = []

    class Observer:
        def on_trace_event(self, event):
            events.append(event)

        def on_trace_events(self, batch):
            events.extend(batch)

    await telemetry.configure(processors=[Observer()])
    original = (
        asyncio.CancelledError("original-primary")
        if primary_kind == "cancelled"
        else RuntimeError("original-primary")
        if primary_kind == "error"
        else None
    )
    try:
        caught = None
        scope = telemetry.trace("oversized-outcome", options={"detached": True})
        try:
            with scope:
                parent = scope.context
                scope.set_outcome(
                    status="ok", data={"private": "outcome-private-sentinel" + "x" * 5000}
                )
                if original is not None:
                    raise original
        except BaseException as error:
            caught = error
        assert (
            caught is original
            if original is not None
            else (
                isinstance(caught, RuntimeError) and str(caught) == "Native trace span end failed"
            )
        )
        assert telemetry.current_context() is None
        result = await telemetry.force_flush(timeout_ms=15000)
        assert result.status == "success"
        ended = [
            event
            for event in events
            if event["span_id"] == parent.span_id and event["lifecycle"] == "end"
        ]
        assert len(ended) == 1, "rejected outcome left the retained native span recording"
        assert ended[0]["outcome"] == "error"
        assert ended[0]["attributes"]["sdk.outcome"] == (
            "cancelled" if primary_kind == "cancelled" else "error"
        )
        assert "outcome-private-sentinel" not in str(events)
        scope.__exit__(None, None, None)
        with telemetry.span("late-child", options={"parent": parent}):
            pass
        assert (await telemetry.force_flush(timeout_ms=15000)).status == "success"
        ended_again = [
            event
            for event in events
            if event["span_id"] == parent.span_id and event["lifecycle"] == "end"
        ]
        assert len(ended_again) == 1
        late = [event for event in events if event["name"] == "late-child"]
        assert len(late) == 2
        assert {event["lifecycle"] for event in late} == {"start", "end"}
        assert all(
            event["parent_span_id"] == parent.span_id and event.get("root_descriptor")
            for event in late
        )
        print("PASS native oversized-outcome cleanup", primary_kind, flush=True)
    finally:
        await telemetry.shutdown(timeout_ms=15000)


@pytest.mark.parametrize("primary_kind", ["none", "error", "cancelled"])
def test_native_rejected_outcome_ends_recording_once(primary_kind):
    script = (
        f"import asyncio,runpy; m=runpy.run_path({str(Path(__file__).resolve())!r}); "
        f"asyncio.run(m['outcome_cleanup_case']({primary_kind!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native oversized-outcome cleanup" in result.stdout

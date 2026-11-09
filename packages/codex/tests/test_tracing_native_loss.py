"""Native drops must not permanently strand bounded descriptor capacity."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from agents import telemetry
from tests.test_native_trace_exporter import collector


async def native_loss_case(long_names: bool):
    entered, release, phase = Event(), Event(), Event()
    observed = []
    groups = 0

    class Observer:
        def on_trace_event(self, event):
            pass

        async def on_trace_events(self, events):
            nonlocal groups
            observed.extend(events)
            if phase.is_set() and any(event["name"].startswith("burst-") for event in events):
                groups += 1
                if groups == 2:
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 0.75)

    with collector() as (endpoint, requests):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        await telemetry.configure(processors=[exporter, Observer()])
        roots = [telemetry.trace(f"active-{i}", options={"detached": True}) for i in range(1024)]
        for root in roots:
            root.__enter__()
        try:
            assert (await telemetry.force_flush(timeout_ms=15000)).status == "success"
            phase.set()
            for i in range(1600):
                with telemetry.trace(
                    "burst-" + (("x" * 4000) if long_names else str(i)), options={"detached": True}
                ):
                    pass
            for root in reversed(roots[1:]):
                root.__exit__(None, None, None)
            assert await asyncio.to_thread(entered.wait, 3)
            # The loss watermark is captured and one batch acknowledged, but
            # the old backlog remains. Queue new events before recovery.
            with telemetry.trace(
                "post-loss-fresh-root", options={"detached": True, "group_id": "fresh-group"}
            ):
                pass
            release.set()
            for _ in range(300):
                if any(
                    row.get("workflow_name") == "post-loss-fresh-root"
                    for request in requests
                    for row in request["payload"]["data"]
                ):
                    break
                await asyncio.sleep(0.01)
            # A genuinely live pre-loss root also has uncertain identity. Its
            # child/end must fail closed, rather than synthesize a new root.
            with telemetry.span("old-after-loss"):
                pass
            roots[0].__exit__(None, None, None)
            result = await telemetry.force_flush(timeout_ms=15000)
            assert result.dropped_records > 0 and result.status == "partial"
            records = [row for request in requests for row in request["payload"]["data"]]
            assert not any(
                row.get("span_data", {}).get("name") == "old-after-loss" for row in records
            )
            fresh = [row for row in records if row.get("workflow_name") == "post-loss-fresh-root"]
            assert len(fresh) == 1 and fresh[0]["group_id"] == "fresh-group"
            assert any(row.get("trace_id") == fresh[0]["id"] for row in records)
            old = [event["sequence"] for event in observed if event["name"].startswith("burst-")]
            new = [
                event["sequence"] for event in observed if event["name"] == "post-loss-fresh-root"
            ]
            assert min(new) > max(old) + 1  # Dropped-only tail still permits progress.
            assert len({tuple(event["processor_ids"]) for event in observed}) == 1
            assert len(observed[0]["processor_ids"]) == 2
            assert len(exporter._traces) <= 1024
            print(
                f"PASS native loss recovery long_names={long_names} "
                f"dropped={result.dropped_records}",
                flush=True,
            )
        finally:
            release.set()
            await telemetry.shutdown(timeout_ms=15000)


@pytest.mark.parametrize("long_names", [False, True])
def test_native_loss_recovers_after_record_or_byte_bound(long_names):
    script = (
        f"import asyncio,runpy; m=runpy.run_path({str(Path(__file__).resolve())!r}); "
        f"asyncio.run(m['native_loss_case']({long_names!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native loss recovery" in result.stdout

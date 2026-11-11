"""Native drops must not permanently strand bounded descriptor capacity."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from agents import _native, telemetry
from tests.test_native_trace_exporter import collector, native_trace_child_env


async def native_loss_case(long_names: bool, forwarded=False, retained_after_recovery=False):
    entered, release, phase = Event(), Event(), Event()
    observed = []
    mixed_batches = []
    groups = 0

    class Observer:
        def on_trace_event(self, event):
            pass

        async def on_trace_events(self, events):
            nonlocal groups
            observed.extend(events)
            names = {event["name"] for event in events}
            if "post-loss-fresh-root" in names and "old-after-loss" in names:
                mixed_batches.append(names)
            if phase.is_set() and any(event["name"].startswith("burst-") for event in events):
                groups += 1
                if groups == 2:
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 0.75)

    with collector() as (endpoint, requests):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)

        class Forwarder:
            def on_trace_event(self, event):
                return exporter.on_trace_event(event)

            def on_trace_events(self, events):
                return exporter.on_trace_events(events)

        await telemetry.configure(processors=[Forwarder() if forwarded else exporter, Observer()])
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
            # Queue stale and fresh traces synchronously while native delivery
            # is gated, so one recovery batch contains both admission outcomes.
            with telemetry.span("old-after-loss"):
                pass
            roots[0].__exit__(None, None, None)
            with telemetry.trace(
                "post-loss-fresh-root", options={"detached": True, "group_id": "fresh-group"}
            ):
                pass
            release.set()
            result = await telemetry.force_flush(timeout_ms=15000)
            assert result.dropped_records > 0 and result.status == "partial"
            assert mixed_batches, "native recovery never exercised mixed stale/fresh admission"
            records = [row for request in requests for row in request["payload"]["data"]]
            assert not any(
                row.get("span_data", {}).get("name") == "old-after-loss" for row in records
            )
            fresh = [row for row in records if row.get("workflow_name") == "post-loss-fresh-root"]
            assert len(fresh) == 1 and fresh[0]["group_id"] == "fresh-group"
            assert any(row.get("trace_id") == fresh[0]["id"] for row in records)
            if forwarded:
                recovered_roots = [
                    telemetry.trace(f"recovered-active-{i}", options={"detached": True})
                    for i in range(1024)
                ]
                for recovered in recovered_roots:
                    recovered.__enter__()
                active = await telemetry.force_flush(timeout_ms=15000)
                assert active.failed_deliveries == result.failed_deliveries, (
                    "forwarding kept lost roots pinned"
                )
                for recovered in reversed(recovered_roots):
                    recovered.__exit__(None, None, None)
                completed = await telemetry.force_flush(timeout_ms=15000)
                assert completed.failed_deliveries == result.failed_deliveries
                assert completed.dropped_records == result.dropped_records
            if retained_after_recovery:
                failures = result.failed_deliveries
                with telemetry.trace(
                    "post-recovery-retained",
                    options={
                        "detached": True,
                        "group_id": "recovered-group",
                        "metadata": {"tenant": "recovered-tenant"},
                    },
                ) as recovered:
                    parent = recovered.context
                await telemetry.force_flush(timeout_ms=15000)
                for i in range(1024):
                    with telemetry.trace(f"recovered-churn-{i}", options={"detached": True}):
                        pass
                    if i % 32 == 31:
                        await telemetry.force_flush(timeout_ms=15000)
                with telemetry.span(
                    "post-recovery-late-child", options={"parent": parent}
                ) as child:
                    child.set_outcome(status="ok", data={"private": "recovered-private-sentinel"})
                late_result = await telemetry.force_flush(timeout_ms=15000)
                assert late_result.failed_deliveries == failures, (
                    "post-recovery retained child rejected"
                )
                records = [row for request in requests for row in request["payload"]["data"]]
                descriptors = [
                    row
                    for row in records
                    if row.get("object") == "trace" and row.get("id") == "trace_" + parent.trace_id
                ]
                assert len(descriptors) == 2
                assert descriptors[-1]["workflow_name"] == "post-recovery-retained"
                assert descriptors[-1]["group_id"] == "recovered-group"
                assert descriptors[-1]["metadata"]["tenant"] == "recovered-tenant"
                late = [
                    row
                    for row in records
                    if row.get("span_data", {}).get("name") == "post-recovery-late-child"
                ]
                assert len(late) == 1 and late[0]["parent_id"] == "span_" + parent.span_id.rjust(
                    24, "0"
                )
                assert "recovered-private-sentinel" not in str(records)
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


async def native_loss_overlap_case():
    cutoffs = []
    observed = []
    second_loss = asyncio.Event()
    staged = False
    stale_name = "intervening-stale-child"
    healthy_name = "healthy-after-recovery"

    class Observer:
        def on_trace_event(self, event):
            if event["name"] in (stale_name, healthy_name):
                observed.append(event)

    def fill(count):
        for _ in range(count):
            with telemetry.trace("bounded-overflow", options={"detached": True}):
                pass

    native_routes = _native.set_trace_routes
    native_ack = _native.ack_trace_events

    def set_routes(raw):
        cutoff = native_routes(raw)
        if json.loads(raw) == {"processor_ids": [], "replace": False}:
            cutoffs.append(cutoff)
            if len(cutoffs) == 2:
                second_loss.set()
        return cutoff

    def acknowledge(through, failed):
        nonlocal staged
        native_ack(through, failed)
        if len(cutoffs) == 1 and not staged:
            staged = True
            # At most two batches of 64 can have freed slots: a drain that read
            # during the initial fill and the first drain that reported loss.
            # The stale span and 64 traces emit 130 records, forcing another loss.
            with telemetry.span(stale_name, options={"parent": parent}):
                pass
            fill(64)

    with collector() as (endpoint, requests), pytest.MonkeyPatch.context() as patch:
        patch.setattr(_native, "set_trace_routes", set_routes)
        patch.setattr(_native, "ack_trace_events", acknowledge)
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic", endpoint=endpoint)
        await telemetry.configure(processors=[exporter, Observer()])
        try:
            with telemetry.trace("completed-stale-parent", options={"detached": True}) as root:
                parent = root.context
            assert (await telemetry.force_flush()).status == "success"

            fill(1600)
            await asyncio.wait_for(second_loss.wait(), 10)
            loss = await telemetry.force_flush(timeout_ms=15000)
            with telemetry.trace(healthy_name, options={"detached": True}):
                pass
            later = await telemetry.force_flush(timeout_ms=15000)

            first, second = cutoffs
            stale = [event for event in observed if event["name"] == stale_name]
            healthy = [event for event in observed if event["name"] == healthy_name]
            assert len(stale) == len(healthy) == 2
            assert all(
                event["root_descriptor"]["start_sequence"] < first < event["sequence"] <= second
                for event in stale
            )
            assert all(second < event["sequence"] for event in healthy)
            assert loss.status == later.status == "partial"
            assert loss.dropped_records == later.dropped_records > 0

            records = [row for request in requests for row in request["payload"]["data"]]
            names = [row["span_data"]["name"] for row in records if row["object"] == "trace.span"]
            assert stale_name not in names
            assert names.count(healthy_name) == 1
            assert 0 < loss.failed_deliveries == later.failed_deliveries
            assert {event["recovery_sequence"] for event in stale} == {first}
            assert {event["recovery_sequence"] for event in healthy} == {second}
        finally:
            await telemetry.shutdown(timeout_ms=15000)


@pytest.mark.serial
def test_native_loss_preserves_overlapping_recovery_cutoffs():
    script = (
        f"import asyncio,runpy; m=runpy.run_path({str(Path(__file__).resolve())!r}); "
        "asyncio.run(m['native_loss_overlap_case']())"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.serial
@pytest.mark.parametrize("long_names", [False, True])
def test_native_loss_recovers_after_record_or_byte_bound(long_names):
    script = (
        f"import asyncio,runpy; m=runpy.run_path({str(Path(__file__).resolve())!r}); "
        f"asyncio.run(m['native_loss_case']({long_names!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native loss recovery" in result.stdout


@pytest.mark.parametrize("forwarded", [False, True])
def test_native_loss_preserves_forwarding_and_recovered_retained_context(forwarded):
    script = (
        f"import asyncio,runpy; m=runpy.run_path({str(Path(__file__).resolve())!r}); "
        f"asyncio.run(m['native_loss_case'](False, forwarded={forwarded!r}, "
        f"retained_after_recovery={not forwarded!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native loss recovery" in result.stdout

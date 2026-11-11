"""Native trace scheduling, overflow accounting, and callback budgets."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any

import pytest

from _agents_codex import telemetry as runtime_telemetry
from agents import telemetry
from tests.test_native_trace_exporter import native_trace_child_env


async def test_trace_delivery_configuration_captures_limits_before_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import _native

    captured: list[dict[str, Any]] = []

    async def configure(encoded: str) -> None:
        captured.append(json.loads(encoded))

    monkeypatch.setattr(_native, "configure_telemetry", configure)
    for name in ("_engine_options", "_processor_batch_size", "_processor_callback_timeout"):
        monkeypatch.setattr(runtime_telemetry, name, getattr(runtime_telemetry, name))
    await telemetry.configure(
        trace_delivery=telemetry.TraceDeliveryConfig(
            max_queue_size=2,
            max_queue_bytes=8192,
            max_batch_size=8,
            schedule_delay_ms=100,
            export_trigger_ratio=0.5,
            callback_timeout_ms=None,
        )
    )
    assert captured[0]["sdk.trace_delivery"] == {
        "max_queue_size": 2,
        "max_queue_bytes": 8192,
        "max_batch_size": 8,
        "schedule_delay_ms": 100,
        "export_trigger_ratio": 0.5,
    }
    assert runtime_telemetry._processor_batch_size == 8
    assert runtime_telemetry._processor_callback_timeout is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_queue_size", 0),
        ("max_queue_size", True),
        ("max_queue_size", 2**32),
        ("max_queue_bytes", -1),
        ("max_batch_size", 1.5),
        ("schedule_delay_ms", -1),
        ("export_trigger_ratio", float("nan")),
        ("export_trigger_ratio", float("inf")),
        ("export_trigger_ratio", -0.1),
        ("export_trigger_ratio", 1.1),
        ("export_trigger_ratio", True),
        ("callback_timeout_ms", 0),
        ("callback_timeout_ms", 60001),
        ("callback_timeout_ms", True),
    ],
)
async def test_invalid_trace_delivery_does_not_reach_native(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    from agents import _native

    async def unexpected(encoded: str) -> None:
        pytest.fail("Invalid trace delivery settings reached native initialization")

    monkeypatch.setattr(_native, "configure_telemetry", unexpected)
    with pytest.raises(ValueError, match=field):
        await telemetry.configure(trace_delivery=telemetry.TraceDeliveryConfig(**{field: value}))


@pytest.mark.parametrize(
    "case",
    [
        "flush_trigger",
        "timer",
        "overflow_count",
        "overflow_bytes",
        "long",
        "unbounded",
        "short",
        "invalid",
    ],
)
def test_native_trace_delivery(case: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from tests.test_trace_delivery_config import native_case; "
            "asyncio.run(native_case(sys.argv[1]))",
            case,
        ],
        env=native_trace_child_env(),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {case}" in result.stdout


async def native_case(case: str) -> None:
    batches: list[tuple[int, ...]] = []

    class Recorder:
        def on_trace_event(self, event: telemetry.TraceLifecycleEvent) -> None:
            raise AssertionError("The batch callback must take precedence")

        def on_trace_events(self, events: tuple[telemetry.TraceLifecycleEvent, ...]) -> None:
            batches.append(tuple(event["sequence"] for event in events))

    async def wait_for_count(count: int) -> None:
        async def wait() -> None:
            while sum(map(len, batches)) < count:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait(), timeout=3)

    if case == "invalid":
        from agents import _native

        for options in ({"max_queue_size": 0}, {"unknown": 1}, {"export_trigger_ratio": 2}):
            with pytest.raises(RuntimeError, match="Invalid trace delivery options"):
                await _native.configure_telemetry(json.dumps({"sdk.trace_delivery": options}))
        await telemetry.configure()
        await telemetry.shutdown()
        print(f"PASS {case}")
        return

    if case in ("long", "unbounded", "short"):
        seen: list[int] = []

        class PerEvent:
            async def on_trace_event(self, event: telemetry.TraceLifecycleEvent) -> None:
                seen.append(event["sequence"])
                if len(seen) == 1:
                    await asyncio.sleep(0.08 if case == "short" else 1.05)

        timeout = {"long": 1500, "unbounded": None, "short": 25}[case]
        await telemetry.configure(
            processors=[PerEvent()],
            trace_delivery=telemetry.TraceDeliveryConfig(callback_timeout_ms=timeout),
        )
        try:
            with telemetry.trace("callback-budget"):
                pass
            result = await telemetry.force_flush(timeout_ms=3000)
            if case == "short":
                assert result.failed_deliveries > 0
                await asyncio.sleep(0.15)
                assert len(seen) == 1
            else:
                assert result.status == "success"
                assert len(seen) == 2
        finally:
            await telemetry.shutdown(timeout_ms=3000)
        print(f"PASS {case}")
        return

    options = telemetry.TraceDeliveryConfig(
        max_queue_size=2 if case == "overflow_count" else 16,
        max_queue_bytes=1 if case == "overflow_bytes" else 4 * 1024 * 1024,
        max_batch_size=3,
        schedule_delay_ms=40 if case == "timer" else 60000,
        export_trigger_ratio=1,
    )
    await telemetry.configure(processors=[Recorder()], trace_delivery=options)
    try:
        with telemetry.trace("first"):
            pass
        if case.startswith("overflow"):
            with telemetry.trace("second"):
                pass
            result = await telemetry.force_flush(timeout_ms=3000)
            assert result.status == "partial"
            assert result.dropped_records >= (4 if case == "overflow_bytes" else 2)
            assert sum(map(len, batches)) == (0 if case == "overflow_bytes" else 2)
        elif case == "timer":
            await wait_for_count(2)
            assert (await telemetry.force_flush(timeout_ms=3000)).status == "success"
        else:
            await asyncio.sleep(0.06)
            assert not batches
            assert (await telemetry.force_flush(timeout_ms=3000)).status == "success"
            assert sum(map(len, batches)) == 2
            with telemetry.trace("after-completed-flush"):
                pass
            await asyncio.sleep(0.06)
            assert sum(map(len, batches)) == 2
            assert (await telemetry.force_flush(timeout_ms=3000)).status == "success"
            assert sum(map(len, batches)) == 4
            for index in range(8):
                with telemetry.trace(f"trigger-{index}"):
                    pass
            await wait_for_count(20)
            assert (await telemetry.force_flush(timeout_ms=3000)).status == "success"
            flattened = [sequence for batch in batches for sequence in batch]
            assert flattened == sorted(set(flattened))
        assert all(len(batch) <= 3 for batch in batches)
    finally:
        await telemetry.shutdown(timeout_ms=3000)
    print(f"PASS {case}")

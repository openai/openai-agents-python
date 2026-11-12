"""Built-in HTTP delivery stops when the host retires its callback."""

from __future__ import annotations

import asyncio
import json
import sys
from threading import Event
from types import SimpleNamespace

import httpx2
import pytest

from _agents_codex import telemetry as runtime
from agents import telemetry
from tests._asyncio import timeout
from tests.test_native_trace_exporter import completed_event


@pytest.mark.parametrize(
    "retirement,phase",
    [
        ("shutdown", "backoff"),
        ("shutdown", "request"),
        ("callback_timeout", "backoff"),
        ("callback_timeout", "request"),
        ("flush_timeout", "backoff"),
    ],
)
async def test_exporter_stops_fallback_http_when_delivery_retires(
    monkeypatch: pytest.MonkeyPatch, retirement: str, phase: str
) -> None:
    entered, closed = Event(), Event()
    requests: list[object] = []
    acknowledged: list[tuple[int, int]] = []
    identifiers: list[str] = []
    sent = False

    async def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        entered.set()
        if phase == "request":
            await asyncio.sleep(1)
        return httpx2.Response(503 if len(requests) == 1 else 200, request=request)

    original = httpx2.AsyncClient

    class Client(original):
        async def __aexit__(self, *args):
            try:
                return await super().__aexit__(*args)
            finally:
                closed.set()

    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **options: Client(**options, transport=httpx2.MockTransport(respond)),
    )

    async def configure(encoded: str) -> None:
        pass

    def routes(encoded: str) -> int:
        identifiers.extend(json.loads(encoded)["processor_ids"])
        return 0

    async def drain(count: int) -> str:
        nonlocal sent
        events = (
            [] if sent else [{**completed_event(), "sequence": 1, "processor_ids": identifiers}]
        )
        sent = True
        return json.dumps(
            {"events": events, "through_sequence": int(bool(events)), "dropped_records": 0}
        )

    async def shutdown(timeout: int | None = None) -> None:
        pass

    async def flush(timeout: int) -> str:
        return json.dumps(
            {"status": "timeout", "dropped_records": 0, "failed_deliveries": 0, "barrier": 1}
        )

    monkeypatch.setitem(
        sys.modules,
        "agents._native",
        SimpleNamespace(
            configure_telemetry=configure,
            set_trace_routes=routes,
            drain_trace_events=drain,
            ack_trace_events=lambda *args: acknowledged.append(args),
            shutdown_telemetry=shutdown,
            force_flush_telemetry=flush,
        ),
    )
    for name in ("_processors", "_retired_routes"):
        monkeypatch.setattr(runtime, name, {})
    monkeypatch.setattr(runtime, "_active_processor_ids", set())
    for name, value in (
        ("_engine_options", None),
        ("_processor_task", None),
        ("_processor_closing", False),
        ("_processor_starting", False),
        ("_processor_callback_timeout", runtime._processor_callback_timeout),
        ("_processor_batch_size", runtime._processor_batch_size),
    ):
        monkeypatch.setattr(runtime, name, value)

    async def wait_until(predicate) -> None:
        async with timeout(2):
            while not predicate():
                await asyncio.sleep(0.005)

    exporter = telemetry.OpenAITraceExporter(
        api_key="synthetic-fallback-key",
        timeout_ms=1000,
        max_retries=1,
        retry_delay_ms=200,
    )
    await telemetry.configure(
        processors=[exporter],
        trace_delivery=telemetry.TraceDeliveryConfig(
            callback_timeout_ms=50 if retirement == "callback_timeout" else None,
        ),
    )
    try:
        await wait_until(entered.is_set)
        if retirement == "shutdown":
            await telemetry.shutdown(timeout_ms=1)
        elif retirement == "flush_timeout":
            assert (await telemetry.force_flush(timeout_ms=1)).status == "timeout"
            assert not closed.is_set()
            await wait_until(lambda: bool(acknowledged))
            assert len(requests) == 2
            assert acknowledged == [(1, 0)]
            return
        else:
            await wait_until(lambda: bool(acknowledged))
        await asyncio.sleep(0.1)
        assert closed.is_set(), "Retired built-in HTTP work has still been running"
        await asyncio.sleep(0.15)
        assert len(requests) == 1
        assert acknowledged == [(1, 1)]
    finally:
        await telemetry.shutdown(timeout_ms=1)
        await wait_until(closed.is_set)

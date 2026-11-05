"""Credential ownership and processor callback boundaries (REQ429/438/439)."""

from __future__ import annotations

import asyncio
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from agents import telemetry
from tests.test_tracing_scopes import NativeTracing


def _assert_no_exporter_credentials(error: BaseException) -> None:
    def reveal(value: Any) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            return {field.name: reveal(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, dict):
            return {key: reveal(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [reveal(item) for item in value]
        return repr(value)

    seen: set[int] = set()
    current_error: BaseException | None = error
    while current_error is not None and id(current_error) not in seen:
        seen.add(id(current_error))
        current = current_error.__traceback__
        while current is not None:
            frame = current.tb_frame
            if frame.f_code.co_filename == telemetry.__file__:
                # Exporter repr deliberately hides these fields. Inspect the
                # retained objects too, as a traceback-local collector can.
                assert "traceback-secret" not in repr(reveal(frame.f_locals))
            current = current.tb_next
        current_error = current_error.__cause__ or current_error.__context__


def _private_exporter() -> telemetry.OtlpHttp:
    return telemetry.OtlpHttp(
        endpoint="https://traceback-secret-user:traceback-secret-password@test.invalid/v1/logs",
        headers={"authorization": "traceback-secret-header"},
        tls=telemetry.OtelTlsConfig(client_private_key=Path("/traceback-secret/key.pem")),
    )


@pytest.mark.parametrize("failure", ["endpoint", "tls"])
def test_exporter_conversion_drops_hidden_credential_arguments(failure: str) -> None:
    exporter = _private_exporter()
    if failure == "endpoint":
        exporter = telemetry.OtlpHttp(endpoint="", headers=exporter.headers)
    else:
        exporter = telemetry.OtlpHttp(
            endpoint=exporter.endpoint,
            headers=exporter.headers,
            tls=telemetry.OtelTlsConfig(client_private_key=cast(Any, 7)),
        )
    with pytest.raises((TypeError, ValueError)) as raised:
        telemetry._exporter(exporter, "traces")
    _assert_no_exporter_credentials(raised.value)
    assert exporter.headers == {"authorization": "traceback-secret-header"}


@pytest.mark.parametrize("failure", ["conversion", "native", "cancelled", "processors", "import"])
async def test_configure_drops_hidden_exporters_from_error_frames(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from agents import _native

    started = asyncio.Event()
    release = asyncio.Event()

    async def initialize(encoded: str) -> None:
        started.set()
        if failure == "cancelled":
            await release.wait()
        else:
            raise RuntimeError("native initialization failed")

    monkeypatch.setattr(_native, "configure_telemetry", initialize)
    monkeypatch.setattr(telemetry, "_engine_options", None)
    if failure == "import":
        original_import = telemetry.importlib.import_module

        def unavailable(name: str, package: str | None = None) -> Any:
            if name == "agents._native":
                raise ImportError("native addon unavailable")
            return original_import(name, package)

        monkeypatch.setattr(telemetry.importlib, "import_module", unavailable)
    exporter = _private_exporter()
    options: dict[str, Any] = {"traces": exporter, "logs": exporter, "metrics": exporter}
    if failure == "conversion":
        options["metrics"] = telemetry.OtlpGrpc(endpoint="")
    if failure == "processors":
        options["processors"] = [object()]
    if failure == "cancelled":
        task = asyncio.create_task(telemetry.configure(**options))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        _assert_no_exporter_credentials(cancelled.value)
    else:
        with pytest.raises((TypeError, ValueError, RuntimeError, ImportError)) as raised:
            await telemetry.configure(**options)
        _assert_no_exporter_credentials(raised.value)
    assert exporter.headers == {"authorization": "traceback-secret-header"}


async def test_processor_callback_rejects_flush_without_blocking_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = NativeTracing()
    monkeypatch.setattr(telemetry, "_native_tracing", lambda: native)
    monkeypatch.setattr(telemetry, "_engine_options", "{}")
    monkeypatch.setattr(telemetry, "_processors", {})
    delivered: list[int] = []

    class Processor:
        async def on_trace_event(self, event: telemetry.TraceLifecycleEvent) -> None:
            with pytest.raises(RuntimeError, match="inside a trace processor callback"):
                await telemetry.force_flush()
            delivered.append(event["sequence"])

    route = telemetry._ProcessorRoute(Processor())
    for sequence in (1, 2):
        await route._invoke(cast(telemetry.TraceLifecycleEvent, {"sequence": sequence}))
    assert delivered == [1, 2]
    assert native.flushes == []
    result = await telemetry.force_flush(timeout_ms=100)
    assert result.status == "partial"
    assert native.flushes == [100]

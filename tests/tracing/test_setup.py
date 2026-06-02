from __future__ import annotations

import atexit
from typing import Any, cast

import pytest

from agents.tracing import (
    processors as tracing_processors,
    provider as tracing_provider,
    setup as tracing_setup,
)


class _DummyProvider:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _DefaultProviderWithTimeout(tracing_provider.DefaultTraceProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_timeout: float | None = None

    def shutdown(self, timeout: float | None = None) -> None:
        self.shutdown_timeout = timeout


class _BootstrapProvider:
    def __init__(self) -> None:
        self.processors: list[Any] = []
        self.shutdown_calls = 0

    def register_processor(self, processor: Any) -> None:
        self.processors.append(processor)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_shutdown_global_trace_provider_calls_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _DummyProvider()
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", provider)

    tracing_setup._shutdown_global_trace_provider()

    assert provider.shutdown_calls == 1


def test_shutdown_global_trace_provider_passes_timeout_to_default_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _DefaultProviderWithTimeout()
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", provider)

    tracing_setup._shutdown_global_trace_provider()

    assert provider.shutdown_timeout == tracing_setup._DEFAULT_SHUTDOWN_TIMEOUT


def test_set_trace_provider_registers_shutdown_once(monkeypatch: pytest.MonkeyPatch) -> None:
    registrations: list[Any] = []

    def fake_register(callback: Any) -> Any:
        registrations.append(callback)
        return callback

    first = _DummyProvider()
    second = _DummyProvider()

    monkeypatch.setattr(atexit, "register", fake_register)
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", None)
    monkeypatch.setattr(tracing_setup, "_SHUTDOWN_HANDLER_REGISTERED", False)

    tracing_setup.set_trace_provider(cast(Any, first))
    tracing_setup.set_trace_provider(cast(Any, second))

    assert cast(Any, tracing_setup.GLOBAL_TRACE_PROVIDER) is second
    assert registrations == [tracing_setup._shutdown_global_trace_provider]


def test_get_trace_provider_returns_existing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _DummyProvider()

    def fail_register(_: Any) -> None:
        raise AssertionError("atexit.register should not be called for an existing provider.")

    monkeypatch.setattr(atexit, "register", fail_register)
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", provider)

    assert cast(Any, tracing_setup.get_trace_provider()) is provider


def test_get_trace_provider_bootstraps_provider_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registrations: list[Any] = []
    default_processor = object()

    def fake_register(callback: Any) -> Any:
        registrations.append(callback)
        return callback

    monkeypatch.setattr(atexit, "register", fake_register)
    monkeypatch.setattr(tracing_setup, "GLOBAL_TRACE_PROVIDER", None)
    monkeypatch.setattr(tracing_setup, "_SHUTDOWN_HANDLER_REGISTERED", False)
    monkeypatch.setattr(tracing_processors, "default_processor", lambda: default_processor)
    monkeypatch.setattr(tracing_provider, "DefaultTraceProvider", _BootstrapProvider)

    provider = tracing_setup.get_trace_provider()

    assert isinstance(provider, _BootstrapProvider)
    assert provider.processors == [default_processor]
    assert tracing_setup.GLOBAL_TRACE_PROVIDER is provider
    assert registrations == [tracing_setup._shutdown_global_trace_provider]

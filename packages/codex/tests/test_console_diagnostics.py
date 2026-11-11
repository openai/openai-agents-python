"""Public console configuration and process-only diagnostic settings."""

from __future__ import annotations

import json

import pytest

from _agents_codex import telemetry as runtime_telemetry
from agents import _native, telemetry


@pytest.mark.asyncio
async def test_console_settings_reach_native_without_becoming_session_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    async def capture(encoded: str) -> None:
        captured.append(json.loads(encoded))

    monkeypatch.setattr(_native, "configure_telemetry", capture)
    monkeypatch.setattr(runtime_telemetry, "_engine_options", None)
    await telemetry.configure(
        diagnostics=telemetry.ConsoleDiagnostics(
            level="debug",
            targets=("codex_core", "codex_otel::events"),
            include_fields=True,
        )
    )

    assert captured[0]["sdk.console_diagnostics"] == {
        "level": "debug",
        "targets": ["codex_core", "codex_otel::events"],
        "include_fields": True,
    }
    emission = runtime_telemetry._engine_options
    assert emission is not None
    assert "sdk.console_diagnostics" not in json.loads(emission)
    assert telemetry.ConsoleDiagnostics.__module__ == "agents.telemetry"

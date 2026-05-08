from __future__ import annotations

import logging

from agents.tracing import trace
from agents.tracing.scope import Scope
from agents.tracing.setup import GLOBAL_TRACE_PROVIDER, set_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from agents.tracing.traces import NoOpTrace


def test_nested_trace_under_noop_does_not_warn(caplog, monkeypatch) -> None:
    """When tracing is disabled and the outer trace is a NoOpTrace, creating an
    inner trace must not emit the misleading 'Trace already exists' warning."""
    Scope.set_current_trace(None)
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    set_trace_provider(DefaultTraceProvider())
    try:
        with trace("outer") as outer:
            assert isinstance(outer, NoOpTrace)
            with caplog.at_level(logging.WARNING, logger="openai.agents"):
                with trace("inner") as inner:
                    assert isinstance(inner, NoOpTrace)
        warnings = [r for r in caplog.records if "Trace already exists" in r.getMessage()]
        assert warnings == [], (
            "Nested trace warning should not fire when outer trace is a NoOpTrace, "
            f"but got: {[r.getMessage() for r in warnings]}"
        )
    finally:
        Scope.set_current_trace(None)
        # Restore previous global provider so we don't leak state between tests.
        if GLOBAL_TRACE_PROVIDER is not None:
            set_trace_provider(GLOBAL_TRACE_PROVIDER)

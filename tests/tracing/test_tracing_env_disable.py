import logging

from agents.tracing.provider import DefaultTraceProvider
from agents.tracing.scope import Scope
from agents.tracing.span_data import AgentSpanData
from agents.tracing.spans import NoOpSpan, SpanImpl
from agents.tracing.traces import NoOpTrace, TraceImpl


def test_env_read_on_first_use(monkeypatch):
    """Env flag set before first trace disables tracing."""
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    provider = DefaultTraceProvider()

    trace = provider.create_trace("demo")

    assert isinstance(trace, NoOpTrace)


def test_env_cached_after_first_use(monkeypatch):
    """Env flag is cached after the first trace and later env changes do not flip it."""
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "0")
    provider = DefaultTraceProvider()

    first = provider.create_trace("first")
    assert isinstance(first, TraceImpl)

    # Change env after first use; cached value should keep tracing enabled.
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    second = provider.create_trace("second")

    assert isinstance(second, TraceImpl)


def test_manual_override_after_cache(monkeypatch):
    """Manual toggle still works after env value is cached."""
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "0")
    provider = DefaultTraceProvider()

    provider.create_trace("warmup")
    provider.set_disabled(True)
    disabled = provider.create_trace("disabled")
    assert isinstance(disabled, NoOpTrace)

    provider.set_disabled(False)
    enabled = provider.create_trace("enabled")
    assert isinstance(enabled, TraceImpl)


def test_manual_override_env_disable(monkeypatch):
    """Manual enable can override env disable flag."""
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    provider = DefaultTraceProvider()

    env_disabled = provider.create_trace("env_disabled")
    assert isinstance(env_disabled, NoOpTrace)

    provider.set_disabled(False)
    reenabled = provider.create_trace("reenabled")

    assert isinstance(reenabled, TraceImpl)


def test_missing_active_trace_logs_debug_for_noop_span(caplog):
    Scope.set_current_trace(None)
    Scope.set_current_span(None)
    provider = DefaultTraceProvider()

    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        span = provider.create_span(AgentSpanData(name="missing-trace"))

    assert isinstance(span, NoOpSpan)
    assert "No active trace" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_noop_span_id_returns_noop_span_with_active_trace():
    Scope.set_current_trace(None)
    Scope.set_current_span(None)
    provider = DefaultTraceProvider()
    trace = provider.create_trace("active", trace_id="trace_123")
    trace_token = Scope.set_current_trace(trace)
    try:
        span = provider.create_span(AgentSpanData(name="invalid"), span_id="no-op")
    finally:
        Scope.reset_current_trace(trace_token)

    assert isinstance(span, NoOpSpan)


def test_noop_current_span_id_does_not_become_parent_id():
    Scope.set_current_trace(None)
    Scope.set_current_span(None)
    provider = DefaultTraceProvider()
    trace = provider.create_trace("active", trace_id="trace_123")
    invalid_parent = SpanImpl(
        trace_id="trace_123",
        span_id="no-op",
        parent_id=None,
        processor=provider._multi_processor,
        span_data=AgentSpanData(name="invalid-parent"),
        tracing_api_key=None,
    )
    trace_token = Scope.set_current_trace(trace)
    span_token = Scope.set_current_span(invalid_parent)
    try:
        span = provider.create_span(AgentSpanData(name="child"))
    finally:
        Scope.reset_current_span(span_token)
        Scope.reset_current_trace(trace_token)

    assert isinstance(span, NoOpSpan)

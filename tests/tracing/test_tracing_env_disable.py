import logging

import pytest

import agents._debug as _debug
from agents.tracing import get_current_trace, trace
from agents.tracing.provider import DefaultTraceProvider
from agents.tracing.scope import Scope
from agents.tracing.span_data import AgentSpanData
from agents.tracing.spans import NoOpSpan, SpanImpl
from agents.tracing.traces import NoOpTrace, TraceImpl


@pytest.mark.parametrize("policy", [(True, True), (True, False), (False, True), (False, False)])
@pytest.mark.parametrize("disabled_by", [None, "call", "provider"])
def test_trace_lifecycle_logging_omits_caller_data(monkeypatch, caplog, policy, disabled_by):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", policy[0])
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", policy[1])
    provider = DefaultTraceProvider()
    provider.set_disabled(disabled_by == "provider")
    monkeypatch.setattr("agents.tracing.create.get_trace_provider", lambda: provider)
    workflow_name = "PRIVATE_WORKFLOW_CANARY"
    trace_id = "trace_0123456789abcdef0123456789abcdef"
    metadata = {"private": "PRIVATE_METADATA_CANARY"}
    previous = get_current_trace()

    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        with trace(
            workflow_name,
            trace_id=trace_id,
            metadata=metadata,
            disabled=disabled_by == "call",
        ) as created:
            assert get_current_trace() is created
            if disabled_by is None:
                assert created.name == workflow_name
                assert created.trace_id == trace_id
                exported = created.export()
                assert exported is not None
                assert exported["workflow_name"] == workflow_name
                assert exported["id"] == trace_id
                assert exported["metadata"] == metadata
            else:
                assert isinstance(created, NoOpTrace)
                assert created.export() is None
        assert get_current_trace() is previous

    records = [record for record in caplog.records if record.name == "openai.agents"]
    assert records
    for record in records:
        assert record.args == ()
        assert record.exc_info is None
        for value in (workflow_name, trace_id, metadata["private"]):
            assert value not in repr(vars(record))
            assert value not in record.getMessage()


def test_env_read_on_first_use(monkeypatch):
    """Env flag set before first trace disables tracing."""
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    provider = DefaultTraceProvider()

    trace = provider.create_trace("demo")

    assert isinstance(trace, NoOpTrace)


@pytest.mark.parametrize("redacted", [True, False])
def test_disabled_span_logging_respects_data_policy(monkeypatch, caplog, redacted: bool):
    class SensitiveAgentSpanData(AgentSpanData):
        def __repr__(self) -> str:
            return "SECRET_SPAN_NAME"

    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    provider = DefaultTraceProvider()

    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        span = provider.create_span(SensitiveAgentSpanData(name="agent"))

    assert isinstance(span, NoOpSpan)
    assert ("SECRET_SPAN_NAME" not in caplog.text) is redacted
    assert "Tracing is disabled. Not creating span" in caplog.text


def test_force_flush_initializes_env_disable_cache(monkeypatch):
    """Force flush preserves the first-use timing for the env disable flag."""
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    provider = DefaultTraceProvider()

    provider.force_flush()

    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "0")
    trace = provider.create_trace("still-disabled")

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


def test_falsy_current_span_becomes_parent() -> None:
    class FalsySpan(SpanImpl[AgentSpanData]):
        def __bool__(self) -> bool:
            return False

    Scope.set_current_trace(None)
    Scope.set_current_span(None)
    provider = DefaultTraceProvider()
    trace = provider.create_trace("active", trace_id="trace_123")
    parent = FalsySpan(
        trace_id="trace_123",
        span_id="span_parent",
        parent_id=None,
        processor=provider._multi_processor,
        span_data=AgentSpanData(name="parent"),
        tracing_api_key=None,
    )
    trace_token = Scope.set_current_trace(trace)
    span_token = Scope.set_current_span(parent)
    try:
        child = provider.create_span(AgentSpanData(name="child"))
    finally:
        Scope.reset_current_span(span_token)
        Scope.reset_current_trace(trace_token)

    assert isinstance(child, SpanImpl)
    assert child.parent_id == "span_parent"

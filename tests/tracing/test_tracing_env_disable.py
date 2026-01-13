from agents.tracing.provider import DefaultTraceProvider
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

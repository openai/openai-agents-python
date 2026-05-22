"""Verify that public tracing names are re-exported from the top-level agents package."""

import agents
from agents import tracing as tracing_module


def test_tracing_config_is_exported_at_top_level() -> None:
    # TracingConfig is referenced in docs as the value to pass via RunConfig.tracing,
    # so it must be importable from the top-level package without reaching into agents.tracing.
    from agents import TracingConfig

    assert TracingConfig is tracing_module.TracingConfig
    assert "TracingConfig" in agents.__all__


def test_trace_provider_is_exported_at_top_level() -> None:
    # TraceProvider is the abstract base for custom trace providers and is documented as
    # part of the tracing extension surface; it should be importable from agents.
    from agents import TraceProvider

    assert TraceProvider is tracing_module.TraceProvider
    assert "TraceProvider" in agents.__all__


def test_get_trace_provider_is_exported_at_top_level() -> None:
    # set_trace_provider is already exported from the top-level package, so its getter
    # counterpart should be too. Callers swapping providers commonly need both.
    from agents import get_trace_provider

    assert get_trace_provider is tracing_module.get_trace_provider
    assert "get_trace_provider" in agents.__all__


def test_trace_ctx_manager_is_exported_at_top_level() -> None:
    # TraceCtxManager is used by custom runners and voice pipelines to manage a trace
    # lifecycle, and is part of agents.tracing.__all__.
    from agents import TraceCtxManager

    assert TraceCtxManager is tracing_module.TraceCtxManager
    assert "TraceCtxManager" in agents.__all__


def test_all_public_tracing_names_are_reexported() -> None:
    # Every name exposed by agents.tracing.__all__ should also be available from the
    # top-level agents package, so users have a single import path for tracing types.
    for name in tracing_module.__all__:
        assert hasattr(agents, name), f"agents.{name} is not re-exported from agents package"
        assert name in agents.__all__, f"{name} is missing from agents.__all__"

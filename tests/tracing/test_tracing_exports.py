"""Verify that the global trace provider accessor and type are re-exported from agents."""

import agents
from agents import tracing as tracing_module


def test_get_trace_provider_is_exported_at_top_level() -> None:
    # get_trace_provider is the read-side counterpart to the already-exported
    # set_trace_provider. A user who sets a custom provider should be able to read it
    # back from the same top-level import surface.
    from agents import get_trace_provider

    assert get_trace_provider is tracing_module.get_trace_provider
    assert "get_trace_provider" in agents.__all__


def test_trace_provider_type_is_exported_at_top_level() -> None:
    # TraceProvider is the return type of get_trace_provider and the parameter type of
    # set_trace_provider, so it must be importable from agents for type annotations.
    from agents import TraceProvider

    assert TraceProvider is tracing_module.TraceProvider
    assert "TraceProvider" in agents.__all__

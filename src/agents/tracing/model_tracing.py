from __future__ import annotations

from ..models.interface import ModelTracing


def get_model_tracing_impl(
    tracing_disabled: bool, trace_include_sensitive_data: bool
) -> ModelTracing:
    """Return the ModelTracing setting based on run-level tracing configuration."""
    if tracing_disabled:
        return ModelTracing.DISABLED
    if trace_include_sensitive_data:
        return ModelTracing.ENABLED
    return ModelTracing.ENABLED_WITHOUT_DATA

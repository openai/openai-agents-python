
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .prometheus import PrometheusMetrics
    from .hooks import MetricsHooks

__all__ = [
    "PrometheusMetrics",
    "MetricsHooks",
    "enable_metrics",
    "get_metrics",
    "disable_metrics",
]


def __getattr__(name: str):
    if name == "PrometheusMetrics":
        from .prometheus import PrometheusMetrics as _PrometheusMetrics

        return _PrometheusMetrics
    elif name == "MetricsHooks":
        from .hooks import MetricsHooks as _MetricsHooks

        return _MetricsHooks
    elif name == "enable_metrics":
        from .hooks import enable_metrics as _enable_metrics

        return _enable_metrics
    elif name == "get_metrics":
        from .hooks import get_metrics as _get_metrics

        return _get_metrics
    elif name == "disable_metrics":
        from .hooks import disable_metrics as _disable_metrics

        return _disable_metrics
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

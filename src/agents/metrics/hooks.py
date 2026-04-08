
from __future__ import annotations

import time
from typing import Any

from ..agent import Agent
from ..lifecycle import RunHooks
from ..logger import logger
from ..result import RunResult
from ..run_context import RunContextWrapper

try:
    from .prometheus import PrometheusMetrics
except ImportError:
    PrometheusMetrics = None

_global_metrics: PrometheusMetrics | None = None


def enable_metrics(metrics: PrometheusMetrics) -> None:
    global _global_metrics
    _global_metrics = metrics


def get_metrics() -> PrometheusMetrics | None:
    return _global_metrics


def disable_metrics() -> None:
    global _global_metrics
    _global_metrics = None


class MetricsHooks(RunHooks):

    def __init__(self, metrics: PrometheusMetrics | None = None) -> None:
        self._metrics = metrics or _global_metrics
        self._run_start_times: dict[str, float] = {}
        self._tool_start_times: dict[str, float] = {}

    async def on_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
    ) -> None:
        if self._metrics is None:
            return

        agent_name = agent.name or "unknown"
        self._run_start_times[context.context_id] = time.monotonic()
        self._metrics.record_run_start(agent_name)

    async def on_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        result: RunResult,
    ) -> None:
        if self._metrics is None:
            return

        agent_name = agent.name or "unknown"
        start_time = self._run_start_times.pop(context.context_id, None)
        duration = None
        if start_time is not None:
            duration = time.monotonic() - start_time

        self._metrics.record_run_end(agent_name, duration, status="success")

    async def on_error(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        error: Exception,
    ) -> None:
        if self._metrics is None:
            return

        agent_name = agent.name or "unknown"
        start_time = self._run_start_times.pop(context.context_id, None)
        duration = None
        if start_time is not None:
            duration = time.monotonic() - start_time

        error_type = type(error).__name__
        self._metrics.record_error(error_type, agent_name)
        self._metrics.record_run_end(agent_name, duration, status="error")

    async def on_tool_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool_name: str,
        input_data: dict[str, Any],
    ) -> None:
        if self._metrics is None:
            return

        key = f"{context.context_id}:{tool_name}"
        self._tool_start_times[key] = time.monotonic()

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool_name: str,
        result: Any,
    ) -> None:
        if self._metrics is None:
            return

        key = f"{context.context_id}:{tool_name}"
        start_time = self._tool_start_times.pop(key, None)
        if start_time is not None:
            latency = time.monotonic() - start_time
            agent_name = agent.name or "unknown"
            self._metrics.record_tool_execution(tool_name, latency, agent_name)

    async def on_tool_error(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool_name: str,
        error: Exception,
    ) -> None:
        if self._metrics is None:
            return

        key = f"{context.context_id}:{tool_name}"
        start_time = self._tool_start_times.pop(key, None)
        if start_time is not None:
            latency = time.monotonic() - start_time
            agent_name = agent.name or "unknown"
            self._metrics.record_tool_execution(tool_name, latency, agent_name)

        error_type = f"tool_error:{type(error).__name__}"
        agent_name = agent.name or "unknown"
        self._metrics.record_error(error_type, agent_name)


def create_metrics_hooks(metrics: PrometheusMetrics | None = None) -> MetricsHooks:
    return MetricsHooks(metrics)

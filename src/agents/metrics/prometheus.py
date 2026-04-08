
from __future__ import annotations

import time
from typing import Any

from ..logger import logger

try:
    from prometheus_client import Counter, Histogram, CollectorRegistry, REGISTRY

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    class Counter:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...
        def inc(self, *args: Any, **kwargs: Any) -> None: ...
        def labels(self, *args: Any, **kwargs: Any) -> "Counter": ...

    class Histogram:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...
        def observe(self, *args: Any, **kwargs: Any) -> None: ...
        def labels(self, *args: Any, **kwargs: Any) -> "Histogram": ...

    class CollectorRegistry:
        pass

    REGISTRY = None


class PrometheusMetrics:

    def __init__(
        self,
        registry: "CollectorRegistry | None" = None,
        namespace: str = "agents",
    ) -> None:
        if not PROMETHEUS_AVAILABLE:
            raise ImportError(
                "prometheus-client is not installed. "
                "Install it with: pip install 'prometheus-client>=0.21.0'"
            )

        self._registry = registry or REGISTRY
        self._namespace = namespace
        self._run_start_times: dict[str, float] = {}

        self._llm_latency = Histogram(
            f"{namespace}_llm_latency_seconds",
            "Latency of LLM API calls in seconds",
            ["model"],
            buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
            registry=self._registry,
        )

        self._tokens = Counter(
            f"{namespace}_tokens_total",
            "Total tokens used",
            ["direction", "model"],
            registry=self._registry,
        )

        self._errors = Counter(
            f"{namespace}_errors_total",
            "Total number of errors",
            ["error_type", "agent_name"],
            registry=self._registry,
        )

        self._runs = Counter(
            f"{namespace}_runs_total",
            "Total number of agent runs",
            ["agent_name", "status"],
            registry=self._registry,
        )

        self._run_duration = Histogram(
            f"{namespace}_run_duration_seconds",
            "Duration of agent runs in seconds",
            ["agent_name"],
            buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
            registry=self._registry,
        )

        self._turns = Counter(
            f"{namespace}_turns_total",
            "Total number of LLM turns",
            ["agent_name"],
            registry=self._registry,
        )

        self._tool_executions = Counter(
            f"{namespace}_tool_executions_total",
            "Total number of tool executions",
            ["tool_name", "agent_name"],
            registry=self._registry,
        )

        self._tool_latency = Histogram(
            f"{namespace}_tool_latency_seconds",
            "Latency of tool executions in seconds",
            ["tool_name"],
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
            registry=self._registry,
        )

    def record_llm_call(
        self,
        latency: float,
        tokens_in: int,
        tokens_out: int,
        model: str = "unknown",
    ) -> None:
        if not PROMETHEUS_AVAILABLE:
            return

        try:
            self._llm_latency.labels(model=model).observe(latency)
            self._tokens.labels(direction="input", model=model).inc(tokens_in)
            self._tokens.labels(direction="output", model=model).inc(tokens_out)
        except Exception as e:
            logger.debug(f"Failed to record LLM metrics: {e}")

    def record_error(
        self,
        error_type: str,
        agent_name: str = "unknown",
    ) -> None:
        if not PROMETHEUS_AVAILABLE:
            return

        try:
            self._errors.labels(error_type=error_type, agent_name=agent_name).inc()
        except Exception as e:
            logger.debug(f"Failed to record error metrics: {e}")

    def record_run_start(self, agent_name: str) -> None:
        if not PROMETHEUS_AVAILABLE:
            return

        self._run_start_times[agent_name] = time.monotonic()
        try:
            self._runs.labels(agent_name=agent_name, status="started").inc()
        except Exception as e:
            logger.debug(f"Failed to record run start metrics: {e}")

    def record_run_end(
        self,
        agent_name: str,
        duration: float | None = None,
        status: str = "success",
    ) -> None:
        if not PROMETHEUS_AVAILABLE:
            return

        try:
            self._runs.labels(agent_name=agent_name, status=status).inc()

            if duration is None:
                start_time = self._run_start_times.pop(agent_name, None)
                if start_time is not None:
                    duration = time.monotonic() - start_time

            if duration is not None:
                self._run_duration.labels(agent_name=agent_name).observe(duration)
        except Exception as e:
            logger.debug(f"Failed to record run end metrics: {e}")

    def record_turn(self, agent_name: str) -> None:
        if not PROMETHEUS_AVAILABLE:
            return

        try:
            self._turns.labels(agent_name=agent_name).inc()
        except Exception as e:
            logger.debug(f"Failed to record turn metrics: {e}")

    def record_tool_execution(
        self,
        tool_name: str,
        latency: float,
        agent_name: str = "unknown",
    ) -> None:
        if not PROMETHEUS_AVAILABLE:
            return

        try:
            self._tool_executions.labels(tool_name=tool_name, agent_name=agent_name).inc()
            self._tool_latency.labels(tool_name=tool_name).observe(latency)
        except Exception as e:
            logger.debug(f"Failed to record tool metrics: {e}")

    def get_registry(self) -> "CollectorRegistry":
        return self._registry

    @property
    def is_available(self) -> bool:
        return PROMETHEUS_AVAILABLE

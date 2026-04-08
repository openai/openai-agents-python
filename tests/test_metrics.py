"""Tests for the metrics module."""

from __future__ import annotations

import pytest
import time

from agents.metrics.prometheus import PrometheusMetrics, PROMETHEUS_AVAILABLE
from agents.metrics.hooks import MetricsHooks, enable_metrics, disable_metrics, get_metrics

pytestmark = pytest.mark.skipif(
    not PROMETHEUS_AVAILABLE,
    reason="prometheus-client not installed",
)


@pytest.fixture
def fresh_registry():
    """Create a fresh registry for each test."""
    from prometheus_client import CollectorRegistry

    return CollectorRegistry()


@pytest.fixture
def metrics(fresh_registry):
    """Create a metrics instance with fresh registry."""
    return PrometheusMetrics(registry=fresh_registry, namespace="test_agents")


class TestPrometheusMetrics:
    """Tests for PrometheusMetrics class."""

    def test_init(self, fresh_registry):
        """Test metrics initialization."""
        m = PrometheusMetrics(registry=fresh_registry)
        assert m.is_available is True
        assert m.get_registry() is fresh_registry

    def test_init_without_prometheus(self, monkeypatch):
        """Test initialization when prometheus is not available."""
        monkeypatch.setattr(
            "agents.metrics.prometheus.PROMETHEUS_AVAILABLE",
            False,
        )
        with pytest.raises(ImportError, match="prometheus-client is not installed"):
            PrometheusMetrics()

    def test_record_llm_call(self, metrics, fresh_registry):
        """Test recording LLM call metrics."""
        from prometheus_client import REGISTRY

        # Record a call
        metrics.record_llm_call(
            latency=1.5,
            tokens_in=100,
            tokens_out=50,
            model="gpt-4",
        )

        # Get metrics from registry
        latency_metric = fresh_registry._names_to_collectors.get("test_agents_llm_latency_seconds")
        assert latency_metric is not None

        tokens_metric = fresh_registry._names_to_collectors.get("test_agents_tokens_total")
        assert tokens_metric is not None

    def test_record_error(self, metrics, fresh_registry):
        """Test recording error metrics."""
        metrics.record_error("RateLimitError", "test_agent")
        metrics.record_error("TimeoutError", "test_agent")
        metrics.record_error("RateLimitError", "other_agent")

        error_metric = fresh_registry._names_to_collectors.get("test_agents_errors_total")
        assert error_metric is not None

    def test_record_run(self, metrics, fresh_registry):
        """Test recording run metrics."""
        metrics.record_run_start("agent_1")
        time.sleep(0.01)  # Small delay
        metrics.record_run_end("agent_1", status="success")

        runs_metric = fresh_registry._names_to_collectors.get("test_agents_runs_total")
        assert runs_metric is not None

        duration_metric = fresh_registry._names_to_collectors.get(
            "test_agents_run_duration_seconds"
        )
        assert duration_metric is not None

    def test_record_run_with_auto_duration(self, metrics):
        """Test recording run with auto-calculated duration."""
        metrics.record_run_start("auto_agent")
        time.sleep(0.05)
        metrics.record_run_end("auto_agent", status="success")

        # Should complete without error and calculate duration

    def test_record_turn(self, metrics, fresh_registry):
        """Test recording turn metrics."""
        metrics.record_turn("test_agent")
        metrics.record_turn("test_agent")
        metrics.record_turn("other_agent")

        turns_metric = fresh_registry._names_to_collectors.get("test_agents_turns_total")
        assert turns_metric is not None

    def test_record_tool_execution(self, metrics, fresh_registry):
        """Test recording tool execution metrics."""
        metrics.record_tool_execution("calculator", 0.5, "math_agent")
        metrics.record_tool_execution("search", 1.2, "research_agent")

        exec_metric = fresh_registry._names_to_collectors.get("test_agents_tool_executions_total")
        assert exec_metric is not None

        latency_metric = fresh_registry._names_to_collectors.get("test_agents_tool_latency_seconds")
        assert latency_metric is not None

    def test_record_with_exception_handling(self, metrics):
        """Test that recording failures don't crash the application."""
        # This should not raise even if prometheus has issues
        metrics.record_llm_call(
            latency=float("inf"),  # Edge case
            tokens_in=-1,  # Invalid
            tokens_out=0,
            model="test",
        )


class TestMetricsHooks:
    """Tests for MetricsHooks class."""

    @pytest.fixture
    def mock_context(self):
        """Create a mock RunContextWrapper."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.context_id = "test-context-123"
        return ctx

    @pytest.fixture
    def mock_agent(self):
        """Create a mock Agent."""
        from unittest.mock import MagicMock

        agent = MagicMock()
        agent.name = "test_agent"
        return agent

    @pytest.fixture
    def mock_result(self):
        """Create a mock RunResult."""
        from unittest.mock import MagicMock

        result = MagicMock()
        result.usage = None
        return result

    @pytest.mark.asyncio
    async def test_on_start(self, metrics, mock_context, mock_agent):
        """Test on_start hook."""
        hooks = MetricsHooks(metrics)
        await hooks.on_start(mock_context, mock_agent)

        # Should record run start
        assert hooks._run_start_times.get(mock_context.context_id) is not None

    @pytest.mark.asyncio
    async def test_on_end(self, metrics, mock_context, mock_agent, mock_result):
        """Test on_end hook."""
        hooks = MetricsHooks(metrics)

        # First start the run
        await hooks.on_start(mock_context, mock_agent)

        # Then end it
        await hooks.on_end(mock_context, mock_agent, mock_result)

        # Should clean up start time
        assert hooks._run_start_times.get(mock_context.context_id) is None

    @pytest.mark.asyncio
    async def test_on_error(self, metrics, mock_context, mock_agent):
        """Test on_error hook."""
        hooks = MetricsHooks(metrics)

        # Start a run
        await hooks.on_start(mock_context, mock_agent)

        # Simulate error
        error = ValueError("Test error")
        await hooks.on_error(mock_context, mock_agent, error)

        # Should clean up and record error
        assert hooks._run_start_times.get(mock_context.context_id) is None

    @pytest.mark.asyncio
    async def test_on_tool_execution(self, metrics, mock_context, mock_agent):
        """Test tool execution hooks."""
        hooks = MetricsHooks(metrics)

        # Start tool execution
        await hooks.on_tool_start(mock_context, mock_agent, "test_tool", {"arg": "value"})

        # Small delay
        time.sleep(0.01)

        # End tool execution
        await hooks.on_tool_end(mock_context, mock_agent, "test_tool", "result")

        # Should record the execution

    @pytest.mark.asyncio
    async def test_on_tool_error(self, metrics, mock_context, mock_agent):
        """Test tool error hook."""
        hooks = MetricsHooks(metrics)

        # Start tool execution
        await hooks.on_tool_start(mock_context, mock_agent, "failing_tool", {})

        time.sleep(0.01)

        # Simulate tool error
        error = RuntimeError("Tool failed")
        await hooks.on_tool_error(mock_context, mock_agent, "failing_tool", error)

        # Should record error and execution


class TestGlobalFunctions:
    """Tests for global metrics functions."""

    def test_enable_get_disable_metrics(self, metrics):
        """Test global metrics lifecycle."""
        # Initially None
        disable_metrics()
        assert get_metrics() is None

        # Enable
        enable_metrics(metrics)
        assert get_metrics() is metrics

        # Disable
        disable_metrics()
        assert get_metrics() is None

    def test_create_metrics_hooks_with_global(self, metrics):
        """Test creating hooks with global metrics."""
        from agents.metrics.hooks import create_metrics_hooks

        enable_metrics(metrics)
        hooks = create_metrics_hooks()

        assert hooks._metrics is metrics

        disable_metrics()

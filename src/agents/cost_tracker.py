from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agents.tracing.spans import Span
    from agents.tracing.traces import Trace

_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o":        {"input": 0.005,    "output": 0.015},
    "gpt-4o-mini":   {"input": 0.000150, "output": 0.000600},
    "gpt-4-turbo":   {"input": 0.010,    "output": 0.030},
    "gpt-4":         {"input": 0.030,    "output": 0.060},
    "gpt-3.5-turbo": {"input": 0.0005,   "output": 0.0015},
    "o1":            {"input": 0.015,    "output": 0.060},
    "o1-mini":       {"input": 0.003,    "output": 0.012},
    "o3":            {"input": 0.010,    "output": 0.040},
    "o3-mini":       {"input": 0.0011,   "output": 0.0044},
    "o4-mini":       {"input": 0.0011,   "output": 0.0044},
}
_FALLBACK_PRICING = {"input": 0.005, "output": 0.015}


def _price_for(model: str) -> dict[str, float]:
    if model in _PRICING:
        return _PRICING[model]
    for key in sorted(_PRICING, key=len, reverse=True):
        if model.startswith(key):
            return _PRICING[key]
    return _FALLBACK_PRICING


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = _price_for(model)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000


@dataclass
class _Bucket:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


class CostTracker:
    """Thread-safe accumulator for token usage and estimated USD cost."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_agent: dict[str, _Bucket] = defaultdict(_Bucket)
        self.by_model: dict[str, _Bucket] = defaultdict(_Bucket)
        self._totals = _Bucket()

    def record(self, *, agent_name: str, model: str,
               input_tokens: int, output_tokens: int) -> None:
        cost = _compute_cost(model, input_tokens, output_tokens)
        with self._lock:
            for bucket in (self.by_agent[agent_name],
                           self.by_model[model],
                           self._totals):
                bucket.input_tokens += input_tokens
                bucket.output_tokens += output_tokens
                bucket.cost_usd += cost
                bucket.calls += 1

    def total_cost(self) -> float:
        with self._lock:
            return self._totals.cost_usd

    def total_tokens(self) -> dict[str, int]:
        with self._lock:
            return {
                "input": self._totals.input_tokens,
                "output": self._totals.output_tokens,
                "total": self._totals.input_tokens + self._totals.output_tokens,
            }

    def summary(self) -> dict[str, Any]:
        def _fmt(b: _Bucket) -> dict[str, Any]:
            return {
                "input_tokens": b.input_tokens,
                "output_tokens": b.output_tokens,
                "cost_usd": round(b.cost_usd, 6),
                "calls": b.calls,
            }
        with self._lock:
            return {
                "total_cost_usd":      round(self._totals.cost_usd, 6),
                "total_input_tokens":  self._totals.input_tokens,
                "total_output_tokens": self._totals.output_tokens,
                "total_calls":         self._totals.calls,
                "by_agent": {k: _fmt(v) for k, v in self.by_agent.items()},
                "by_model":  {k: _fmt(v) for k, v in self.by_model.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self.by_agent.clear()
            self.by_model.clear()
            self._totals = _Bucket()

    def __repr__(self) -> str:
        s = self.summary()
        return (f"<CostTracker calls={s['total_calls']} "
                f"tokens={s['total_input_tokens']}+{s['total_output_tokens']} "
                f"cost=${s['total_cost_usd']:.6f}>")


from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.span_data import AgentSpanData, GenerationSpanData


class CostTrackerProcessor(TracingProcessor):
    """
    Tracing processor that feeds GenerationSpan usage into a CostTracker.

    Register once at startup:
        from agents.tracing import add_trace_processor
        add_trace_processor(CostTrackerProcessor(tracker))
    """

    def __init__(self, tracker: CostTracker) -> None:
        self.tracker = tracker
        self._active_agents: dict[str, str] = {}
        self._lock = threading.Lock()

    def on_span_start(self, span: Any) -> None:
        if isinstance(span.span_data, AgentSpanData):
            with self._lock:
                self._active_agents[span.span_id] = span.span_data.name

    def on_span_end(self, span: Any) -> None:
        data = span.span_data

        if isinstance(data, AgentSpanData):
            with self._lock:
                self._active_agents.pop(span.span_id, None)
            return

        if not isinstance(data, GenerationSpanData):
            return

        usage = data.usage
        if not usage:
            return

        input_tokens  = usage.get("input_tokens",  0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0
        if input_tokens == 0 and output_tokens == 0:
            return

        model      = data.model or "unknown"
        agent_name = self._resolve_agent_name(span)

        self.tracker.record(
            agent_name=agent_name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def on_trace_start(self, trace: Any) -> None:
        pass

    def on_trace_end(self, trace: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass

    def _resolve_agent_name(self, span: Any) -> str:
        with self._lock:
            parent_id = span.parent_id
            if parent_id and parent_id in self._active_agents:
                return self._active_agents[parent_id]
            if self._active_agents:
                return next(reversed(self._active_agents))
        return "unknown"
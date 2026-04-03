"""
cost_tracker.py — Per-agent token cost attribution for the OpenAI Agents SDK.

Hooks into the existing TracingProcessor pipeline with zero changes to
any existing code.

Basic usage::

    from agents import Agent, Runner
    from agents.tracing import add_trace_processor
    from agents.cost_tracker import CostTracker, CostTrackerProcessor

    tracker = CostTracker()
    add_trace_processor(CostTrackerProcessor(tracker))

    async def main():
        agent = Agent(name="Assistant", instructions="You are helpful.")
        await Runner.run(agent, "Explain black holes.")
        print(tracker.summary())
        # {
        #   "total_cost_usd": 0.000412,
        #   "total_input_tokens": 310,
        #   "total_output_tokens": 89,
        #   "total_calls": 2,
        #   "by_agent": {
        #     "Assistant": {"input_tokens": 310, "output_tokens": 89,
        #                   "cost_usd": 0.000412, "calls": 2}
        #   },
        #   "by_model": {
        #     "gpt-4o-mini": {"input_tokens": 310, "output_tokens": 89,
        #                     "cost_usd": 0.000412, "calls": 2}
        #   }
        # }
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agents.tracing.spans import Span
    from agents.tracing.traces import Trace

# ---------------------------------------------------------------------------
# Pricing table — USD per 1,000 tokens (April 2026)
# Update this table as OpenAI releases new models or changes pricing.
# See: https://openai.com/api/pricing
# ---------------------------------------------------------------------------
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

# Fallback pricing used when a model name doesn't match any entry above.
_FALLBACK_PRICING = {"input": 0.005, "output": 0.015}


def _price_for(model: str) -> dict[str, float]:
    """Return the pricing dict for a given model name.

    Performs exact match first, then longest-prefix match to handle versioned
    model names like ``gpt-4o-mini-2024-07-18`` correctly resolving to
    ``gpt-4o-mini`` rather than the shorter ``gpt-4o`` prefix.

    Args:
        model: The model name string as returned by the OpenAI API.

    Returns:
        A dict with ``"input"`` and ``"output"`` keys representing the
        cost per 1,000 tokens in USD.
    """
    if model in _PRICING:
        return _PRICING[model]
    # Sort by key length descending so "gpt-4o-mini" matches before "gpt-4o"
    for key in sorted(_PRICING, key=len, reverse=True):
        if model.startswith(key):
            return _PRICING[key]
    return _FALLBACK_PRICING


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute the estimated USD cost for a single LLM call.

    Args:
        model: The model name used for the call.
        input_tokens: Number of input (prompt) tokens consumed.
        output_tokens: Number of output (completion) tokens generated.

    Returns:
        Estimated cost in USD as a float.
    """
    p = _price_for(model)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000


# ---------------------------------------------------------------------------
# Internal accumulator bucket
# ---------------------------------------------------------------------------
@dataclass
class _Bucket:
    """Accumulates token usage and cost for a single agent or model slice."""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
class CostTracker:
    """Thread-safe accumulator for token usage and estimated USD cost.

    Attach to the tracing pipeline via :class:`CostTrackerProcessor`, then
    read results with :meth:`summary`, :meth:`total_cost`, or
    :meth:`total_tokens`.

    All public methods are safe to call from multiple threads simultaneously.

    Attributes:
        by_agent: Per-agent-name breakdown of token usage and cost.
        by_model: Per-model breakdown of token usage and cost.

    Example::

        tracker = CostTracker()
        add_trace_processor(CostTrackerProcessor(tracker))

        await Runner.run(agent, "Hello")

        print(tracker.total_cost())   # 0.000023
        print(tracker.summary())      # full breakdown dict
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_agent: dict[str, _Bucket] = defaultdict(_Bucket)
        self.by_model: dict[str, _Bucket] = defaultdict(_Bucket)
        self._totals = _Bucket()

    def record(
        self,
        *,
        agent_name: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record token usage for a single LLM call.

        Called automatically by :class:`CostTrackerProcessor`. You can also
        call this manually when testing or using a custom tracing setup.

        Args:
            agent_name: Name of the agent that made the call.
            model: Model name used for the call (e.g. ``"gpt-4o-mini"``).
            input_tokens: Number of input (prompt) tokens consumed.
            output_tokens: Number of output (completion) tokens generated.
        """
        cost = _compute_cost(model, input_tokens, output_tokens)
        with self._lock:
            for bucket in (
                self.by_agent[agent_name],
                self.by_model[model],
                self._totals,
            ):
                bucket.input_tokens += input_tokens
                bucket.output_tokens += output_tokens
                bucket.cost_usd += cost
                bucket.calls += 1

    def total_cost(self) -> float:
        """Return the total estimated USD cost across all recorded calls.

        Returns:
            Total cost in USD as a float.
        """
        with self._lock:
            return self._totals.cost_usd

    def total_tokens(self) -> dict[str, int]:
        """Return total token counts across all recorded calls.

        Returns:
            Dict with keys ``"input"``, ``"output"``, and ``"total"``.
        """
        with self._lock:
            return {
                "input": self._totals.input_tokens,
                "output": self._totals.output_tokens,
                "total": self._totals.input_tokens + self._totals.output_tokens,
            }

    def summary(self) -> dict[str, Any]:
        """Return a full JSON-serialisable cost breakdown.

        Returns:
            A dict containing total cost and token counts, plus per-agent
            and per-model breakdowns. Each breakdown entry contains
            ``input_tokens``, ``output_tokens``, ``cost_usd``, and
            ``calls``.

        Example::

            {
                "total_cost_usd": 0.000412,
                "total_input_tokens": 310,
                "total_output_tokens": 89,
                "total_calls": 2,
                "by_agent": {
                    "Researcher": {"input_tokens": 200, "output_tokens": 60,
                                   "cost_usd": 0.000270, "calls": 1},
                    "Writer":     {"input_tokens": 110, "output_tokens": 29,
                                   "cost_usd": 0.000142, "calls": 1}
                },
                "by_model": {
                    "gpt-4o-mini": {"input_tokens": 310, "output_tokens": 89,
                                    "cost_usd": 0.000412, "calls": 2}
                }
            }
        """
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
        """Clear all accumulated data.

        Useful when running multiple back-to-back runs in tests or scripts
        and you want a fresh tracker for each run.
        """
        with self._lock:
            self.by_agent.clear()
            self.by_model.clear()
            self._totals = _Bucket()

    def __repr__(self) -> str:
        s = self.summary()
        return (
            f"<CostTracker calls={s['total_calls']} "
            f"tokens={s['total_input_tokens']}+{s['total_output_tokens']} "
            f"cost=${s['total_cost_usd']:.6f}>"
        )


# ---------------------------------------------------------------------------
# Tracing processor
# ---------------------------------------------------------------------------
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.span_data import AgentSpanData, GenerationSpanData


class CostTrackerProcessor(TracingProcessor):
    """Tracing processor that feeds span usage data into a :class:`CostTracker`.

    Plugs into the SDK's existing ``TracingProcessor`` interface — no changes
    to any existing code required.

    How agent names are resolved
    ----------------------------
    The SDK emits spans in this nesting order::

        AgentSpan        (span_data.name = agent name)
          └─ GenerationSpan  (span_data.model + span_data.usage)

    This processor tracks open ``AgentSpan`` IDs and their names, then walks
    the ``parent_id`` of each ``GenerationSpan`` to attribute the cost to the
    correct agent.

    Args:
        tracker: The :class:`CostTracker` instance to record usage into.

    Example::

        from agents.tracing import add_trace_processor
        from agents.cost_tracker import CostTracker, CostTrackerProcessor

        tracker = CostTracker()
        add_trace_processor(CostTrackerProcessor(tracker))
    """

    def __init__(self, tracker: CostTracker) -> None:
        self.tracker = tracker
        # Maps open AgentSpan span_id -> agent name
        self._active_agents: dict[str, str] = {}
        self._lock = threading.Lock()

    def on_span_start(self, span: Any) -> None:
        """Track agent name when an AgentSpan opens.

        Args:
            span: The span that just started.
        """
        if isinstance(span.span_data, AgentSpanData):
            with self._lock:
                self._active_agents[span.span_id] = span.span_data.name

    def on_span_end(self, span: Any) -> None:
        """Record token usage when a GenerationSpan closes.

        Ignores all span types except ``GenerationSpanData``. Cleans up
        agent tracking when an ``AgentSpanData`` span closes.

        Args:
            span: The span that just finished.
        """
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

        self.tracker.record(
            agent_name=self._resolve_agent_name(span),
            model=data.model or "unknown",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def on_trace_start(self, trace: Any) -> None:
        """No-op. Required by the TracingProcessor interface."""
        pass

    def on_trace_end(self, trace: Any) -> None:
        """No-op. Required by the TracingProcessor interface."""
        pass

    def shutdown(self) -> None:
        """No-op. Required by the TracingProcessor interface."""
        pass

    def force_flush(self) -> None:
        """No-op. Required by the TracingProcessor interface."""
        pass

    def _resolve_agent_name(self, span: Any) -> str:
        """Resolve the agent name for a GenerationSpan.

        Walks the parent span ID to find the nearest open AgentSpan.
        Falls back to the most recently opened agent, then ``"unknown"``.

        Args:
            span: The GenerationSpan to resolve the agent name for.

        Returns:
            The agent name string, or ``"unknown"`` if none can be found.
        """
        with self._lock:
            parent_id = span.parent_id
            if parent_id and parent_id in self._active_agents:
                return self._active_agents[parent_id]
            if self._active_agents:
                return next(reversed(self._active_agents))
        return "unknown"
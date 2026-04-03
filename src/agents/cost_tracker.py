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

Custom pricing::

    tracker = CostTracker(pricing={
        "gpt-4o": {"input": 0.0025, "output": 0.010},
    })
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# SDK tracing imports — at the top where they belong
from agents.tracing.processor_interface import TracingProcessor
from agents.tracing.span_data import AgentSpanData, GenerationSpanData

# ResponseSpanData is the span type emitted by the default OpenAIResponsesModel
# path. Import it defensively — older SDK versions may not have it.
try:
    from agents.tracing.span_data import ResponseSpanData  # type: ignore[attr-defined]
    _HAS_RESPONSE_SPAN = True
except ImportError:
    ResponseSpanData = None  # type: ignore[assignment,misc]
    _HAS_RESPONSE_SPAN = False

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing table — USD per 1,000 tokens
# Last verified: April 2026
# Source: https://openai.com/api/pricing
#
# To supply your own rates pass a ``pricing`` dict to CostTracker():
#   CostTracker(pricing={"gpt-4o": {"input": 0.0025, "output": 0.010}})
# Entries not present in your custom table fall back to _DEFAULT_PRICING,
# then to _FALLBACK_PRICING.
# ---------------------------------------------------------------------------
_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # GPT-4o family (prices updated May 2024)
    "gpt-4o":        {"input": 0.0025,   "output": 0.010},
    "gpt-4o-mini":   {"input": 0.000150, "output": 0.000600},
    # GPT-4 family
    "gpt-4-turbo":   {"input": 0.010,    "output": 0.030},
    "gpt-4":         {"input": 0.030,    "output": 0.060},
    # GPT-3.5
    "gpt-3.5-turbo": {"input": 0.0005,   "output": 0.0015},
    # o-series reasoning models
    "o1":            {"input": 0.015,    "output": 0.060},
    "o1-mini":       {"input": 0.003,    "output": 0.012},
    "o3":            {"input": 0.010,    "output": 0.040},
    "o3-mini":       {"input": 0.0011,   "output": 0.0044},
    "o4-mini":       {"input": 0.0011,   "output": 0.0044},
}

# Used when a model name matches nothing in the pricing table. Logs a warning.
_FALLBACK_PRICING = {"input": 0.005, "output": 0.015}


def _price_for(
    model: str,
    custom: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """Return the pricing dict for *model*.

    Resolution order:
      1. Exact match in *custom* table (if supplied).
      2. Longest-prefix match in *custom* table.
      3. Exact match in :data:`_DEFAULT_PRICING`.
      4. Longest-prefix match in :data:`_DEFAULT_PRICING`.
      5. :data:`_FALLBACK_PRICING` (with a warning log).

    Longest-prefix matching means ``gpt-4o-mini-2024-07-18`` correctly
    resolves to ``gpt-4o-mini`` rather than the shorter ``gpt-4o``.
    """
    tables = [t for t in (custom, _DEFAULT_PRICING) if t]
    for table in tables:
        if model in table:
            return table[model]
        for key in sorted(table, key=len, reverse=True):
            if model.startswith(key):
                return table[key]
    log.warning(
        "cost_tracker: no pricing entry for model %r — using fallback "
        "($%.4f/$%.4f per 1k tokens). Pass a custom pricing table to "
        "CostTracker() to silence this warning.",
        model,
        _FALLBACK_PRICING["input"],
        _FALLBACK_PRICING["output"],
    )
    return _FALLBACK_PRICING


def _compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    custom_pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    """Return estimated USD cost for a single LLM call."""
    p = _price_for(model, custom_pricing)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000


def _extract_tokens(usage: Any) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a usage value.

    Handles both dict-style usage (some span types) and object-style usage
    (SDK dataclasses / Pydantic models). Returns ``(0, 0)`` if the value
    is ``None`` or the fields are absent.
    """
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        inp = usage.get("input_tokens", 0) or 0
        out = usage.get("output_tokens", 0) or 0
    else:
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
    return int(inp), int(out)


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
    The :attr:`by_agent` and :attr:`by_model` properties return a thread-safe
    snapshot of the internal dicts — the returned ``_Bucket`` objects are copies
    and will not reflect future updates. Use :meth:`summary` if you need a
    single consistent JSON-serialisable view.

    Args:
        pricing: Optional custom pricing table. Dict mapping model-name
            prefixes to ``{"input": <per-1k-USD>, "output": <per-1k-USD>}``.
            Entries here take priority over the built-in table.

    Example::

        tracker = CostTracker(pricing={
            "my-fine-tune": {"input": 0.008, "output": 0.024},
        })
        add_trace_processor(CostTrackerProcessor(tracker))

        await Runner.run(agent, "Hello")

        print(tracker.total_cost())   # 0.000023
        print(tracker.summary())      # full breakdown dict
    """

    def __init__(
        self,
        pricing: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._custom_pricing = pricing  # None means "use built-in table only"
        self._by_agent: dict[str, _Bucket] = defaultdict(_Bucket)
        self._by_model: dict[str, _Bucket] = defaultdict(_Bucket)
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
        cost = _compute_cost(model, input_tokens, output_tokens, self._custom_pricing)
        with self._lock:
            for bucket in (
                self._by_agent[agent_name],
                self._by_model[model],
                self._totals,
            ):
                bucket.input_tokens += input_tokens
                bucket.output_tokens += output_tokens
                bucket.cost_usd += cost
                bucket.calls += 1

    @property
    def by_agent(self) -> dict[str, _Bucket]:
        """Thread-safe snapshot of per-agent token usage.

        Returns a copy of the internal dict with each ``_Bucket`` also copied,
        so the snapshot is consistent and won't race with concurrent
        :meth:`record` calls.

        Example::

            tracker.by_agent["Researcher"].calls    # int
            tracker.by_agent["Researcher"].cost_usd # float
        """
        with self._lock:
            return {k: _Bucket(**v.__dict__) for k, v in self._by_agent.items()}

    @property
    def by_model(self) -> dict[str, _Bucket]:
        """Thread-safe snapshot of per-model token usage.

        Returns a copy — see :attr:`by_agent` for details.
        """
        with self._lock:
            return {k: _Bucket(**v.__dict__) for k, v in self._by_model.items()}

    def total_cost(self) -> float:
        """Return the total estimated USD cost across all recorded calls."""
        with self._lock:
            return self._totals.cost_usd

    def total_tokens(self) -> dict[str, int]:
        """Return total token counts across all recorded calls.

        Returns:
            Dict with keys ``"input"``, ``"output"``, and ``"total"``.
        """
        with self._lock:
            return {
                "input":  self._totals.input_tokens,
                "output": self._totals.output_tokens,
                "total":  self._totals.input_tokens + self._totals.output_tokens,
            }

    def summary(self) -> dict[str, Any]:
        """Return a full JSON-serialisable cost breakdown.

        Returns:
            A dict containing total cost and token counts, plus per-agent
            and per-model breakdowns.

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
                "input_tokens":  b.input_tokens,
                "output_tokens": b.output_tokens,
                "cost_usd":      round(b.cost_usd, 6),
                "calls":         b.calls,
            }

        with self._lock:
            return {
                "total_cost_usd":      round(self._totals.cost_usd, 6),
                "total_input_tokens":  self._totals.input_tokens,
                "total_output_tokens": self._totals.output_tokens,
                "total_calls":         self._totals.calls,
                "by_agent": {k: _fmt(v) for k, v in self._by_agent.items()},
                "by_model":  {k: _fmt(v) for k, v in self._by_model.items()},
            }

    def reset(self) -> None:
        """Clear all accumulated data.

        Useful when running multiple back-to-back runs in tests or scripts
        and you want a fresh tracker for each run.
        """
        with self._lock:
            self._by_agent.clear()
            self._by_model.clear()
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
class CostTrackerProcessor(TracingProcessor):
    """Tracing processor that feeds span usage data into a :class:`CostTracker`.

    Plugs into the SDK's existing ``TracingProcessor`` interface — no changes
    to any existing code required.

    Handled span types
    ------------------
    * ``GenerationSpanData`` — emitted by the legacy ``OpenAIChatCompletionsModel``
      path and custom model implementations.
    * ``ResponseSpanData`` — emitted by the default ``OpenAIResponsesModel`` path
      (the Responses API). Imported defensively; older SDK versions that pre-date
      this span type continue to work via ``GenerationSpanData`` only.

    How agent names are resolved
    ----------------------------
    The SDK emits spans in this nesting order::

        AgentSpan            (span_data.name = agent name)
          └─ GenerationSpan  (span_data.model + span_data.usage)
          └─ ResponseSpan    (span_data.model + span_data.usage)

    This processor tracks open ``AgentSpan`` IDs → names, then looks up the
    ``parent_id`` of each usage span to attribute cost to the correct agent.
    If the parent ID is not found (e.g. intermediate wrapper spans), cost is
    attributed to ``"unknown"`` and a warning is logged — never silently
    misattributed to a random agent.

    Args:
        tracker: The :class:`CostTracker` instance to record usage into.
    """

    def __init__(self, tracker: CostTracker) -> None:
        self.tracker = tracker
        # Maps open AgentSpan span_id -> agent name (human-readable string)
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

        # Determine usage + model depending on span type.
        usage: Any = None
        model: str = "unknown"

        if isinstance(data, GenerationSpanData):
            usage = data.usage
            model = data.model or "unknown"
        elif _HAS_RESPONSE_SPAN and isinstance(data, ResponseSpanData):
            # ResponseSpanData wraps the full API response object.
            # Usage lives at data.response.usage.
            response = getattr(data, "response", None)
            if response is not None:
                usage = getattr(response, "usage", None)
            model = getattr(data, "model", None) or "unknown"
        else:
            return  # span type we don't care about

        input_tokens, output_tokens = _extract_tokens(usage)
        if input_tokens == 0 and output_tokens == 0:
            return

        self.tracker.record(
            agent_name=self._resolve_agent_name(span),
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
        """Resolve the agent name for a usage span.

        Looks up ``span.parent_id`` in the active-agent table.  Does **not**
        fall back to a random agent on a miss — returns ``"unknown"`` and logs
        a warning so misattribution is always visible.

        Note: this method acquires ``self._lock`` independently; callers must
        not hold it when calling here to avoid deadlock.
        """
        with self._lock:
            parent_id = getattr(span, "parent_id", None)
            if parent_id and parent_id in self._active_agents:
                # Happy path: generation span is a direct child of an agent span.
                return self._active_agents[parent_id]

        # parent_id not in active agents — log and attribute to unknown rather
        # than silently picking whatever agent happens to be last in the dict.
        log.warning(
            "cost_tracker: could not resolve agent for span %r (parent_id=%r); "
            "attributing to 'unknown'. This may indicate intermediate wrapper "
            "spans between AgentSpan and GenerationSpan/ResponseSpan.",
            getattr(span, "span_id", "?"),
            getattr(span, "parent_id", "?"),
        )
        return "unknown"
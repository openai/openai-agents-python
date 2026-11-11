"""Token usage reported by the native thread."""

from __future__ import annotations

from collections.abc import Callable as Callable
from dataclasses import dataclass as dataclass
from threading import RLock
from typing import Any as Any, Literal as Literal

from ._public import preserve_module_identity as _preserve_module_identity

_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Native token counts. Cached and reasoning tokens are subsets, not additions."""

    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class RunUsage(TokenUsage):
    """Measured owner-local run totals, separate from native thread/context estimates.

    requests counts observed native response completions and explicitly reported
    external requests. missing_usage_requests counts requests without token data.
    Child owners remain separate until an authoritative parent run is known.
    """

    requests: int
    missing_usage_requests: int

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, name)) is not int or getattr(self, name) < 0
            for name in RunUsage.__dataclass_fields__
        ):
            raise ValueError("RunUsage counters must be nonnegative integers")
        if self.missing_usage_requests > self.requests:
            raise ValueError("Missing-usage requests exceed measured requests")


class _UsageCollector:
    def __init__(self, initial: RunUsage | None = None) -> None:
        self.lock = RLock()
        self.value = initial or RunUsage(0, 0, 0, 0, 0, 0, 0, 0)
        self.children: set[_UsageCollector] = set()

    @staticmethod
    def capture(usage: TokenUsage | None, requests: int = 1) -> RunUsage:
        if type(requests) is not int or requests < 0:
            raise ValueError("requests must be a nonnegative integer")
        names = tuple(TokenUsage.__dataclass_fields__)
        if usage is not None and not isinstance(usage, TokenUsage):
            raise TypeError("usage must be TokenUsage or None")
        counts = tuple(0 if usage is None else getattr(usage, name) for name in names)
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("usage counters must be nonnegative integers")
        return RunUsage(
            **dict(zip(names, counts, strict=True)),
            requests=requests,
            missing_usage_requests=requests if usage is None else 0,
        )

    def add(self, delta: RunUsage) -> None:
        with self.lock:
            self.value = RunUsage(
                *(
                    getattr(self.value, name) + getattr(delta, name)
                    for name in RunUsage.__dataclass_fields__
                )
            )

    def report(self, usage: TokenUsage | None, requests: int = 1) -> None:
        self.add(self.capture(usage, requests))

    def observe(self, raw: dict[str, Any] | None) -> None:
        try:
            usage = None if raw is None else TokenUsage(**raw)
            self.report(usage)
        except (TypeError, ValueError):
            self.report(None)

    def attach(self, child: _UsageCollector) -> None:
        with self.lock:
            self.children.add(child)

    def snapshot(self) -> RunUsage:
        with self.lock:
            value = self.value
            children = tuple(self.children)
        for child in children:
            nested = child.snapshot()
            value = RunUsage(
                *(
                    getattr(value, name) + getattr(nested, name)
                    for name in RunUsage.__dataclass_fields__
                )
            )
        return value


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Optional limits for the next root sampling admission in a direct native run.

    Set at least one limit. Each limit is an integer from zero through
    2**53 - 1. A zero limit blocks the first sampling admission. The SDK
    stops at the next admission when any limit has been reached. An admitted
    request can overshoot a token or cost limit and still return a final result.

    max_requests counts root sampling-step admissions, including prompted
    output repairs. Transport retries, internal requests such as compaction,
    and child requests do not charge this counter. Token and cost limits use
    reported response completions on the active root, including any reported
    completions from internal requests on that root. They exclude child
    responses and thread usage snapshots.

    Before the next admission, the SDK checks known limits first. Under
    the default policy, missing usage also blocks admission before any
    estimates run. If a cost limit needs an estimate, the SDK calls
    estimate_cost_micros synchronously with the response's TokenUsage.
    Core fills omitted token details such as cached and reasoning tokens
    with zero. Return an integer from zero through 2**53 - 1 in micro-USD
    (millionths of a US dollar), or None for an unknown estimate.
    Estimates can differ from actual charges.

    Missing usage, a negative token total, an absent estimator for a cost
    limit, or an unknown estimate blocks the next admission by default.
    missing_usage="continue" lets the run continue using known values.
    Reported zero is a known value. A request-only budget works without
    usage or an estimator.

    A blocked admission raises NativeError with code "run_budget_exceeded"
    or "run_budget_usage_unavailable". An estimator that raises an exception
    or returns an invalid value stops with "run_budget_invalid_estimate".

    The budget follows live replies, same-run steering, and output repair.
    Active runs with a budget cannot be captured or resumed from portable
    checkpoints. An idle checkpoint can start a new run with a fresh budget.
    """

    max_requests: int | None = None
    max_tokens: int | None = None
    max_estimated_cost_micros: int | None = None
    estimate_cost_micros: Callable[[TokenUsage], int | None] | None = None
    missing_usage: Literal["stop", "continue"] = "stop"

    def __post_init__(self) -> None:
        limits = {
            "max_requests": self.max_requests,
            "max_tokens": self.max_tokens,
            "max_estimated_cost_micros": self.max_estimated_cost_micros,
        }
        if all(limit is None for limit in limits.values()):
            raise ValueError("RunBudget requires at least one limit")
        for name, limit in limits.items():
            if limit is not None and (
                type(limit) is not int or not 0 <= limit <= _MAX_SAFE_INTEGER
            ):
                raise ValueError(f"RunBudget.{name} must be a nonnegative safe integer")
        if self.estimate_cost_micros is not None and not callable(self.estimate_cost_micros):
            raise TypeError("RunBudget.estimate_cost_micros must be callable")
        if self.missing_usage not in ("stop", "continue"):
            raise ValueError("RunBudget.missing_usage must be 'stop' or 'continue'")


@dataclass(frozen=True, slots=True)
class TokenUsageInfo:
    """The native thread's cumulative usage and its most recent usage.

    These are thread snapshots, not this submission's cost or a bill. Native
    counts can include restored history or context estimates. They do not
    aggregate child threads. The most recent usage may span only one response
    even when a submission required several model responses.
    """

    total_token_usage: TokenUsage
    last_token_usage: TokenUsage
    model_context_window: int | None


def _token_usage_info(info: dict[str, Any] | None) -> TokenUsageInfo | None:
    if info is None:
        return None
    return TokenUsageInfo(
        total_token_usage=TokenUsage(**info["total_token_usage"]),
        last_token_usage=TokenUsage(**info["last_token_usage"]),
        model_context_window=info["model_context_window"],
    )


_preserve_module_identity(globals(), "agents.usage")

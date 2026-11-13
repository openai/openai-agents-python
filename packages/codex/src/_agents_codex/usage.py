"""Token usage reported by the native thread."""

from __future__ import annotations

from collections.abc import Callable as Callable
from dataclasses import dataclass as dataclass, field
from threading import RLock
from typing import Any as Any, Literal as Literal

from ._public import preserve_module_identity as _preserve_module_identity

_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True)
class RawProviderUsage:
    """Original usage from one accepted provider response."""

    response_id: str
    upstream_request_id: str | None
    usage: dict[str, Any] = field(repr=False)


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

    requests includes native logical generation attempts, untagged internal or
    legacy completions, and explicit external reports. missing_usage_requests
    includes requests without measured token data.
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


def _subtract_configured_usage(current: RunUsage, baseline: RunUsage) -> RunUsage:
    return RunUsage(
        *(
            getattr(current, name) - getattr(baseline, name)
            for name in RunUsage.__dataclass_fields__
        )
    )


def _merge_configured_child_restore_usage(
    parent: RunUsage, child: RunUsage, accounted: RunUsage | None = None
) -> RunUsage:
    accounted = accounted or RunUsage(0, 0, 0, 0, 0, 0, 0, 0)
    _subtract_configured_usage(parent, accounted)
    delta = _subtract_configured_usage(child, accounted)
    return RunUsage(
        *(getattr(parent, name) + getattr(delta, name) for name in RunUsage.__dataclass_fields__)
    )


class _UsageCollector:
    def __init__(self, initial: RunUsage | None = None) -> None:
        self.lock = RLock()
        self.value = initial or RunUsage(0, 0, 0, 0, 0, 0, 0, 0)
        self.children: set[_UsageCollector] = set()
        self.configured_children: dict[_UsageCollector, RunUsage] = {}
        # Native generation totals replace this mirror; value retains only explicit,
        # historical-offset and untagged internal-request contributions.
        self.model_usage: RunUsage | None = None
        self.model_owner_attempt_id: str | None = None
        self.model_terminal_attempt_id: str | None = None
        self.model_final = False
        self._managed_model_owner: str | None = None

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

    def observe(self, raw: dict[str, Any] | None, model_attempt_id: str | None = None) -> None:
        if model_attempt_id is not None:
            if not isinstance(model_attempt_id, str) or not model_attempt_id:
                raise ValueError("Invalid native model attempt identity")
            # The exact native attempt owns count, tokens and missing usage together.
            # Untagged legacy and compaction completions retain their additive path.
            return
        try:
            usage = None if raw is None else TokenUsage(**raw)
            self.report(usage)
        except (TypeError, ValueError):
            self.report(None)

    def manage_provider_attempts(self) -> None:
        """Defer logical mirrors after Core accepts receipt-owned physical accounting."""
        with self.lock:
            if self.model_final or self.model_owner_attempt_id is None:
                raise ValueError("Native physical accounting requires a live model owner")
            if self._managed_model_owner is None:
                self._managed_model_owner = self.model_owner_attempt_id
                self.model_usage = None
            elif self._managed_model_owner != self.model_owner_attempt_id:
                raise ValueError("Native physical accounting owner changed")

    def replace_model_attempt(self, snapshot: dict[str, Any], *, final: bool = False) -> None:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("usage"), dict):
            raise ValueError("Invalid native model attempt snapshot")
        raw = snapshot["usage"]
        requests, missing = raw.get("requests"), raw.get("requests_without_token_usage")
        if (
            type(requests) is not int
            or not 0 <= requests <= 2**64 - 1
            or type(missing) is not int
            or not 0 <= missing <= requests
        ):
            raise ValueError("Invalid native model usage counters")
        owner = snapshot.get("owner_attempt_id")
        if (owner is not None and (not isinstance(owner, str) or not owner)) or (
            requests > 0 and owner is None
        ):
            raise ValueError("Invalid native model usage owner")
        terminal = snapshot.get("terminal_attempt_id")
        if terminal is not None and (not isinstance(terminal, str) or not terminal):
            raise ValueError("Invalid native model terminal identity")
        tokens = raw.get("token_usage")
        if not isinstance(tokens, dict):
            raise ValueError("Invalid native model token usage")
        measured = self.capture(TokenUsage(**tokens), requests)
        captured = RunUsage(
            **{name: getattr(measured, name) for name in TokenUsage.__dataclass_fields__},
            requests=requests,
            missing_usage_requests=missing,
        )
        with self.lock:
            if requests == 0 and owner is not None and self._managed_model_owner != owner:
                raise ValueError("Invalid native model usage owner")
            if self.model_final and not final:
                return
            if self._managed_model_owner is not None:
                if owner != self._managed_model_owner and not (requests == 0 and owner is None):
                    raise ValueError("Native physical accounting owner changed")
                # Logical started/settled events still describe the attempt timeline.
                # Only Core's terminal snapshot owns the physical transport totals.
                if not final:
                    return
            elif self.model_owner_attempt_id is not None and owner != self.model_owner_attempt_id:
                raise ValueError("Native model usage owner changed")
            if self.model_usage is not None and captured.requests < self.model_usage.requests:
                if final:
                    raise ValueError("Native final model usage regressed")
                return
            self.model_usage = captured
            self.model_owner_attempt_id = owner
            self.model_terminal_attempt_id = terminal
            self.model_final = self.model_final or final

    def attach(self, child: _UsageCollector) -> None:
        with self.lock:
            self.children.add(child)

    def attach_configured_child(
        self, child: _UsageCollector, accounted: RunUsage | None = None
    ) -> None:
        if child is self:
            raise ValueError("Configured child cannot own its parent usage")
        with self.lock:
            if child not in self.configured_children:
                if child in self.children:
                    raise ValueError("Configured child already has a plain usage link")
                _merge_configured_child_restore_usage(self.snapshot(), child.snapshot(), accounted)
                self.configured_children[child] = accounted or RunUsage(0, 0, 0, 0, 0, 0, 0, 0)

    def snapshot(self) -> RunUsage:
        with self.lock:
            value = self.value
            if self.model_usage is not None:
                value = RunUsage(
                    *(
                        getattr(value, name) + getattr(self.model_usage, name)
                        for name in RunUsage.__dataclass_fields__
                    )
                )
            children = tuple(self.children)
            configured_children = tuple(self.configured_children.items())
        for child in children:
            nested = child.snapshot()
            value = RunUsage(
                *(
                    getattr(value, name) + getattr(nested, name)
                    for name in RunUsage.__dataclass_fields__
                )
            )
        for child, baseline in configured_children:
            if child in children:
                raise ValueError("Configured child already has a plain usage link")
            delta = _subtract_configured_usage(child.snapshot(), baseline)
            value = RunUsage(
                *(
                    getattr(value, name) + getattr(delta, name)
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
    Request-only limits have been retaining prior admissions across supported
    manual checkpoints, with the original ceiling supplied again on restore.
    Token limits have been retaining observed consumption and missing usage,
    with matching current limits and missing-usage policy on restore.
    Estimated-cost limits have been rejecting active portable capture.
    Idle checkpoints have been starting new runs with fresh budgets.
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

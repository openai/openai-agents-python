"""Application retry decisions; the native runtime owns attempts and waiting."""

from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ._public import preserve_module_identity as _preserve_module_identity
from .usage import TokenUsage


def _delay(value: float | None, name: str) -> None:
    try:
        valid = value is None or (
            type(value) in (int, float) and math.isfinite(value) and value >= 0
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite nonnegative number or None")


@dataclass(frozen=True, slots=True)
class RetryBackoffSettings:
    """Milliseconds between attempts, excluding each attempt's timeout."""

    initial_delay_ms: float = 250
    max_delay_ms: float = 2_000
    multiplier: float = 2
    jitter: bool = True

    def __post_init__(self) -> None:
        for name in ("initial_delay_ms", "max_delay_ms", "multiplier"):
            value = getattr(self, name)
            _delay(value, name)
            if value is None:
                raise ValueError(f"{name} cannot be None")
        if type(self.jitter) is not bool:
            raise TypeError("jitter must be a bool")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "initial_delay_ms": self.initial_delay_ms,
            "max_delay_ms": self.max_delay_ms,
            "multiplier": self.multiplier,
            "jitter": self.jitter,
        }


@dataclass(frozen=True, slots=True)
class NormalizedError:
    """Captured error facts, independent of subsequent application mutation."""

    message: str = ""
    status_code: int | None = None
    error_code: str | None = None
    request_id: str | None = None
    retry_after_ms: float | None = None
    is_abort: bool = False
    is_network_error: bool = False
    is_timeout: bool = False


@dataclass(frozen=True, slots=True)
class RetryAdvice:
    suggested: bool | None = None
    retry_after_ms: float | None = None
    replay_safety: Literal["safe", "unsafe", "unknown"] = "unknown"
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.suggested is not None and type(self.suggested) is not bool:
            raise TypeError("suggested must be a bool or None")
        _delay(self.retry_after_ms, "retry_after_ms")
        if self.replay_safety not in ("safe", "unsafe", "unknown"):
            raise ValueError("replay_safety must be safe, unsafe, or unknown")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be a string or None")


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    delay_ms: float | None = None
    reason: str | None = None
    approve_unsafe_replay: bool = False
    _veto: Literal["hard", "provider_unsafe"] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.retry) is not bool or type(self.approve_unsafe_replay) is not bool:
            raise TypeError("retry and approve_unsafe_replay must be bools")
        _delay(self.delay_ms, "delay_ms")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be a string or None")


@dataclass(frozen=True, slots=True)
class RetryPolicyContext:
    attempt_id: str
    attempt: int
    request_attempt: int
    max_retries: int
    model: str
    thread_id: str
    turn_id: str
    transport: str
    normalized: NormalizedError
    error: BaseException | None = field(default=None, repr=False)
    provider_advice: RetryAdvice | None = None
    response_started: bool = False
    retry_unsafe_output: bool = False
    local_effects: bool = False
    stateful: bool = False
    previous_response_id: str | None = None
    conversation_id: str | None = None
    usage: TokenUsage | None = None


RetryPolicy = Callable[[RetryPolicyContext], bool | RetryDecision | Awaitable[bool | RetryDecision]]


@dataclass(frozen=True, slots=True)
class ModelRetrySettings:
    """Captured retry settings. Configuration JSON excludes the policy callable.

    Omitted members preserve accepted settings within the same captured source.
    A newly configured budget defaults to zero; absent retry settings retain the
    native provider defaults. Zero disables the conversation-locked retry too.
    An affirmative decision cannot override cancellation, exposed unsafe output,
    admitted local effects, or invalid protocol/tool authority.
    """

    max_retries: int | None = None
    backoff: RetryBackoffSettings | None = None
    policy: RetryPolicy | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_retries is not None and (
            type(self.max_retries) is not int or not 0 <= self.max_retries <= 4_294_967_295
        ):
            raise ValueError("max_retries must be an unsigned 32-bit integer or None")
        if self.backoff is not None and not isinstance(self.backoff, RetryBackoffSettings):
            raise TypeError("backoff must be RetryBackoffSettings or None")
        if self.policy is not None and not callable(self.policy):
            raise TypeError("policy must be callable or None")

    def to_json_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.max_retries is not None:
            result["max_retries"] = self.max_retries
        if self.backoff is not None:
            result["backoff"] = self.backoff.to_json_dict()
        return result


async def _evaluate(policy: RetryPolicy, context: RetryPolicyContext) -> RetryDecision:
    result = policy(context)
    if inspect.isawaitable(result):
        result = await result
    if type(result) is bool:
        return RetryDecision(result)
    if not isinstance(result, RetryDecision):
        raise TypeError("Retry policy must return a bool or RetryDecision")
    return result


class _RetryPolicies:
    def never(self) -> RetryPolicy:
        return lambda _context: False

    def provider_suggested(self) -> RetryPolicy:
        def policy(context: RetryPolicyContext) -> RetryDecision:
            advice = context.provider_advice
            if advice is None or advice.suggested is None:
                return RetryDecision(False)
            if not advice.suggested:
                return RetryDecision(
                    False,
                    reason=advice.reason,
                    _veto="provider_unsafe" if advice.replay_safety == "unsafe" else "hard",
                )
            return RetryDecision(True, delay_ms=advice.retry_after_ms, reason=advice.reason)

        return policy

    def network_error(self) -> RetryPolicy:
        return lambda context: context.normalized.is_network_error or context.normalized.is_timeout

    def retry_after(self) -> RetryPolicy:
        def policy(context: RetryPolicyContext) -> RetryDecision:
            delay = context.normalized.retry_after_ms
            if delay is None and context.provider_advice is not None:
                delay = context.provider_advice.retry_after_ms
            return RetryDecision(delay is not None, delay_ms=delay)

        return policy

    def http_status(self, statuses: Iterable[int]) -> RetryPolicy:
        allowed = frozenset(statuses)
        if any(type(status) is not int or not 100 <= status <= 599 for status in allowed):
            raise ValueError("statuses must contain HTTP status integers")
        return lambda context: context.normalized.status_code in allowed

    def _combine(self, policies: tuple[RetryPolicy, ...], require_all: bool) -> RetryPolicy:
        async def policy(context: RetryPolicyContext) -> RetryDecision:
            positive: RetryDecision | None = None
            negative = RetryDecision(False)
            veto: RetryDecision | None = None
            for predicate in policies:
                decision = await _evaluate(predicate, context)
                if decision._veto == "hard":
                    return decision
                if decision._veto == "provider_unsafe":
                    veto = veto or decision
                    continue
                if not decision.retry:
                    if require_all:
                        return decision
                    negative = decision
                    continue
                if positive is None:
                    positive = decision
                else:
                    positive = replace(
                        positive,
                        delay_ms=(
                            decision.delay_ms
                            if decision.delay_ms is not None
                            else positive.delay_ms
                        ),
                        reason=decision.reason if decision.reason is not None else positive.reason,
                        approve_unsafe_replay=(
                            positive.approve_unsafe_replay or decision.approve_unsafe_replay
                        ),
                    )
            if veto is not None and (positive is None or not positive.approve_unsafe_replay):
                return veto
            if veto is not None and positive is not None:
                return replace(positive, reason=positive.reason or veto.reason)
            return positive or negative

        return policy

    def all(self, *policies: RetryPolicy) -> RetryPolicy:
        return self._combine(policies, require_all=True)

    def any(self, *policies: RetryPolicy) -> RetryPolicy:
        return self._combine(policies, require_all=False)


retry_policies = _RetryPolicies()

_preserve_module_identity(globals(), "agents.retry")

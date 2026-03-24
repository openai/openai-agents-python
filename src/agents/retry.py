from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass
from typing_extensions import TypeAlias

from .util._types import MaybeAwaitable


@pydantic_dataclass
class ModelRetryBackoffSettings:
    """Backoff configuration for runner-managed model retries."""

    initial_delay: float | None = None
    """Delay in seconds before the first retry attempt."""

    max_delay: float | None = None
    """Maximum delay in seconds between retry attempts."""

    multiplier: float | None = None
    """Multiplier applied after each retry attempt."""

    jitter: bool | None = None
    """Whether to apply random jitter to the computed delay."""

    def to_json_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


ModelRetryBackoffInput: TypeAlias = ModelRetryBackoffSettings | dict[str, Any]


def _coerce_backoff_settings(
    value: ModelRetryBackoffInput | None,
) -> ModelRetryBackoffSettings | None:
    if value is None or isinstance(value, ModelRetryBackoffSettings):
        return value
    return ModelRetryBackoffSettings(**value)


_UNSET: Any = object()


@dataclass(init=False)
class ModelRetryNormalizedError:
    """Normalized error facts exposed to retry policies."""

    status_code: int | None = None
    error_code: str | None = None
    message: str | None = None
    request_id: str | None = None
    retry_after: float | None = None
    is_abort: bool = False
    is_network_error: bool = False
    is_timeout: bool = False

    def __init__(
        self,
        status_code: int | None = _UNSET,
        error_code: str | None = _UNSET,
        message: str | None = _UNSET,
        request_id: str | None = _UNSET,
        retry_after: float | None = _UNSET,
        is_abort: bool = _UNSET,
        is_network_error: bool = _UNSET,
        is_timeout: bool = _UNSET,
    ) -> None:
        explicit_fields: set[str] = set()

        def assign(name: str, value: Any, default: Any) -> Any:
            if value is _UNSET:
                return default
            explicit_fields.add(name)
            return value

        self.status_code = assign("status_code", status_code, None)
        self.error_code = assign("error_code", error_code, None)
        self.message = assign("message", message, None)
        self.request_id = assign("request_id", request_id, None)
        self.retry_after = assign("retry_after", retry_after, None)
        self.is_abort = assign("is_abort", is_abort, False)
        self.is_network_error = assign("is_network_error", is_network_error, False)
        self.is_timeout = assign("is_timeout", is_timeout, False)
        self._explicit_fields = frozenset(explicit_fields)


@dataclass
class ModelRetryAdvice:
    """Provider-specific retry guidance returned by model adapters."""

    suggested: bool | None = None
    retry_after: float | None = None
    replay_safety: str | None = None
    reason: str | None = None
    normalized: ModelRetryNormalizedError | None = None


@dataclass
class ModelRetryAdviceRequest:
    """Context passed to a model adapter when deriving retry advice."""

    error: Exception
    attempt: int
    stream: bool
    previous_response_id: str | None = None
    conversation_id: str | None = None


@dataclass
class RetryDecision:
    """Explicit retry decision returned by retry policies."""

    retry: bool
    delay: float | None = None
    reason: str | None = None
    _hard_veto: bool = field(default=False, init=False, repr=False, compare=False)
    _approves_replay: bool = field(default=False, init=False, repr=False, compare=False)


@dataclass
class RetryPolicyContext:
    """Context passed to runtime retry policy callbacks."""

    error: Exception
    attempt: int
    max_retries: int
    stream: bool
    normalized: ModelRetryNormalizedError
    provider_advice: ModelRetryAdvice | None = None


RetryPolicy: TypeAlias = Callable[[RetryPolicyContext], MaybeAwaitable[bool | RetryDecision]]
_RETRIES_SAFE_TRANSPORT_ERRORS_ATTR = "_openai_agents_retries_safe_transport_errors"
_RETRIES_ALL_TRANSIENT_ERRORS_ATTR = "_openai_agents_retries_all_transient_errors"


def _mark_retry_capabilities(
    policy: RetryPolicy,
    *,
    retries_safe_transport_errors: bool,
    retries_all_transient_errors: bool,
) -> RetryPolicy:
    setattr(policy, _RETRIES_SAFE_TRANSPORT_ERRORS_ATTR, retries_safe_transport_errors)  # noqa: B010
    setattr(policy, _RETRIES_ALL_TRANSIENT_ERRORS_ATTR, retries_all_transient_errors)  # noqa: B010
    return policy


def retry_policy_retries_safe_transport_errors(policy: RetryPolicy | None) -> bool:
    return bool(policy and getattr(policy, _RETRIES_SAFE_TRANSPORT_ERRORS_ATTR, False))


def retry_policy_retries_all_transient_errors(policy: RetryPolicy | None) -> bool:
    return bool(policy and getattr(policy, _RETRIES_ALL_TRANSIENT_ERRORS_ATTR, False))


@pydantic_dataclass
class ModelRetrySettings:
    """Opt-in runner-managed retry settings for model calls."""

    max_retries: int | None = None
    """Retries allowed after the initial model request."""

    backoff: ModelRetryBackoffInput | None = None
    """Backoff settings applied when the policy retries without an explicit delay."""

    policy: Callable[..., Any] | None = Field(default=None, exclude=True, repr=False)
    """Runtime-only retry policy callback. This field is not serialized."""

    def __post_init__(self) -> None:
        self.backoff = _coerce_backoff_settings(self.backoff)

    def to_json_dict(self) -> dict[str, Any]:
        backoff = _coerce_backoff_settings(self.backoff)
        return {
            "max_retries": self.max_retries,
            "backoff": backoff.to_json_dict() if backoff is not None else None,
        }


def _coerce_decision(value: bool | RetryDecision) -> RetryDecision:
    if isinstance(value, RetryDecision):
        return value
    return RetryDecision(retry=bool(value))


async def _evaluate_policy(
    policy: RetryPolicy,
    context: RetryPolicyContext,
) -> RetryDecision:
    value = policy(context)
    if isawaitable(value):
        value = await value
    return _coerce_decision(value)


def _with_hard_veto(decision: RetryDecision) -> RetryDecision:
    decision._hard_veto = True
    return decision


def _with_replay_safe_approval(decision: RetryDecision) -> RetryDecision:
    decision._approves_replay = True
    return decision


def _merge_positive_retry_decisions(
    existing: RetryDecision,
    incoming: RetryDecision,
) -> RetryDecision:
    merged = RetryDecision(
        retry=True,
        delay=existing.delay,
        reason=existing.reason,
    )
    if existing._approves_replay:
        merged = _with_replay_safe_approval(merged)
    if incoming.delay is not None:
        merged.delay = incoming.delay
    if incoming.reason is not None:
        merged.reason = incoming.reason
    if incoming._approves_replay:
        merged = _with_replay_safe_approval(merged)
    return merged


class _RetryPolicies:
    def never(self) -> RetryPolicy:
        def policy(_context: RetryPolicyContext) -> bool:
            return False

        return _mark_retry_capabilities(
            policy,
            retries_safe_transport_errors=False,
            retries_all_transient_errors=False,
        )

    def provider_suggested(self) -> RetryPolicy:
        def policy(context: RetryPolicyContext) -> bool | RetryDecision:
            advice = context.provider_advice
            if advice is None or advice.suggested is None:
                return False
            if advice.suggested is False:
                return _with_hard_veto(RetryDecision(retry=False, reason=advice.reason))
            decision = RetryDecision(retry=True, delay=advice.retry_after, reason=advice.reason)
            if advice.replay_safety == "safe":
                return _with_replay_safe_approval(decision)
            return decision

        return _mark_retry_capabilities(
            policy,
            retries_safe_transport_errors=True,
            retries_all_transient_errors=False,
        )

    def network_error(self) -> RetryPolicy:
        def policy(context: RetryPolicyContext) -> bool:
            return context.normalized.is_network_error or context.normalized.is_timeout

        return _mark_retry_capabilities(
            policy,
            retries_safe_transport_errors=True,
            retries_all_transient_errors=False,
        )

    def retry_after(self) -> RetryPolicy:
        def policy(context: RetryPolicyContext) -> bool | RetryDecision:
            delay = context.normalized.retry_after
            if delay is None and context.provider_advice is not None:
                delay = context.provider_advice.retry_after
            if delay is None:
                return False
            return RetryDecision(retry=True, delay=delay)

        return _mark_retry_capabilities(
            policy,
            retries_safe_transport_errors=False,
            retries_all_transient_errors=False,
        )

    def http_status(self, statuses: Iterable[int]) -> RetryPolicy:
        allowed = frozenset(statuses)

        def policy(context: RetryPolicyContext) -> bool:
            status_code = context.normalized.status_code
            return status_code is not None and status_code in allowed

        return _mark_retry_capabilities(
            policy,
            retries_safe_transport_errors=False,
            retries_all_transient_errors=False,
        )

    def all(self, *policies: RetryPolicy) -> RetryPolicy:
        if not policies:
            return self.never()

        async def policy(context: RetryPolicyContext) -> bool | RetryDecision:
            merged = RetryDecision(retry=True)
            for predicate in policies:
                decision = await _evaluate_policy(predicate, context)
                if decision._hard_veto:
                    return decision
                if not decision.retry:
                    return decision
                if decision.delay is not None:
                    merged.delay = decision.delay
                if decision.reason is not None:
                    merged.reason = decision.reason
                if decision._approves_replay:
                    merged = _with_replay_safe_approval(merged)

            return merged

        return _mark_retry_capabilities(
            policy,
            retries_safe_transport_errors=all(
                retry_policy_retries_safe_transport_errors(predicate) for predicate in policies
            ),
            retries_all_transient_errors=all(
                retry_policy_retries_all_transient_errors(predicate) for predicate in policies
            ),
        )

    def any(self, *policies: RetryPolicy) -> RetryPolicy:
        if not policies:
            return self.never()

        async def policy(context: RetryPolicyContext) -> bool | RetryDecision:
            first_positive: RetryDecision | None = None
            last_negative: RetryDecision | None = None
            for predicate in policies:
                decision = await _evaluate_policy(predicate, context)
                if decision._hard_veto:
                    return decision
                if decision.retry:
                    if first_positive is None:
                        first_positive = decision
                    else:
                        first_positive = _merge_positive_retry_decisions(first_positive, decision)
                    continue
                last_negative = decision

            return first_positive or last_negative or RetryDecision(retry=False)

        return _mark_retry_capabilities(
            policy,
            retries_safe_transport_errors=any(
                retry_policy_retries_safe_transport_errors(predicate) for predicate in policies
            ),
            retries_all_transient_errors=any(
                retry_policy_retries_all_transient_errors(predicate) for predicate in policies
            ),
        )


retry_policies = _RetryPolicies()

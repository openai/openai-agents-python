from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, TypeAlias

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from .util._types import MaybeAwaitable


@pydantic_dataclass
class ModelRetryBackoffSettings:
    """Backoff configuration for runner-managed model retries."""

    initial_delay: float | None = Field(default=None, ge=0)
    """Delay in seconds before the first retry attempt."""

    max_delay: float | None = Field(default=None, ge=0)
    """Maximum delay in seconds between retry attempts."""

    multiplier: float | None = Field(default=None, ge=0)
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
    response_started: bool = False
    """Whether the provider had begun emitting the response when the failure occurred."""


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
    approve_unsafe_replay: bool = False
    """Explicit application approval to replay a request the provider marked replay-unsafe.

    This is deliberately separate from ``retry``: an ordinary ``RetryDecision(retry=True)``
    never bypasses replay protection. Set this only for workloads where repeating
    provider-side work that may already have happened is acceptable.
    """
    _hard_veto: bool = field(default=False, init=False, repr=False, compare=False)
    _delegable_replay_veto: bool = field(default=False, init=False, repr=False, compare=False)
    _approves_replay: bool = field(default=False, init=False, repr=False, compare=False)


@dataclass(frozen=True)
class _ProviderRetryAuthority:
    suggested: bool | None
    replay_safety: str
    response_started: bool


@dataclass
class RetryPolicyContext:
    """Context passed to runtime retry policy callbacks."""

    error: Exception
    attempt: int
    max_retries: int
    stream: bool
    normalized: ModelRetryNormalizedError
    provider_advice: ModelRetryAdvice | None = None
    previous_response_id: str | None = None
    conversation_id: str | None = None
    _provider_authority: _ProviderRetryAuthority = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        advice = self.provider_advice
        replay_safety = advice.replay_safety if advice is not None else None
        self._provider_authority = _ProviderRetryAuthority(
            suggested=advice.suggested if advice is not None else None,
            replay_safety=(replay_safety if replay_safety in {"safe", "unsafe"} else "unknown"),
            response_started=advice.response_started if advice is not None else False,
        )

    @property
    def response_started(self) -> bool:
        """Whether the provider had begun emitting the response when the failure occurred."""
        return self._provider_authority.response_started

    @property
    def replay_safety(self) -> str:
        """Provider replay classification: ``"safe"``, ``"unsafe"`` or ``"unknown"``."""
        return self._provider_authority.replay_safety

    @property
    def stateful_request(self) -> bool:
        """Whether the request carried ``previous_response_id`` or ``conversation_id``."""
        return bool(self.previous_response_id or self.conversation_id)


RetryPolicy: TypeAlias = Callable[[RetryPolicyContext], MaybeAwaitable[bool | RetryDecision]]
_RETRIES_SAFE_TRANSPORT_ERRORS_ATTR = "_openai_agents_retries_safe_transport_errors"
_RETRIES_ALL_TRANSIENT_ERRORS_ATTR = "_openai_agents_retries_all_transient_errors"


def _mark_retry_capabilities(
    policy: RetryPolicy,
    *,
    retries_safe_transport_errors: bool,
    retries_all_transient_errors: bool,
) -> RetryPolicy:
    setattr(policy, _RETRIES_SAFE_TRANSPORT_ERRORS_ATTR, retries_safe_transport_errors)
    setattr(policy, _RETRIES_ALL_TRANSIENT_ERRORS_ATTR, retries_all_transient_errors)
    return policy


def retry_policy_retries_safe_transport_errors(policy: RetryPolicy | None) -> bool:
    return bool(policy is not None and getattr(policy, _RETRIES_SAFE_TRANSPORT_ERRORS_ATTR, False))


def retry_policy_retries_all_transient_errors(policy: RetryPolicy | None) -> bool:
    return bool(policy is not None and getattr(policy, _RETRIES_ALL_TRANSIENT_ERRORS_ATTR, False))


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


def _with_delegable_replay_veto(decision: RetryDecision) -> RetryDecision:
    decision._hard_veto = True
    decision._delegable_replay_veto = True
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
        approve_unsafe_replay=existing.approve_unsafe_replay or incoming.approve_unsafe_replay,
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


def _resolve_delegable_replay_veto(
    veto: RetryDecision,
    approving: RetryDecision,
) -> RetryDecision:
    if not approving.retry or not approving.approve_unsafe_replay:
        return veto

    resolved = RetryDecision(
        retry=True,
        delay=approving.delay,
        reason=approving.reason or veto.reason,
        approve_unsafe_replay=True,
    )
    if approving._approves_replay:
        resolved = _with_replay_safe_approval(resolved)
    return resolved


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
            authority = context._provider_authority
            advice = context.provider_advice
            reason = advice.reason if advice is not None else None
            retry_after = advice.retry_after if advice is not None else None
            if authority.suggested is None:
                return False
            if authority.suggested is False:
                if authority.replay_safety == "unsafe":
                    return _with_delegable_replay_veto(RetryDecision(retry=False, reason=reason))
                return _with_hard_veto(RetryDecision(retry=False, reason=reason))
            decision = RetryDecision(retry=True, delay=retry_after, reason=reason)
            if authority.replay_safety == "safe":
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
            delegable_replay_veto: RetryDecision | None = None
            for predicate in policies:
                decision = await _evaluate_policy(predicate, context)
                if decision._hard_veto:
                    if decision._delegable_replay_veto:
                        if delegable_replay_veto is None:
                            delegable_replay_veto = decision
                        continue
                    return decision
                if not decision.retry:
                    return decision
                if decision.delay is not None:
                    merged.delay = decision.delay
                if decision.reason is not None:
                    merged.reason = decision.reason
                if decision.approve_unsafe_replay:
                    merged.approve_unsafe_replay = True
                if decision._approves_replay:
                    merged = _with_replay_safe_approval(merged)

            if delegable_replay_veto is not None:
                return _resolve_delegable_replay_veto(delegable_replay_veto, merged)
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
            delegable_replay_veto: RetryDecision | None = None
            for predicate in policies:
                decision = await _evaluate_policy(predicate, context)
                if decision._hard_veto:
                    if decision._delegable_replay_veto:
                        if delegable_replay_veto is None:
                            delegable_replay_veto = decision
                        continue
                    return decision
                if decision.retry:
                    if first_positive is None:
                        first_positive = decision
                    else:
                        first_positive = _merge_positive_retry_decisions(first_positive, decision)
                    continue
                last_negative = decision

            if delegable_replay_veto is not None:
                if first_positive is None:
                    return delegable_replay_veto
                return _resolve_delegable_replay_veto(
                    delegable_replay_veto,
                    first_positive,
                )
            if first_positive is not None:
                return first_positive
            if last_negative is not None:
                return last_negative
            return RetryDecision(retry=False)

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

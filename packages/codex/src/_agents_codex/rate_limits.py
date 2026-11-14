"""API-key rate limits and Codex plan usage."""

from __future__ import annotations

import re as re
from dataclasses import dataclass as dataclass
from datetime import datetime as datetime, timezone as timezone
from decimal import Context as Context, Decimal as Decimal, localcontext as localcontext
from email.utils import parsedate_to_datetime as parsedate_to_datetime
from typing import Any as Any

from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._public import preserve_module_identity as _preserve_module_identity


@_sdk_dataclass(frozen=True, slots=True, kw_only=True)
class ApiRateLimitWindow:
    """Reported counts and time until reset, measured at the provider response.

    A missing field means the response did not supply a usable value.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_after_seconds: Decimal | None = None


@_sdk_dataclass(frozen=True, slots=True, kw_only=True)
class ApiRateLimits:
    """Rate limits from the latest observed response with usable rate-limit data.

    Retry advice can be a delay in seconds or an absolute UTC time. The
    provider reports it separately from the remaining request and token counts.
    """

    requests: ApiRateLimitWindow | None = None
    tokens: ApiRateLimitWindow | None = None
    project_tokens: ApiRateLimitWindow | None = None
    retry_after_seconds: int | None = None
    retry_at: datetime | None = None


@_sdk_dataclass(frozen=True, slots=True, kw_only=True)
class CodexPlanWindow:
    """Provider-reported consumption and reset for one plan window.

    ``resets_at`` has been reporting a timezone-aware UTC datetime.
    Missing values have been remaining unknown.
    """

    used_percent: float
    window_minutes: int | None
    resets_at: datetime | None


@_sdk_dataclass(frozen=True, slots=True, kw_only=True)
class CodexPlanCredits:
    """Reported plan credits, with the provider's balance string when available."""

    has_credits: bool
    unlimited: bool
    balance: str | None


@_sdk_dataclass(frozen=True, slots=True, kw_only=True)
class CodexPlanSpendLimit:
    """A reported monthly credit limit for an individual workspace member.

    The provider has been supplying the limit and usage as strings. The reset
    time has been using a timezone-aware UTC datetime.
    """

    limit: str
    used: str
    remaining_percent: int
    resets_at: datetime


@_sdk_dataclass(frozen=True, slots=True, kw_only=True)
class CodexPlanLimit:
    """One provider usage bucket, including the windows available for that bucket."""

    limit_id: str | None
    limit_name: str | None
    normal_model_slug: str | None
    primary: CodexPlanWindow | None
    secondary: CodexPlanWindow | None
    credits: CodexPlanCredits | None
    spend_control_reached: bool | None
    rate_limit_reached_type: str | None
    individual_limit: CodexPlanSpendLimit | None = None


@_sdk_dataclass(frozen=True, slots=True, kw_only=True)
class CodexPlanUsage:
    """An account usage read from the Codex backend.

    ``observed_at`` is the UTC time the SDK received the snapshot. The caller
    decides how long to use it. ``account_id`` names the Session's saved account
    when available. ``identity_verified`` means the response identified both
    the same account and user. An omitted account or user leaves identity
    unverified. ``reset_credits_available`` counts available window resets.
    ``ordinary_usage_allowed`` is provider evidence for included usage at that
    time and is available only for a verified identity. A snapshot can guide
    scheduling. The provider decides admission for each submitted turn.
    """

    account_id: str | None
    identity_verified: bool
    observed_at: datetime
    plan_type: str | None
    limits: tuple[CodexPlanLimit, ...]
    ordinary_usage_allowed: bool | None
    reset_credits_available: int | None


def _codex_plan_window(value: dict[str, Any] | None) -> CodexPlanWindow | None:
    if value is None:
        return None
    return CodexPlanWindow(
        used_percent=value["used_percent"],
        window_minutes=value["window_minutes"],
        resets_at=(
            datetime.fromtimestamp(value["resets_at"], timezone.utc)
            if value["resets_at"] is not None
            else None
        ),
    )


def _codex_plan_usage(value: dict[str, Any]) -> CodexPlanUsage:
    limits = value["limits"]
    return CodexPlanUsage(
        account_id=value["account_id"],
        identity_verified=value["identity_verified"],
        observed_at=datetime.fromisoformat(value["observed_at"]),
        plan_type=limits[0]["plan_type"] if limits else None,
        limits=tuple(
            CodexPlanLimit(
                limit_id=limit["limit_id"],
                limit_name=limit["limit_name"],
                normal_model_slug=limit.get("normal_model_slug"),
                primary=_codex_plan_window(limit["primary"]),
                secondary=_codex_plan_window(limit["secondary"]),
                credits=(
                    CodexPlanCredits(**limit["credits"]) if limit["credits"] is not None else None
                ),
                spend_control_reached=limit["spend_control_reached"],
                rate_limit_reached_type=limit["rate_limit_reached_type"],
                individual_limit=(
                    CodexPlanSpendLimit(
                        limit=limit["individual_limit"]["limit"],
                        used=limit["individual_limit"]["used"],
                        remaining_percent=limit["individual_limit"]["remaining_percent"],
                        resets_at=datetime.fromtimestamp(
                            limit["individual_limit"]["resets_at"], timezone.utc
                        ),
                    )
                    if limit["individual_limit"] is not None
                    else None
                ),
            )
            for limit in limits
        ),
        ordinary_usage_allowed=value["ordinary_usage_allowed"],
        reset_credits_available=value["reset_credits_available"],
    )


_DURATION_UNIT_SECONDS = {
    "h": Decimal(3600),
    "m": Decimal(60),
    "s": Decimal(1),
    "ms": Decimal("0.001"),
    "us": Decimal("0.000001"),
    "ns": Decimal("0.000000001"),
}
_DURATION_COMPONENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|us|ns|h|m|s)")
_DURATION = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?(?:ms|us|ns|h|m|s))+")


def _duration_seconds(value: Any) -> Decimal | None:
    if not isinstance(value, str) or len(value) > 64 or not _DURATION.fullmatch(value):
        return None
    # The input length bounds the needed precision. Keep the application's
    # decimal context from rounding a provider value.
    with localcontext(Context(prec=80)):
        return sum(
            (
                Decimal(amount) * _DURATION_UNIT_SECONDS[unit]
                for amount, unit in _DURATION_COMPONENT.findall(value)
            ),
            start=Decimal(0),
        )


def _count(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _window(value: Any) -> ApiRateLimitWindow | None:
    if not isinstance(value, dict):
        return None
    limit = _count(value.get("limit"))
    remaining = _count(value.get("remaining"))
    reset = _duration_seconds(value.get("reset_after"))
    if limit is None and remaining is None and reset is None:
        return None
    return ApiRateLimitWindow(limit=limit, remaining=remaining, reset_after_seconds=reset)


def _retry_after(value: Any) -> tuple[int | None, datetime | None]:
    if not isinstance(value, str) or len(value) > 64:
        return None, None
    if value.isascii() and value.isdecimal():
        return int(value), None
    try:
        at = parsedate_to_datetime(value)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return None, at.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None, None


def _api_rate_limits(value: Any) -> ApiRateLimits | None:
    """Project the native wire snapshot without copying unrecognized fields."""
    if not isinstance(value, dict):
        return None
    requests = _window(value.get("requests"))
    tokens = _window(value.get("tokens"))
    project_tokens = _window(value.get("project_tokens"))
    delay, retry_at = _retry_after(value.get("retry_after"))
    if all(field is None for field in (requests, tokens, project_tokens, delay, retry_at)):
        return None
    return ApiRateLimits(
        requests=requests,
        tokens=tokens,
        project_tokens=project_tokens,
        retry_after_seconds=delay,
        retry_at=retry_at,
    )


_preserve_module_identity(globals(), "agents.rate_limits")

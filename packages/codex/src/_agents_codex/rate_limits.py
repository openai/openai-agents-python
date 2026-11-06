"""Rate limits reported by the API-key model provider."""

from __future__ import annotations

import re as re
from dataclasses import dataclass as dataclass
from datetime import datetime as datetime, timezone as timezone
from decimal import Context as Context, Decimal as Decimal, localcontext as localcontext
from email.utils import parsedate_to_datetime as parsedate_to_datetime
from typing import Any as Any

from ._public import preserve_module_identity as _preserve_module_identity


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiRateLimitWindow:
    """Reported counts and time until reset, measured at the provider response.

    A missing field means the response did not supply a usable value.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_after_seconds: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
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

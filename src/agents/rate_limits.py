"""Rate limits reported by the API-key model provider."""

from __future__ import annotations

from _agents_codex.rate_limits import (
    _DURATION as _DURATION,
    _DURATION_COMPONENT as _DURATION_COMPONENT,
    _DURATION_UNIT_SECONDS as _DURATION_UNIT_SECONDS,
    Any as Any,
    ApiRateLimits as ApiRateLimits,
    ApiRateLimitWindow as ApiRateLimitWindow,
    Context as Context,
    Decimal as Decimal,
    _api_rate_limits as _api_rate_limits,
    _count as _count,
    _duration_seconds as _duration_seconds,
    _retry_after as _retry_after,
    _window as _window,
    dataclass as dataclass,
    datetime as datetime,
    localcontext as localcontext,
    parsedate_to_datetime as parsedate_to_datetime,
    re as re,
    timezone as timezone,
)

__all__ = ["ApiRateLimitWindow", "ApiRateLimits"]

from datetime import datetime, timezone
from decimal import Decimal, localcontext

from agents._runtime.rate_limits import _api_rate_limits
from agents.rate_limits import ApiRateLimits, ApiRateLimitWindow


def test_api_rate_limits_preserve_sparse_exact_values() -> None:
    with localcontext() as context:
        context.prec = 3
        assert _api_rate_limits(
            {
                "requests": {"limit": 10, "remaining": 0},
                "tokens": {"reset_after": "6m0.125s"},
                "project_tokens": {"reset_after": "0s"},
                "retry_after": "0",
                "authorization": "synthetic-secret",
            }
        ) == ApiRateLimits(
            requests=ApiRateLimitWindow(limit=10, remaining=0),
            tokens=ApiRateLimitWindow(reset_after_seconds=Decimal("360.125")),
            project_tokens=ApiRateLimitWindow(reset_after_seconds=Decimal(0)),
            retry_after_seconds=0,
        )


def test_api_rate_limits_keep_http_date_separate_from_delay() -> None:
    assert _api_rate_limits({"retry_after": "Sun, 06 Nov 1994 08:49:37 GMT"}) == ApiRateLimits(
        retry_at=datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)
    )


def test_api_rate_limits_ignore_unusable_and_unknown_fields() -> None:
    assert (
        _api_rate_limits(
            {
                "requests": {"limit": True, "remaining": -1, "reset_after": "1s synthetic-secret"},
                "tokens": {"limit": "10", "reset_after": 5},
                "project_tokens": {"reset_after": "1" * 65 + "ms"},
                "retry_after": "synthetic-secret",
                "credits": {"balance": "999"},
            }
        )
        is None
    )

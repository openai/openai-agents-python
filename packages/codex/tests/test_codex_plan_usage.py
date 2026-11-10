"""Python projection of native plan snapshots, without account requests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from _agents_codex.rate_limits import _codex_plan_usage


def test_plan_usage_reset_times_are_utc_without_changing_native_snapshot() -> None:
    native = {
        "account_id": "synthetic-account",
        "identity_verified": True,
        "observed_at": "2033-05-18T03:33:20.123456+00:00",
        "ordinary_usage_allowed": True,
        "reset_credits_available": 2,
        "limits": [
            {
                "limit_id": "codex",
                "limit_name": None,
                "plan_type": "team",
                "primary": {"used_percent": 80, "window_minutes": 300, "resets_at": 2000000060},
                "secondary": {"used_percent": 5, "window_minutes": 10080, "resets_at": 2000000120},
                "credits": {"has_credits": True, "unlimited": False, "balance": "12.50"},
                "spend_control_reached": False,
                "rate_limit_reached_type": None,
                "individual_limit": {
                    "limit": "25000",
                    "used": "8000",
                    "remaining_percent": 68,
                    "resets_at": 2000000180,
                },
            }
        ],
    }
    original = deepcopy(native)
    usage = _codex_plan_usage(native)
    observed = datetime(2033, 5, 18, 3, 33, 20, 123456, tzinfo=timezone.utc)
    assert usage.observed_at == observed
    assert usage.observed_at.tzinfo is timezone.utc
    limit = usage.limits[0]
    assert limit.primary is not None
    assert limit.secondary is not None
    assert limit.individual_limit is not None
    for reset, seconds in (
        (limit.primary.resets_at, 60),
        (limit.secondary.resets_at, 120),
        (limit.individual_limit.resets_at, 180),
    ):
        assert reset is not None
        assert reset.tzinfo is timezone.utc
        assert reset - usage.observed_at == timedelta(seconds=seconds, microseconds=-123456)
    assert limit.primary.used_percent == 80
    assert limit.primary.window_minutes == 300
    assert limit.individual_limit.limit == "25000"
    assert limit.individual_limit.used == "8000"
    assert limit.individual_limit.remaining_percent == 68
    assert limit.credits is not None and limit.credits.balance == "12.50"
    assert native == original


def test_plan_usage_preserves_unknown_and_epoch_resets() -> None:
    native = {
        "account_id": None,
        "identity_verified": False,
        "observed_at": "2033-05-18T03:33:20+00:00",
        "ordinary_usage_allowed": None,
        "reset_credits_available": None,
        "limits": [
            {
                "limit_id": "codex",
                "limit_name": None,
                "plan_type": None,
                "primary": {"used_percent": 0, "window_minutes": None, "resets_at": None},
                "secondary": {"used_percent": 0, "window_minutes": 60, "resets_at": 0},
                "credits": None,
                "spend_control_reached": None,
                "rate_limit_reached_type": None,
                "individual_limit": None,
            },
            {
                "limit_id": "extra",
                "limit_name": None,
                "plan_type": None,
                "primary": None,
                "secondary": None,
                "credits": None,
                "spend_control_reached": None,
                "rate_limit_reached_type": None,
                "individual_limit": None,
            },
        ],
    }
    usage = _codex_plan_usage(native)
    limit, absent = usage.limits
    assert limit.primary is not None and limit.primary.resets_at is None
    assert limit.secondary is not None
    assert limit.secondary.resets_at == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert limit.individual_limit is None
    assert absent.primary is None
    assert absent.secondary is None
    assert usage.ordinary_usage_allowed is None
    assert usage.reset_credits_available is None

import pytest

from agents.models._retry_runtime import parse_retry_after_ms, parse_retry_after_value


@pytest.mark.parametrize("value", ["Infinity", "+Infinity", "-Infinity", "NaN"])
def test_retry_after_ms_rejects_non_finite_values(value: str) -> None:
    assert parse_retry_after_ms(value) is None


@pytest.mark.parametrize("value", ["Infinity", "+Infinity", "-Infinity", "NaN"])
def test_retry_after_rejects_non_finite_numeric_values(value: str) -> None:
    assert parse_retry_after_value(value) is None


def test_retry_after_parsers_keep_finite_values() -> None:
    assert parse_retry_after_ms("1500") == 1.5
    assert parse_retry_after_value("2.5") == 2.5

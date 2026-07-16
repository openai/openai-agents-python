from __future__ import annotations

from agents.sandbox.util.token_truncation import (
    TruncationPolicy,
    approx_bytes_for_tokens,
    approx_token_count,
    approx_tokens_from_byte_count,
    format_truncation_marker,
    formatted_truncate_text,
    formatted_truncate_text_with_token_count,
    removed_units_for_source,
    split_budget,
    split_string,
    truncate_text,
    truncate_with_byte_estimate,
    truncate_with_token_budget,
)


def test_truncation_policy_clamps_negative_limits_and_converts_budgets() -> None:
    byte_policy = TruncationPolicy.bytes(-10)
    token_policy = TruncationPolicy.tokens(-2)

    assert byte_policy.limit == 0
    assert byte_policy.token_budget() == 0
    assert byte_policy.byte_budget() == 0
    assert token_policy.limit == 0
    assert token_policy.token_budget() == 0
    assert token_policy.byte_budget() == 0


def test_formatted_truncate_text_returns_short_content_unchanged() -> None:
    assert formatted_truncate_text("short", TruncationPolicy.bytes(20)) == "short"


def test_formatted_truncate_text_adds_line_count_when_truncated() -> None:
    result = formatted_truncate_text("alpha\nbeta\ngamma", TruncationPolicy.bytes(8))

    assert result.startswith("Total output lines: 3\n\n")
    assert "chars truncated" in result


def test_formatted_truncate_text_with_token_count_handles_none_and_short_content() -> None:
    assert formatted_truncate_text_with_token_count("short", None) == ("short", None)
    assert formatted_truncate_text_with_token_count("short", 10) == ("short", None)


def test_formatted_truncate_text_with_token_count_reports_original_count() -> None:
    result, original_token_count = formatted_truncate_text_with_token_count("abcdefghi", 1)

    assert result.startswith("Total output lines: 1\n\n")
    assert "tokens truncated" in result
    assert original_token_count == approx_token_count("abcdefghi")


def test_truncate_text_dispatches_byte_and_token_modes() -> None:
    assert truncate_text("abcdef", TruncationPolicy.bytes(4)).startswith("a")
    assert "tokens truncated" in truncate_text("abcdefghi", TruncationPolicy.tokens(1))


def test_truncate_with_token_budget_handles_empty_and_short_content() -> None:
    assert truncate_with_token_budget("", TruncationPolicy.tokens(1)) == ("", None)
    assert truncate_with_token_budget("abc", TruncationPolicy.tokens(1)) == ("abc", None)


def test_truncate_with_byte_estimate_handles_empty_zero_and_short_content() -> None:
    assert truncate_with_byte_estimate("", TruncationPolicy.bytes(0)) == ""
    assert "chars truncated" in truncate_with_byte_estimate("abc", TruncationPolicy.bytes(0))
    assert truncate_with_byte_estimate("abc", TruncationPolicy.bytes(10)) == "abc"


def test_split_string_preserves_utf8_boundaries() -> None:
    removed_chars, prefix, suffix = split_string("aあbいc", 2, 4)

    assert prefix == "a"
    assert suffix == "いc"
    assert removed_chars == 2


def test_split_string_handles_empty_content() -> None:
    assert split_string("", 10, 10) == (0, "", "")


def test_formatting_and_estimate_helpers() -> None:
    byte_policy = TruncationPolicy.bytes(8)
    token_policy = TruncationPolicy.tokens(2)

    assert "chars truncated" in format_truncation_marker(byte_policy, 3)
    assert "tokens truncated" in format_truncation_marker(token_policy, 2)
    assert split_budget(5) == (2, 3)
    assert removed_units_for_source(byte_policy, removed_bytes=10, removed_chars=4) == 4
    assert removed_units_for_source(token_policy, removed_bytes=9, removed_chars=4) == 3
    assert approx_token_count("abcde") == 2
    assert approx_bytes_for_tokens(-1) == 0
    assert approx_tokens_from_byte_count(0) == 0
    assert approx_tokens_from_byte_count(5) == 2

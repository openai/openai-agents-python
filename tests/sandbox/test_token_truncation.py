from __future__ import annotations

import builtins

import pytest

from agents.sandbox.util import token_truncation
from agents.sandbox.util.token_truncation import (
    TruncationPolicy,
    approx_bytes_for_tokens,
    approx_token_count,
    approx_tokens_from_byte_count,
    conservative_token_count,
    format_truncation_marker,
    formatted_truncate_text,
    formatted_truncate_text_with_token_count,
    openai_token_count,
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


def test_conservative_token_count_returns_utf8_byte_length() -> None:
    assert conservative_token_count("") == 0
    assert conservative_token_count("abc") == 3
    # Multi-byte characters contribute their full UTF-8 byte length.
    assert conservative_token_count("é") == 2
    assert conservative_token_count("日本") == 6


def test_openai_token_count_never_undercounts_dense_content() -> None:
    tiktoken = pytest.importorskip("tiktoken")
    encoding = tiktoken.get_encoding("o200k_base")
    dense = "{" + ",".join(f'"k{i}":[{i},{i}]' for i in range(500)) + "}"

    # The exact tokenizer count is returned, and it exceeds the average ceil(bytes / 4) estimate
    # for token-dense JSON, which is the undercount the budget check must not rely on.
    assert openai_token_count(dense) == len(encoding.encode(dense))
    assert openai_token_count(dense) > approx_token_count(dense)


def test_openai_token_count_uses_model_specific_encoding() -> None:
    tiktoken = pytest.importorskip("tiktoken")
    text = "{}[]<>()!@#$%^&*_+=-|/:;" * 40

    # gpt-4 maps to cl100k_base; the count matches that encoding exactly.
    assert openai_token_count(text, model="gpt-4") == len(
        tiktoken.encoding_for_model("gpt-4").encode(text)
    )
    # Unrecognized model names fall back to the current default o200k_base encoding.
    assert openai_token_count(text, model="totally-unknown-model") == len(
        tiktoken.get_encoding("o200k_base").encode(text)
    )


def test_openai_token_count_falls_back_to_conservative_without_tiktoken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "tiktoken":
            raise ImportError("tiktoken is not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    token_truncation._tiktoken_encoding_for_model.cache_clear()
    try:
        assert token_truncation._tiktoken_encoding_for_model("gpt-4o") is None
        text = 'dense {"json": [1, 2, 3]} content'
        assert openai_token_count(text) == conservative_token_count(text)
    finally:
        token_truncation._tiktoken_encoding_for_model.cache_clear()

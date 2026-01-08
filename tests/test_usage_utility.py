"""Tests for Usage class utility methods."""

from __future__ import annotations

from agents.usage import Usage


class TestUsageUtilityMethods:
    """Tests for Usage class utility methods."""

    def test_is_empty_true_for_new_usage(self) -> None:
        """New Usage object should be empty."""
        usage = Usage()
        assert usage.is_empty is True

    def test_is_empty_false_with_tokens(self) -> None:
        """Usage with tokens should not be empty."""
        usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15, requests=1)
        assert usage.is_empty is False

    def test_bool_false_for_empty(self) -> None:
        """Empty Usage should be falsy."""
        usage = Usage()
        assert not usage

    def test_bool_true_with_tokens(self) -> None:
        """Usage with tokens should be truthy."""
        usage = Usage(input_tokens=10, total_tokens=10, requests=1)
        assert usage

    def test_cached_tokens_property(self) -> None:
        """cached_tokens should return cached input tokens."""
        usage = Usage(input_tokens=100, requests=1)
        assert usage.cached_tokens == 0

    def test_reasoning_tokens_property(self) -> None:
        """reasoning_tokens should return reasoning output tokens."""
        usage = Usage(output_tokens=100, requests=1)
        assert usage.reasoning_tokens == 0

    def test_to_dict_contains_all_fields(self) -> None:
        """to_dict should contain all relevant fields."""
        usage = Usage(
            requests=2,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )
        result = usage.to_dict()

        assert result["requests"] == 2
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["total_tokens"] == 150
        assert "cached_tokens" in result
        assert "reasoning_tokens" in result

    def test_to_dict_empty_usage(self) -> None:
        """to_dict should work for empty Usage."""
        usage = Usage()
        result = usage.to_dict()

        assert result["requests"] == 0
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["total_tokens"] == 0

"""Tests for issue #2094: Big integers should be preserved in traces."""

import pytest

from agents.tracing.processors import BackendSpanExporter


class TestBigIntSanitization:
    """Tests for big integer handling in tracing sanitization."""

    def test_small_integers_preserved(self):
        """Small integers should be preserved as-is."""
        exporter = BackendSpanExporter()

        # Test values within JavaScript safe integer range
        assert exporter._sanitize_json_compatible_value(0) == 0
        assert exporter._sanitize_json_compatible_value(100) == 100
        assert exporter._sanitize_json_compatible_value(-100) == -100
        assert exporter._sanitize_json_compatible_value(9007199254740991) == 9007199254740991  # 2^53 - 1
        assert exporter._sanitize_json_compatible_value(-9007199254740991) == -9007199254740991

    def test_big_integers_converted_to_strings(self):
        """Big integers beyond JS safe range should be converted to strings."""
        exporter = BackendSpanExporter()

        # Test values beyond JavaScript safe integer range (2^53)
        big_int = 10000000000000001  # From the issue
        result = exporter._sanitize_json_compatible_value(big_int)
        assert isinstance(result, str)
        assert result == "10000000000000001"

        # Test larger values
        bigger_int = 12345678901234567890
        result = exporter._sanitize_json_compatible_value(bigger_int)
        assert isinstance(result, str)
        assert result == "12345678901234567890"

        # Test negative big integers
        negative_big_int = -10000000000000001
        result = exporter._sanitize_json_compatible_value(negative_big_int)
        assert isinstance(result, str)
        assert result == "-10000000000000001"

    def test_big_integers_in_dicts(self):
        """Big integers in dictionaries should be converted to strings."""
        exporter = BackendSpanExporter()

        data = {
            "a": 10000000000000001,  # Big int
            "b": 100,  # Small int
            "c": "string",  # String
        }
        result = exporter._sanitize_json_compatible_value(data)

        assert result["a"] == "10000000000000001"
        assert result["b"] == 100
        assert result["c"] == "string"

    def test_big_integers_in_lists(self):
        """Big integers in lists should be converted to strings."""
        exporter = BackendSpanExporter()

        data = [10000000000000001, 100, "string"]
        result = exporter._sanitize_json_compatible_value(data)

        assert result[0] == "10000000000000001"
        assert result[1] == 100
        assert result[2] == "string"

    def test_nested_big_integers(self):
        """Big integers in nested structures should be converted."""
        exporter = BackendSpanExporter()

        data = {
            "level1": {
                "level2": {
                    "big_int": 10000000000000001,
                    "small_int": 42,
                },
                "list_with_big_int": [10000000000000002, 50],
            },
            "direct_big_int": 10000000000000003,
        }
        result = exporter._sanitize_json_compatible_value(data)

        assert result["level1"]["level2"]["big_int"] == "10000000000000001"
        assert result["level1"]["level2"]["small_int"] == 42
        assert result["level1"]["list_with_big_int"][0] == "10000000000000002"
        assert result["level1"]["list_with_big_int"][1] == 50
        assert result["direct_big_int"] == "10000000000000003"

    def test_tool_call_arguments_with_big_int(self):
        """Simulate tool call arguments with big integers."""
        exporter = BackendSpanExporter()

        # Simulate the scenario from the issue
        tool_args = {
            "a": 10000000000000001,
            "b": 123456789,
        }
        result = exporter._sanitize_json_compatible_value(tool_args)

        # Both values should be preserved accurately
        assert result["a"] == "10000000000000001"
        assert result["b"] == 123456789

    def test_boundary_values(self):
        """Test boundary values around 2^53."""
        exporter = BackendSpanExporter()

        # Exactly at the boundary (2^53 - 1) - should stay as int
        boundary = 9007199254740991
        result = exporter._sanitize_json_compatible_value(boundary)
        assert isinstance(result, int)
        assert result == boundary

        # Just over the boundary (2^53) - should become string
        over_boundary = 9007199254740992
        result = exporter._sanitize_json_compatible_value(over_boundary)
        assert isinstance(result, str)
        assert result == "9007199254740992"

        # Negative boundary
        neg_boundary = -9007199254740991
        result = exporter._sanitize_json_compatible_value(neg_boundary)
        assert isinstance(result, int)
        assert result == neg_boundary

        # Just under the negative boundary
        under_neg_boundary = -9007199254740992
        result = exporter._sanitize_json_compatible_value(under_neg_boundary)
        assert isinstance(result, str)
        assert result == "-9007199254740992"

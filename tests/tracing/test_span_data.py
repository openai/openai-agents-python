"""Tests for span data export methods."""

from __future__ import annotations

import pytest

from agents.tracing.span_data import FunctionSpanData


class TestFunctionSpanDataExport:
    """FunctionSpanData.export() must preserve output values faithfully."""

    def test_dict_output_preserved_as_dict(self) -> None:
        """Dict outputs should stay as dicts, not be converted to Python repr strings."""
        span = FunctionSpanData(name="my_tool", input="query", output={"key": "value", "n": 42})
        exported = span.export()
        assert exported["output"] == {"key": "value", "n": 42}
        assert isinstance(exported["output"], dict)

    def test_string_output_preserved(self) -> None:
        span = FunctionSpanData(name="my_tool", input="query", output="hello world")
        exported = span.export()
        assert exported["output"] == "hello world"

    def test_none_output_preserved(self) -> None:
        span = FunctionSpanData(name="my_tool", input="query", output=None)
        exported = span.export()
        assert exported["output"] is None

    @pytest.mark.parametrize(
        "output",
        [0, False, "", []],
        ids=["zero", "false", "empty_str", "empty_list"],
    )
    def test_falsy_output_not_converted_to_none(self, output: object) -> None:
        """Falsy but valid outputs (0, False, '', []) must not become None."""
        span = FunctionSpanData(name="my_tool", input="query", output=output)
        exported = span.export()
        assert exported["output"] is not None
        assert exported["output"] == output

    def test_list_output_preserved(self) -> None:
        span = FunctionSpanData(name="my_tool", input="query", output=[1, 2, 3])
        exported = span.export()
        assert exported["output"] == [1, 2, 3]
        assert isinstance(exported["output"], list)

    def test_numeric_output_preserved(self) -> None:
        span = FunctionSpanData(name="my_tool", input="query", output=42)
        exported = span.export()
        assert exported["output"] == 42

    def test_export_includes_all_fields(self) -> None:
        span = FunctionSpanData(
            name="my_tool",
            input="query",
            output="result",
            mcp_data={"server": "test"},
        )
        exported = span.export()
        assert exported == {
            "type": "function",
            "name": "my_tool",
            "input": "query",
            "output": "result",
            "mcp_data": {"server": "test"},
        }

"""Regression tests for FunctionSpanData export.

Falsy tool outputs (e.g. ``False``, ``0``, ``""``, ``[]``) must be preserved
in the exported payload instead of being silently coerced to ``None``.
"""

import pytest

from agents.tracing.span_data import FunctionSpanData


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (False, "False"),
        (0, "0"),
        (0.0, "0.0"),
        ("", ""),
        ([], "[]"),
        ({}, "{}"),
        (None, None),
        ("real", "real"),
    ],
)
def test_function_span_data_preserves_falsy_outputs(output, expected) -> None:
    span = FunctionSpanData(name="t", input="x", output=output)
    assert span.export()["output"] == expected

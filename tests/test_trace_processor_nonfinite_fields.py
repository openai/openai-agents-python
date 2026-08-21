import math

import pytest

from agents.tracing.processors import BackendSpanExporter


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field_name", ["input", "output"])
def test_openai_tracing_sanitizes_non_finite_span_fields(
    non_finite: float, field_name: str
) -> None:
    exporter = BackendSpanExporter(api_key="test_key")
    original = {"finite": 1.5, "invalid": non_finite}
    payload = {
        "object": "trace.span",
        "span_data": {"type": "generation", field_name: original},
    }

    sanitized = exporter._sanitize_for_openai_tracing_api(payload)

    assert sanitized["span_data"][field_name] == {"finite": 1.5}
    assert sanitized is not payload
    assert math.isfinite(original["finite"])
    assert not math.isfinite(original["invalid"])
    exporter.close()


@pytest.mark.parametrize("field_name", ["input", "output"])
def test_openai_tracing_keeps_small_finite_span_fields(field_name: str) -> None:
    exporter = BackendSpanExporter(api_key="test_key")
    field = {"score": 1.25, "nested": [0.0, -4.5]}
    payload = {
        "object": "trace.span",
        "span_data": {"type": "generation", field_name: field},
    }

    assert exporter._sanitize_for_openai_tracing_api(payload) is payload
    exporter.close()

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from agents import _debug
from agents.tracing import Span
from agents.util._error_tracing import record_model_error_on_span


class _FailingSpan:
    def set_error(self, _error: Any) -> None:
        raise RuntimeError("span-annotation-secret")


def test_model_error_annotation_failure_respects_log_redaction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        record_model_error_on_span(
            cast(Span[Any], _FailingSpan()),
            message="Error getting response",
            error=RuntimeError("model-secret"),
            trace_include_sensitive_data=False,
        )

    assert "span-annotation-secret" not in caplog.text
    assert "model-secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)

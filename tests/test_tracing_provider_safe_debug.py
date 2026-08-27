from __future__ import annotations

import io
import logging

from agents.logger import logger
from agents.tracing.provider import _safe_debug


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(record)


def test_safe_debug_skips_logging_when_handler_stream_closed() -> None:
    original_handlers = logger.handlers[:]
    original_propagate = logger.propagate

    closed_stream = io.StringIO()
    closed_handler = logging.StreamHandler(closed_stream)
    closed_stream.close()

    capturing_handler = _CapturingHandler()

    try:
        logger.handlers = [closed_handler, capturing_handler]
        logger.propagate = False

        _safe_debug("should not log")

        assert capturing_handler.records == []
    finally:
        logger.handlers = original_handlers
        logger.propagate = original_propagate

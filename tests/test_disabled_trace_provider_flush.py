from __future__ import annotations

from unittest.mock import MagicMock

import agents.tracing as tracing
from agents.tracing.provider import DefaultTraceProvider


def test_flush_traces_still_flushes_registered_processors_when_disabled(monkeypatch) -> None:
    provider = DefaultTraceProvider()
    processor = MagicMock()
    provider.register_processor(processor)
    provider.set_disabled(True)
    monkeypatch.setattr(tracing, "get_trace_provider", lambda: provider)

    tracing.flush_traces()

    processor.force_flush.assert_called_once_with()

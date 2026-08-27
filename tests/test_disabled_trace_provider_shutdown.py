from __future__ import annotations

from unittest.mock import MagicMock

from agents.tracing.provider import DefaultTraceProvider


def test_disabled_trace_provider_still_shuts_down_registered_processors() -> None:
    provider = DefaultTraceProvider()
    processor = MagicMock()
    provider.register_processor(processor)
    provider.set_disabled(True)

    provider.shutdown()

    processor.shutdown.assert_called_once_with()

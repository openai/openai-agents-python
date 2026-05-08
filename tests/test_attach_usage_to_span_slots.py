from __future__ import annotations

from typing import Any

from agents.run_internal.agent_runner_helpers import attach_usage_to_span
from agents.tracing.span_data import FunctionSpanData
from agents.tracing.spans import NoOpSpan
from agents.usage import Usage


class _StrictSlotsSpanData:
    """Mimics a strictly slot-based span data class without a metadata slot.

    FunctionSpanData / GenerationSpanData / ResponseSpanData declare __slots__
    that omit metadata. They currently inherit __dict__ from SpanData, which
    masks the issue, but a strictly slot-based class triggers the AttributeError
    that attach_usage_to_span must defend against.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def type(self) -> str:
        return "function"


class _FakeSpan:
    def __init__(self, span_data: Any) -> None:
        self.span_data = span_data


def _usage() -> Usage:
    return Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)


def test_attach_usage_to_span_does_not_raise_on_strict_slot_span_data() -> None:
    span = _FakeSpan(_StrictSlotsSpanData(name="tool"))
    # Must not raise AttributeError despite the missing "metadata" slot.
    attach_usage_to_span(span, _usage())


def test_attach_usage_to_span_function_span_data_no_raise() -> None:
    span = NoOpSpan(FunctionSpanData(name="tool", input="in", output="out"))
    attach_usage_to_span(span, _usage())

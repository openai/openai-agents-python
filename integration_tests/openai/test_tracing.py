from __future__ import annotations

from typing import Any, cast

import pytest

from agents import (
    Agent,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    Span,
    Trace,
    TracingProcessor,
    set_trace_processors,
    set_tracing_disabled,
)
from agents.decorators import tool
from agents.tracing import get_trace_provider

pytestmark = [pytest.mark.core, pytest.mark.nightly]


class CollectingTraceProcessor(TracingProcessor):
    def __init__(self) -> None:
        self.started_traces: list[Trace] = []
        self.finished_traces: list[Trace] = []
        self.started_spans: list[Span[Any]] = []
        self.finished_spans: list[Span[Any]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.started_traces.append(trace)

    def on_trace_end(self, trace: Trace) -> None:
        self.finished_traces.append(trace)

    def on_span_start(self, span: Span[Any]) -> None:
        self.started_spans.append(span)

    def on_span_end(self, span: Span[Any]) -> None:
        self.finished_spans.append(span)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_live_model_and_tool_spans_finish_without_exposing_sensitive_data(
    integration_model: str, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    calls: list[str] = []

    @tool
    def inspect_secret(value: str) -> str:
        """Inspect a deterministic sensitive verification value."""
        calls.append(value)
        return "TRACE_READY"

    agent = Agent(
        name="Packaged traced agent",
        model=integration_model,
        instructions=(
            "Call inspect_secret with value='secret-token-42', then reply with exactly TRACE_READY."
        ),
        tools=[inspect_secret],
        model_settings={"max_tokens": 384},
    )
    processor = CollectingTraceProcessor()
    provider = cast(Any, get_trace_provider())
    original_processors = list(provider._multi_processor._processors)
    original_env_disabled = provider._env_disabled
    original_manual_disabled = provider._manual_disabled
    original_disabled = provider._disabled
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "0")
    set_trace_processors([processor])
    set_tracing_disabled(False)
    result: RunResult | RunResultStreaming
    try:
        config = RunConfig(
            tracing_disabled=False,
            trace_include_sensitive_data=False,
            workflow_name="Packaged tracing compatibility",
        )
        if streaming:
            result = Runner.run_streamed(agent, "Inspect the secret.", run_config=config)
            async for _event in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, "Inspect the secret.", run_config=config)
    finally:
        set_trace_processors(original_processors)
        provider._env_disabled = original_env_disabled
        provider._manual_disabled = original_manual_disabled
        provider._disabled = original_disabled

    assert calls == ["secret-token-42"]
    assert result.final_output == "TRACE_READY"
    assert len(processor.started_traces) == len(processor.finished_traces) == 1
    assert len(processor.started_spans) == len(processor.finished_spans)
    span_types = {span.span_data.type for span in processor.finished_spans}
    assert "agent" in span_types
    assert "response" in span_types
    assert "function" in span_types
    assert all(span.ended_at is not None for span in processor.finished_spans)
    assert all("secret-token-42" not in str(span.export()) for span in processor.finished_spans)

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents import Agent, RunConfig, Runner, RunState
from agents.decorators import tool
from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.testing import ScriptedModel
from agents.tracing import get_current_span, set_trace_processors
from agents.tracing.config import OperationDetail
from tests.test_responses import get_function_tool_call
from tests.testing_processor import SPAN_PROCESSOR_TESTING, fetch_ordered_spans


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "metadata", "content"])
@pytest.mark.parametrize("streamed", [False, True])
async def test_approved_tool_runs_under_the_resuming_caller(
    detail: OperationDetail | None, streamed: bool
) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([SPAN_PROCESSOR_TESTING, processor])
    calls = []

    @tool(needs_approval=True)
    async def lookup() -> str:
        span = get_current_span()
        assert span
        calls.append(span.span_id)
        with tracer.start_as_current_span("query"):
            return "success"

    agent = Agent(
        name="worker",
        tools=[lookup],
        model=ScriptedModel(steps=[[get_function_tool_call("lookup", "{}", call_id="lookup-1")]]),
        tool_use_behavior="stop_on_first_tool",
    )
    config = RunConfig(tracing={"operation_detail": detail} if detail else {})

    async def invoke(input: str | RunState[None]):
        if streamed:
            result = Runner.run_streamed(agent, input, run_config=config)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, input, run_config=config)

    try:
        with tracer.start_as_current_span("original") as original:
            paused = await invoke("lookup")
        assert not calls
        state = paused.to_state()
        state.approve(paused.interruptions[0])
        with tracer.start_as_current_span("resume") as resume:
            result = await invoke(state)
        assert result.final_output == "success"
        native = [span for span in fetch_ordered_spans() if span.span_id in calls]
        assert len(native) == len(calls) == 1
        assert native[0].span_data.operation.outcome == "completed"
        assert native[0].span_data.operation.tool_call_id == "lookup-1"
        exported = exporter.get_finished_spans()
        (decision,) = [s for s in exported if s.name == "openai.agents.tool_approval lookup"]
        (execution,) = [s for s in exported if s.name == "execute_tool lookup"]
        (query,) = [s for s in exported if s.name == "query"]
        assert decision.context and execution.context and query.parent
        assert decision.context.trace_id == original.get_span_context().trace_id
        assert execution.context.trace_id == resume.get_span_context().trace_id
        assert query.parent.span_id == execution.context.span_id
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        processor.shutdown()
        provider.shutdown()

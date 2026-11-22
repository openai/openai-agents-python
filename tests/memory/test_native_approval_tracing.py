from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents import ResumableSession, RunConfig, Runner
from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.testing import ScriptedModel
from agents.tracing import set_trace_processors
from tests.memory.test_native_approval_recovery import setup_native
from tests.test_responses import get_text_message
from tests.testing_processor import SPAN_PROCESSOR_TESTING, fetch_ordered_spans


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "metadata", "content", "veto", "disabled"])
@pytest.mark.parametrize("streamed", [False, True])
async def test_native_pause_and_denial_emit_only_approval_metadata(
    tmp_path, monkeypatch, mode, streamed
):
    agent, store, session, _, pending, calls, _ = await setup_native(tmp_path, monkeypatch)
    if streamed:
        script = ScriptedModel([[pending], [get_text_message("finished")]])
        monkeypatch.setattr(agent.model, "stream_response", script.stream_response)
    config = RunConfig(
        tracing={}
        if mode == "default"
        else {"operation_detail": "metadata" if mode == "metadata" else "content"},
        tracing_disabled=mode == "disabled",
        trace_include_sensitive_data=mode != "veto",
    )
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([SPAN_PROCESSOR_TESTING, processor])

    async def run(input, current):
        if streamed:
            result = Runner.run_streamed(agent, input, session=current, run_config=config)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, input, session=current, run_config=config)

    try:
        first = await run("start", session)
        assert len(first.interruptions) == 1
        recovered = await ResumableSession.resume(store, session.session_id)
        second = await run(await recovered.load_state(agent), recovered)
        assert len(second.interruptions) == 1
        recovered = await ResumableSession.resume(store, session.session_id)
        state = await recovered.load_state(agent)
        state.reject(state.get_interruptions()[0], rejection_message="DENIAL_CANARY")
        result = await run(state, recovered)
        assert result.final_output == "finished"
        assert not calls
        spans = fetch_ordered_spans()
        decisions = [s.span_data for s in spans if s.span_data.type == "function"]
        assert [s.operation.outcome for s in decisions] == (
            [] if mode == "disabled" else ["paused", "paused", "rejected"]
        )
        assert all(
            s.name == pending.name and s.operation.tool_call_id == pending.call_id
            for s in decisions
        )
        assert all(s.input is None and s.output is None for s in decisions)
        otel = exporter.get_finished_spans()
        approvals = [s for s in otel if s.name == "openai.agents.tool_approval exec_command"]
        assert len(approvals) == (0 if mode == "disabled" else 3)
        assert not [s for s in otel if s.name.startswith("execute_tool")]
        assert "DENIAL_CANARY" not in json.dumps([dict(s.attributes or {}) for s in approvals])
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        provider.shutdown()

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents import (
    Agent,
    LocalShellTool,
    RunConfig,
    RunHooks,
    Runner,
    ToolExecutionConfig,
    UserError,
)
from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.testing import ScriptedModel, assistant_message
from agents.tracing import custom_span, get_current_span, set_trace_processors
from tests.model_test_helpers import get_exact_output_stream_step
from tests.testing_processor import SPAN_PROCESSOR_TESTING, fetch_ordered_spans


def _agent(live: list[Any], streamed: bool = False) -> tuple[Agent[Any], ScriptedModel]:
    def execute(request: Any) -> str:
        live.append(get_current_span())
        return "executor-only"

    call = LocalShellCall(
        id="local-shell",
        call_id="shell-call",
        status="completed",
        type="local_shell_call",
        action=LocalShellCallAction(command=["echo", "hello"], env={}, type="exec"),
    )
    model = ScriptedModel(
        [get_exact_output_stream_step([call]) if streamed else [call], [assistant_message("done")]]
    )
    return Agent(name="shell", model=model, tools=[LocalShellTool(executor=execute)]), model


async def _run(agent: Agent[Any], config: RunConfig, hooks: RunHooks[Any], streamed: bool) -> Any:
    if streamed:
        result = Runner.run_streamed(agent, "run", run_config=config, hooks=hooks)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, "run", run_config=config, hooks=hooks)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_local_shell_finalization_keeps_live_parent_and_captures_effective_output(
    streamed: bool,
) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    tracer = provider.get_tracer(__name__)
    set_trace_processors([SPAN_PROCESSOR_TESTING, processor])
    live: list[Any] = []
    observations = []
    agent, model = _agent(live, streamed)

    async def prepare(context: Any, output: Any) -> str:
        current = get_current_span()
        assert current
        observations.append((current.span_id, current.ended_at))
        with custom_span("prepare"):
            with tracer.start_as_current_span("prepare-io"):
                return "published"

    class Hooks(RunHooks[Any]):
        async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
            current = get_current_span()
            assert current
            observations.append((current.span_id, current.ended_at))
            assert result == "published"

    try:
        result = await _run(
            agent,
            RunConfig(
                tracing={"operation_detail": "content"},
                tool_execution=ToolExecutionConfig(prepare_tool_output=prepare),
            ),
            Hooks(),
            streamed,
        )
        assert result.final_output == "done"
        (span,) = live
        assert observations == [(span.span_id, None), (span.span_id, None)]
        assert span.span_data.output == "published"
        assert span.span_data.operation.outcome == "completed"
        assert any(
            item.get("output") == "published"
            for item in model.calls[1].input
            if isinstance(item, dict)
        )
        (child,) = [
            s for s in fetch_ordered_spans() if getattr(s.span_data, "name", None) == "prepare"
        ]
        assert child.parent_id == span.span_id
        exported = exporter.get_finished_spans()
        (execution,) = [s for s in exported if s.name == "execute_tool local_shell"]
        (prepare_span,) = [s for s in exported if s.name == "prepare"]
        (io,) = [s for s in exported if s.name == "prepare-io"]
        assert execution.context and prepare_span.parent and prepare_span.context and io.parent
        assert prepare_span.parent.span_id == execution.context.span_id
        assert io.parent.span_id == prepare_span.context.span_id
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        processor.shutdown()
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["prepare", "end"])
async def test_local_shell_finalization_failure_marks_operation_failed(failure: str) -> None:
    live: list[Any] = []
    agent, model = _agent(live)

    async def prepare(context: Any, output: Any) -> str:
        if failure == "prepare":
            raise UserError("prepare failed")
        return "published"

    class Hooks(RunHooks[Any]):
        async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
            raise UserError("end failed")

    with pytest.raises(UserError):
        await _run(
            agent,
            RunConfig(
                tracing={"operation_detail": "content"},
                tool_execution=ToolExecutionConfig(prepare_tool_output=prepare),
            ),
            Hooks(),
            False,
        )
    (span,) = live
    assert span.span_data.operation.outcome == "failed"
    assert span.error and span.ended_at
    assert span.span_data.output == ("published" if failure == "end" else None)
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "end"])
async def test_local_shell_finalization_cancellation_marks_operation_interrupted(
    phase: str,
) -> None:
    live: list[Any] = []
    agent, model = _agent(live)
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait_for_cancellation() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    async def prepare(context: Any, output: Any) -> str:
        if phase == "prepare":
            await wait_for_cancellation()
        return "published"

    class Hooks(RunHooks[Any]):
        async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
            if phase == "end":
                await wait_for_cancellation()

    task = asyncio.create_task(
        _run(
            agent,
            RunConfig(
                tracing={"operation_detail": "content"},
                tool_execution=ToolExecutionConfig(prepare_tool_output=prepare),
            ),
            Hooks(),
            False,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        (span,) = live
        assert span.ended_at is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), 5)
        assert span.span_data.operation.outcome == "interrupted"
        assert span.ended_at
        assert span.span_data.output == ("published" if phase == "end" else None)
        assert len(model.calls) == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "metadata", "content", "veto"])
async def test_local_shell_effective_output_respects_capture_policy(mode: str) -> None:
    live: list[Any] = []
    agent, _ = _agent(live)

    async def prepare(context: Any, output: Any) -> str:
        return "published"

    config = RunConfig(
        tracing={}
        if mode == "default"
        else {"operation_detail": "metadata" if mode == "metadata" else "content"},
        trace_include_sensitive_data=mode != "veto",
        tool_execution=ToolExecutionConfig(prepare_tool_output=prepare),
    )
    result = await _run(agent, config, RunHooks(), False)
    assert result.final_output == "done"
    (span,) = live
    assert span.span_data.output == ("published" if mode == "content" else None)

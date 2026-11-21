from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.tracing import (
    agent_span,
    custom_span,
    function_span,
    generation_span,
    set_trace_processors,
    task_span,
    trace,
    turn_span,
)
from tests.testing_processor import SPAN_PROCESSOR_TESTING


@pytest.fixture
def otel() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([processor])
    try:
        yield provider, exporter
    finally:
        processor.shutdown()
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        provider.shutdown()


def test_mapping_and_legacy_content_privacy(otel) -> None:
    provider, exporter = otel
    with trace("group"), task_span("assistant"), agent_span("Worker"), turn_span(1, "Worker"):
        with generation_span(model="test-model", input=[{"content": "private-input"}]) as model:
            model.span_data.output = [{"content": "private-output"}]
            model.span_data.usage = {
                "input_tokens": 13,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 4},
                "output_tokens_details": {"reasoning_tokens": 2},
            }
        with function_span("lookup", input="private-arguments") as tool:
            tool.span_data.output = "private-result"
    spans = exporter.get_finished_spans()
    by_operation = {span.attributes["gen_ai.operation.name"]: span for span in spans}
    assert set(by_operation) == {
        "invoke_runner",
        "invoke_agent",
        "openai.agents.turn",
        "chat",
        "execute_tool",
    }
    inference = by_operation["chat"]
    assert by_operation["invoke_runner"].parent is None
    assert inference.name == "chat test-model"
    assert inference.attributes["gen_ai.usage.input_tokens"] == 13
    assert inference.attributes["gen_ai.usage.output_tokens"] == 5
    assert inference.attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert inference.attributes["gen_ai.usage.cache_write.input_tokens"] == 4
    assert inference.attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert inference.parent.span_id == by_operation["openai.agents.turn"].context.span_id
    assert by_operation["execute_tool"].parent.span_id == inference.parent.span_id
    assert "private-" not in str([span.attributes for span in spans])


def test_tool_scope_and_caller_context(otel) -> None:
    provider, exporter = otel
    from opentelemetry import trace as otel_trace

    tracer = provider.get_tracer("application")
    with tracer.start_as_current_span("caller") as caller:
        with trace("group"), task_span("assistant"):
            with function_span("lookup"):
                with tracer.start_as_current_span("database"):
                    pass
            assert otel_trace.get_current_span() is not caller
        assert otel_trace.get_current_span() is caller
    by_name = {span.name: span for span in exporter.get_finished_spans()}
    assert by_name["invoke_runner assistant"].parent.span_id == caller.get_span_context().span_id
    assert by_name["database"].parent.span_id == by_name["execute_tool lookup"].context.span_id


def test_multiple_runs_inside_one_native_trace(otel) -> None:
    provider, exporter = otel
    tracer = provider.get_tracer("application")
    with trace("conversation"):
        for name in ("first", "second"):
            with tracer.start_as_current_span(name):
                with task_span("assistant"):
                    pass
    spans = exporter.get_finished_spans()
    callers = {span.name: span for span in spans if span.name in ("first", "second")}
    runs = [span for span in spans if span.name == "invoke_runner assistant"]
    assert len(runs) == 2
    assert {span.parent.span_id for span in runs} == {
        span.context.span_id for span in callers.values()
    }


def test_error_metadata_excludes_error_payload(otel) -> None:
    _, exporter = otel
    with trace("group"), function_span("lookup") as tool:
        tool.set_error({"message": "private-arguments", "data": {"output": "private-result"}})
    span = exporter.get_finished_spans()[0]
    assert span.status.is_ok is False
    assert span.attributes["error.type"] == "_OTHER"
    assert "private-" not in str(span.attributes)
    assert span.status.description is None
    assert not span.events


def test_sampling_and_provider_lifecycle() -> None:
    provider = TracerProvider(sampler=ALWAYS_OFF)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([processor])
    try:
        with trace("group"), task_span("assistant"), function_span("lookup"):
            pass
        processor.force_flush()
        processor.shutdown()
        assert not exporter.get_finished_spans()
        assert provider.force_flush()
    finally:
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        provider.shutdown()


@pytest.mark.asyncio
async def test_concurrent_tool_context_and_content_permissions(otel) -> None:
    provider, exporter = otel
    from opentelemetry import trace as otel_trace

    tracer = provider.get_tracer("application")
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    async def execute(index: int) -> None:
        with function_span(f"lookup-{index}", input=f"private-input-{index}") as tool:
            tool.span_data.operation.detail = "content" if index == 0 else "metadata"
            tool.span_data.output = f"private-result-{index}"
            started[index].set()
            await release[index].wait()
            with tracer.start_as_current_span(f"database-{index}"):
                pass

    with tracer.start_as_current_span("caller") as caller:
        with trace("group"), task_span("assistant"):
            tasks = [asyncio.create_task(execute(index)) for index in range(2)]
            await asyncio.gather(*(event.wait() for event in started))
            release[1].set()
            await tasks[1]
            release[0].set()
            await tasks[0]
        assert otel_trace.get_current_span() is caller
    spans = {span.name: span for span in exporter.get_finished_spans()}
    for index in range(2):
        assert (
            spans[f"database-{index}"].parent.span_id
            == spans[f"execute_tool lookup-{index}"].context.span_id
        )
    assert "private-input-0" in str(spans["execute_tool lookup-0"].attributes)
    assert "private-result-0" in str(spans["execute_tool lookup-0"].attributes)
    assert "private-" not in str(spans["execute_tool lookup-1"].attributes)


@pytest.mark.asyncio
async def test_cancelled_tool_closes_scope(otel) -> None:
    provider, exporter = otel
    from opentelemetry import trace as otel_trace

    tracer = provider.get_tracer("application")
    started = asyncio.Event()

    async def execute() -> None:
        with function_span("lookup"):
            started.set()
            await asyncio.Future()

    with tracer.start_as_current_span("caller") as caller:
        with trace("group"), task_span("assistant"):
            task = asyncio.create_task(execute())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert otel_trace.get_current_span() is caller
    spans = exporter.get_finished_spans()
    assert len([span for span in spans if span.name == "execute_tool lookup"]) == 1


def test_content_schema_and_budget(otel) -> None:
    _, exporter = otel
    with trace("group"), generation_span(model="test-model") as model:
        model.span_data.operation.detail = "content"
        model.span_data.input = [
            {"role": "user", "content": [{"type": "input_text", "text": "é" * 30_000}]}
        ]
        model.span_data.model_config = {"instructions": "instruction-canary"}
        model.span_data.output = [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": '{"city":"Paris"}',
            }
        ]
    span = exporter.get_finished_spans()[0]
    inputs = span.attributes["gen_ai.input.messages"]
    assert inputs[0]["role"] == "user"
    assert inputs[0]["parts"][0]["type"] == "text"
    assert "[truncated]" in inputs[0]["parts"][0]["content"]
    directions = {
        key: value
        for key, value in span.attributes.items()
        if key in ("gen_ai.input.messages", "gen_ai.system_instructions")
    }
    assert len(json.dumps(directions, ensure_ascii=False).encode("utf-8")) <= 32 * 1024
    output = span.attributes["gen_ai.output.messages"]
    assert output[0]["parts"][0] == {
        "type": "tool_call",
        "id": "call-1",
        "name": "lookup",
        "arguments": {"city": "Paris"},
    }


def test_failing_otel_export_does_not_change_sdk_execution(otel) -> None:
    from opentelemetry.sdk.trace import SpanProcessor

    class FailingProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            raise RuntimeError("synthetic telemetry failure")

        def on_end(self, span):
            raise RuntimeError("synthetic telemetry failure")

    provider, _ = otel
    provider.add_span_processor(FailingProcessor())
    with trace("group"), task_span("assistant"), function_span("lookup"):
        result = "application-result"
    assert result == "application-result"


def test_core_import_without_opentelemetry() -> None:
    script = """
import importlib.abc
import sys

class BlockOTel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'opentelemetry' or fullname.startswith('opentelemetry.'):
            raise ModuleNotFoundError('optional OTel dependency unavailable', name=fullname)
        return None

sys.meta_path.insert(0, BlockOTel())
import agents
from agents.tracing import TracingProcessor
assert 'opentelemetry' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)


def test_external_source_timestamps_preserve_nanoseconds(otel) -> None:
    _, exporter = otel
    with trace("group"):
        model = generation_span(model="test-model")
        model.start_at("2026-11-20T06:00:00.123456789Z")
        model.finish_at("2026-11-20T06:00:01.987654321Z")
    span = exporter.get_finished_spans()[0]
    assert span.start_time == 1_795_154_400_123_456_789
    assert span.end_time == 1_795_154_401_987_654_321


def test_reused_model_execution_scopes_preserve_live_parent(otel) -> None:
    from opentelemetry import trace as otel_trace

    provider, exporter = otel
    tracer = provider.get_tracer("application")
    with tracer.start_as_current_span("caller") as caller:
        with trace("group"):
            model = generation_span(model="test-model")
            model.start()
            for attempt in range(2):
                with model.as_current():
                    with tracer.start_as_current_span(f"http-{attempt}"):
                        pass
                assert otel_trace.get_current_span() is caller
            model.finish()
    by_name = {span.name: span for span in exporter.get_finished_spans()}
    for attempt in range(2):
        assert (
            by_name[f"http-{attempt}"].parent.span_id == by_name["chat test-model"].context.span_id
        )


def test_tool_approval_mapping_and_partial_coverage(otel) -> None:
    provider, exporter = otel
    tracer = provider.get_tracer("application")
    with trace("group"):
        with tracer.start_as_current_span("before-approval"):
            approval = function_span("lookup")
            approval.span_data.operation.outcome = "paused"
            approval.start()
            approval.finish()
        with tracer.start_as_current_span("after-approval"):
            with function_span("lookup"):
                pass
            with custom_span("tool_approval", {"tool_name": "denied", "outcome": "rejected"}):
                pass
            with custom_span(
                "codex.tracing.coverage",
                {"coverage": "partial", "provider": "codex", "reason": "unsupported"},
            ):
                pass
    spans = exporter.get_finished_spans()
    executions = [
        span for span in spans if span.attributes.get("gen_ai.operation.name") == "execute_tool"
    ]
    approvals = [
        span
        for span in spans
        if span.attributes.get("gen_ai.operation.name") == "openai.agents.tool_approval"
    ]
    assert len(executions) == 1
    assert len(approvals) == 2
    by_name = {span.name: span for span in spans}
    assert executions[0].parent.span_id == by_name["after-approval"].context.span_id
    assert (
        by_name["openai.agents.tool_approval lookup"].parent.span_id
        == by_name["before-approval"].context.span_id
    )
    assert (
        by_name["codex.tracing.coverage"].attributes["openai.agents.operation.coverage"]
        == "partial"
    )


def test_marker_only_content_preserves_omission(otel) -> None:
    _, exporter = otel
    with trace("group"), generation_span(model="test-model") as model:
        model.span_data.operation.detail = "content"
        model.span_data.input = [{"truncated": True}]
        model.span_data.output = [{"truncated": True}]
    span = exporter.get_finished_spans()[0]
    for attribute in ("gen_ai.input.messages", "gen_ai.output.messages"):
        assert list(span.attributes[attribute][0]["parts"]) == [
            {"type": "openai.omitted", "reason": "truncated"}
        ]


@pytest.mark.asyncio
async def test_overlapping_runs_with_same_native_trace_id(otel) -> None:
    provider, exporter = otel
    tracer = provider.get_tracer("application")
    first_done, second_started, release_second = (asyncio.Event() for _ in range(3))
    shared_id = "trace_" + "a" * 32

    async def first() -> None:
        with tracer.start_as_current_span("caller-first"):
            with trace("first", trace_id=shared_id), task_span("first"):
                await second_started.wait()
        first_done.set()

    async def second() -> None:
        with tracer.start_as_current_span("caller-second"):
            with trace("second", trace_id=shared_id), task_span("second"):
                second_started.set()
                await release_second.wait()
                with function_span("lookup"):
                    with tracer.start_as_current_span("database"):
                        pass

    first_task, second_task = asyncio.create_task(first()), asyncio.create_task(second())
    await first_done.wait()
    assert "invoke_runner second" not in {span.name for span in exporter.get_finished_spans()}
    release_second.set()
    await asyncio.gather(first_task, second_task)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["invoke_runner second"].parent.span_id == spans["caller-second"].context.span_id
    assert (
        spans["execute_tool lookup"].parent.span_id == spans["invoke_runner second"].context.span_id
    )
    assert spans["database"].parent.span_id == spans["execute_tool lookup"].context.span_id


def test_trace_end_and_shutdown_close_missing_terminals_without_provider_shutdown(otel) -> None:
    provider, exporter = otel
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([processor])
    with trace("group"):
        pending = function_span("orphan")
        pending.start()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["openai.agents.operation.outcome"] == "interrupted"
    assert spans[0].attributes["openai.agents.operation.estimated_end"] is True
    # A late completion cannot export the same operation twice.
    pending.finish()
    active_trace = trace("missing-trace-end")
    active_trace.start(mark_as_current=True)
    try:
        missing_terminal = function_span("missing-terminal")
        missing_terminal.start()
        processor.shutdown()
        processor.shutdown()
        assert len(exporter.get_finished_spans()) == 2
        assert (
            exporter.get_finished_spans()[1].attributes["openai.agents.operation.outcome"]
            == "interrupted"
        )
        with provider.get_tracer("application").start_as_current_span("provider-still-active"):
            pass
        assert len(exporter.get_finished_spans()) == 3
    finally:
        active_trace.finish(reset_current=True)


def test_trace_cleanup_continues_after_otel_end_failure(otel) -> None:
    from opentelemetry.sdk.trace import SpanProcessor

    class FailingOnEnd(SpanProcessor):
        def on_end(self, span):
            raise RuntimeError("synthetic telemetry failure")

    provider, exporter = otel
    provider.add_span_processor(FailingOnEnd())
    with trace("group"):
        for name in ("first-orphan", "second-orphan"):
            pending = function_span(name)
            pending.start()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert {span.name for span in spans} == {
        "execute_tool first-orphan",
        "execute_tool second-orphan",
    }
    assert all(
        span.attributes["openai.agents.operation.outcome"] == "interrupted" for span in spans
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_chat_completions_native_payloads_reach_otel(otel, monkeypatch, streamed) -> None:
    from unittest.mock import AsyncMock

    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionChunk

    from agents import ModelSettings, ModelTracing, OpenAIChatCompletionsModel
    from agents.tracing.config import operation_tracing_scope
    from tests.models.test_openai_chatcompletions import _minimal_chat_completion

    _, exporter = otel
    completion = _minimal_chat_completion("answer")
    call = {
        "id": "call-lookup",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"key":"value"}'},
    }
    completion.choices[0].message = type(completion.choices[0].message).model_validate(
        {**completion.choices[0].message.model_dump(), "tool_calls": [call]}
    )

    async def chunks():
        payload = completion.model_dump()
        delta = {**payload["choices"][0]["message"], "tool_calls": [{**call, "index": 0}]}
        payload.update(
            object="chat.completion.chunk",
            choices=[{"index": 0, "delta": delta, "finish_reason": "tool_calls"}],
        )
        yield ChatCompletionChunk.model_validate(payload)

    async with AsyncOpenAI(api_key="test-placeholder") as client:
        monkeypatch.setattr(
            client.chat.completions,
            "create",
            AsyncMock(return_value=chunks() if streamed else completion),
        )
        model = OpenAIChatCompletionsModel("fixture", client)
        with trace("group"), operation_tracing_scope({"operation_detail": "content"}, True):
            args = (None, "prompt", ModelSettings(), [], None, [], ModelTracing.ENABLED)
            if streamed:
                async for _ in model.stream_response(*args):
                    pass
            else:
                await model.get_response(*args)
    span = next(s for s in exporter.get_finished_spans() if s.name == "chat fixture")
    messages = json.loads(json.dumps(span.attributes["gen_ai.output.messages"]))
    assert messages[0]["role"] == "assistant"
    assert messages[0]["parts"] == [
        {"type": "text", "content": "answer"},
        {"type": "tool_call", "id": "call-lookup", "name": "lookup", "arguments": {"key": "value"}},
    ]


@pytest.mark.parametrize("details", [None, 0])
def test_missing_or_zero_usage_details_do_not_add_optional_counts(otel, details) -> None:
    _, exporter = otel
    usage: dict[str, object] = {"input_tokens": 13, "output_tokens": 5}
    if details is not None:
        usage.update(
            {
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            }
        )
    with trace("usage"), generation_span(model="test-model") as model:
        model.span_data.usage = usage
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.usage.input_tokens"] == 13
    assert span.attributes["gen_ai.usage.output_tokens"] == 5
    assert not any(
        key in span.attributes
        for key in (
            "gen_ai.usage.cache_read.input_tokens",
            "gen_ai.usage.cache_write.input_tokens",
            "gen_ai.usage.reasoning.output_tokens",
        )
    )

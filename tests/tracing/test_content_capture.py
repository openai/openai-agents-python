from __future__ import annotations

import json
from collections.abc import Iterator

import httpx2
import pytest
from openai import AsyncOpenAI, BadRequestError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents import Agent, RunConfig, Runner
from agents.extensions.tracing import OpenTelemetryTracingProcessor
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tracing import (
    add_trace_processor,
    function_span,
    generation_span,
    set_trace_processors,
    trace,
)
from agents.tracing._content import CONTENT_LIMIT, bound_content
from tests.testing_processor import SPAN_PROCESSOR_TESTING, SpanProcessorForTests


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor = OpenTelemetryTracingProcessor(tracer_provider=provider)
    set_trace_processors([processor])
    try:
        yield exporter
    finally:
        processor.shutdown()
        set_trace_processors([SPAN_PROCESSOR_TESTING])
        provider.shutdown()


def test_public_tool_export_redacts_embedded_credentials_and_binary(exporter) -> None:
    arguments = {
        "request": json.dumps(
            {
                "apiKey": "CREDENTIAL_CANARY",
                "query": "visible query",
                "media": json.dumps({"uri": "data:image/png;base64,NESTED_CANARY"}),
            }
        ),
        "file": {"type": "input_file", "file_data": "FILE_CANARY"},
    }
    result = json.dumps(
        [
            {"type": "image", "data": "IMAGE_CANARY", "mimeType": "image/png"},
            {"type": "resource", "resource": {"blob": "BLOB_CANARY"}},
            {"type": "resource", "resource": {"text": "visible resource"}},
            {"type": "text", "text": "visible result"},
        ]
    )
    with trace("content"), function_span("lookup", input=json.dumps(arguments)) as tool:
        tool.span_data.operation.detail = "content"
        tool.span_data.output = result
    attributes = dict(exporter.get_finished_spans()[0].attributes)
    serialized = json.dumps(attributes)
    assert "CANARY" not in serialized
    assert "visible query" in serialized
    assert "visible resource" in serialized
    assert "visible result" in serialized
    assert "omitted" in serialized
    # Function data remains a string; the exporter preserves embedded JSON validity.
    assert isinstance(tool.span_data.input, str)
    assert isinstance(json.loads(attributes["gen_ai.tool.call.result"]), list)


def test_public_model_export_omits_hidden_reasoning_and_embedded_media(exporter) -> None:
    with trace("content"), generation_span(model="test-model") as model:
        model.span_data.operation.detail = "content"
        model.span_data.input = [
            {
                "role": "user",
                "content": "visible before data:image/png;base64,BINARY_CANARY visible after",
            }
        ]
        model.span_data.output = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {"Authorization": "AUTH_CANARY", "answer": "visible answer"}
                        ),
                    },
                    {"type": "reasoning", "text": "REASONING_CANARY"},
                ],
            }
        ]
    serialized = json.dumps(dict(exporter.get_finished_spans()[0].attributes))
    assert "CANARY" not in serialized
    assert "visible before" in serialized
    assert "visible after" in serialized
    assert "visible answer" in serialized
    assert "omitted" in serialized


def test_public_tool_export_retains_large_result_prefix_with_bounded_json(exporter) -> None:
    with trace("content"), function_span("read_file") as tool:
        tool.span_data.operation.detail = "content"
        tool.span_data.output = json.dumps({"text": "file prefix: " + "é🙂" * 40_000})
    captured = exporter.get_finished_spans()[0].attributes["gen_ai.tool.call.result"]
    assert len(json.dumps(captured, ensure_ascii=False).encode("utf-8")) <= CONTENT_LIMIT
    decoded = json.loads(captured)
    assert decoded["text"].startswith("file prefix: ")
    assert decoded["text"].endswith("[truncated]")


@pytest.mark.parametrize(
    "value",
    [
        "é🙂" * 40_000,
        [{"text": "x" * 180}] * 3_000,
        {"input": [{"role": "user", "content": "x" * 80_000}], "instructions": "later"},
    ],
)
def test_projection_keeps_valid_structure_and_inline_budget_marker(value) -> None:
    captured = bound_content(value)
    serialized = json.dumps(captured, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= CONTENT_LIMIT
    assert "truncated" in serialized
    assert json.loads(serialized) == captured


def test_embedded_tool_json_remains_parseable_after_redaction_and_truncation() -> None:
    raw = json.dumps({"accessToken": "TOKEN_CANARY", "result": "prefix " + "🙂" * 40_000})
    captured = bound_content(raw)
    assert isinstance(captured, str)
    decoded = json.loads(captured)
    assert decoded["result"].startswith("prefix ")
    assert decoded["result"].endswith("[truncated]")
    assert decoded["accessToken"] == "[omitted]"
    assert len(json.dumps(captured, ensure_ascii=False).encode("utf-8")) <= CONTENT_LIMIT


def test_embedded_json_that_fits_the_budget_remains_intact() -> None:
    raw = json.dumps({"text": "x" * 27_000})
    assert bound_content(raw) == raw


def test_oversized_content_preserves_message_role_and_part_identity() -> None:
    captured = bound_content(
        [{"content": [{"text": "prefix " + "x" * 80_000, "type": "input_text"}], "role": "user"}]
    )
    assert captured[0]["role"] == "user"
    assert captured[0]["content"][0]["type"] == "input_text"
    assert captured[0]["content"][0]["text"].startswith("prefix ")
    assert captured[0]["content"][0]["text"].endswith("[truncated]")


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_error_cannot_export_a_reflected_transport_credential(
    exporter, stream
) -> None:
    native = SpanProcessorForTests()
    add_trace_processor(native)
    authorization_headers: list[str] = []

    # An actual provider request can receive an error message echoing its auth header.
    # The wire double preserves the SDK's exception conversion and both Runner paths.
    async def handler(request: httpx2.Request) -> httpx2.Response:
        authorization = request.headers["authorization"]
        authorization_headers.append(authorization)
        return httpx2.Response(
            400,
            json={"error": {"message": json.dumps({"headers": {"Authorization": authorization}})}},
        )

    async with AsyncOpenAI(
        api_key="TRANSPORT_CREDENTIAL_CANARY",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        agent = Agent(
            name="worker", model=OpenAIResponsesModel(model="fixture", openai_client=client)
        )
        config = RunConfig(tracing={"operation_detail": "content"})
        with pytest.raises(BadRequestError):
            if stream:
                result = Runner.run_streamed(agent, "hello", run_config=config)
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "hello", run_config=config)

    assert authorization_headers == ["Bearer TRANSPORT_CREDENTIAL_CANARY"]
    spans = native.get_ordered_spans()
    model_spans = [span for span in spans if span.span_data.type == "generation"]
    assert len(model_spans) == 1
    assert model_spans[0].error is not None
    assert model_spans[0].span_data.operation.outcome == "failed"
    # Check every native span, including agent/task/turn error fields and hosted exports.
    assert "TRANSPORT_CREDENTIAL_CANARY" not in json.dumps([span.export() for span in spans])
    for span in exporter.get_finished_spans():
        assert "TRANSPORT_CREDENTIAL_CANARY" not in str(
            (dict(span.attributes), span.events, span.status.description)
        )

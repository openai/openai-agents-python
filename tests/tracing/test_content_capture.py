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
from agents.tracing.config import OperationDetail, operation_tracing_scope
from agents.tracing.span_data import GenerationSpanData
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


def test_public_tool_export_omits_oversized_structured_result_with_bounded_json(exporter) -> None:
    with trace("content"), function_span("read_file") as tool:
        tool.span_data.operation.detail = "content"
        tool.span_data.output = json.dumps({"text": "file prefix: " + "é🙂" * 40_000})
    captured = exporter.get_finished_spans()[0].attributes["gen_ai.tool.call.result"]
    assert len(json.dumps(captured, ensure_ascii=False).encode("utf-8")) <= CONTENT_LIMIT
    decoded = json.loads(captured)
    assert decoded == {"omitted": True, "truncated": True, "reason": "capture_budget"}


@pytest.mark.parametrize(
    "value",
    [
        "é🙂" * 40_000,
        " " * (CONTENT_LIMIT * 8) + "ordinary text",
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
    if isinstance(value, str):
        assert captured.endswith("[truncated]")


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"accessToken": "TOKEN_CANARY", "result": "prefix " + "🙂" * 40_000}),
        "[" + ",".join(['{"apiKey":"TOKEN_CANARY"}'] * 60_000) + "]",
        json.dumps({"text": "🙂" * 10_000}, ensure_ascii=False),
        " " * (CONTENT_LIMIT * 8) + '{"apiKey":"TOKEN_CANARY"}',
    ],
    ids=["object", "array", "utf8-byte-limit", "whitespace-prefix"],
)
def test_oversized_embedded_tool_json_is_omitted_without_parsing(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_loads = json.loads
    monkeypatch.setattr(
        "agents.tracing._content.json.loads",
        lambda _: pytest.fail("Oversized structured content must not be parsed."),
    )
    captured = bound_content(raw)
    assert isinstance(captured, str)
    assert original_loads(captured) == {
        "omitted": True,
        "truncated": True,
        "reason": "capture_budget",
    }
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


@pytest.mark.parametrize("detail", [None, "metadata", "content"])
@pytest.mark.parametrize("assign", [False, True], ids=["constructor", "assignment"])
def test_generation_payload_capture_policy(detail: OperationDetail | None, assign: bool) -> None:
    messages = [{"role": "user", "content": "visible prompt " + "é🙂" * 40_000}]
    output = [
        {
            "role": "assistant",
            "reasoning_content": "HIDDEN_CANARY",
            "content": "visible answer " + "é🙂" * 40_000,
        }
    ]
    config = {
        "instructions": "visible instructions " + "é🙂" * 40_000,
        "metadata": {"Authorization": "AUTH_CANARY"},
    }
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "description": "visible tool " + "é🙂" * 40_000},
        }
    ]
    usage = {"input_tokens": 10, "output_tokens": 5}
    policy = {"operation_detail": detail} if detail is not None else None
    with operation_tracing_scope(policy, include_sensitive=True):
        if assign:
            data = GenerationSpanData(model="fixture", usage=usage)
            data.input, data.output, data.model_config, data.tools = messages, output, config, tools
        else:
            data = GenerationSpanData(messages, output, "fixture", config, usage, tools)
    captured = (data.input, data.output, data.model_config, data.tools)
    assert all(
        value is repeated
        for value, repeated in zip(
            captured, (data.input, data.output, data.model_config, data.tools), strict=True
        )
    )
    originals = (messages, output, config, tools)
    assert data.model == "fixture" and data.usage is usage
    if detail is None:
        assert all(value is original for value, original in zip(captured, originals, strict=True))
    elif detail == "metadata":
        assert captured == (None, None, None, None)
        assert "CANARY" not in json.dumps(data.export())
    else:
        assert (
            len(
                json.dumps(
                    {"input": data.input, "model_config": data.model_config, "tools": data.tools},
                    ensure_ascii=False,
                ).encode()
            )
            <= CONTENT_LIMIT
        )
        assert len(json.dumps(data.output, ensure_ascii=False).encode()) <= CONTENT_LIMIT
        exported = json.dumps(data.export(), ensure_ascii=False)
        assert "CANARY" not in exported
        assert "visible prompt" in exported and "visible answer" in exported
        assert "truncated" in exported
        assert data.model_config is None and data.tools is None


def test_generation_payload_assignments_honor_content_veto() -> None:
    with operation_tracing_scope({"operation_detail": "content"}, include_sensitive=False):
        data = GenerationSpanData()
        data.input = [{"role": "user", "content": "INPUT_CANARY"}]
        data.output = [{"role": "assistant", "content": "OUTPUT_CANARY"}]
        data.model_config = {"instructions": "INSTRUCTIONS_CANARY"}
        data.tools = [{"description": "TOOL_CANARY"}]
    assert (data.input, data.output, data.model_config, data.tools) == (None, None, None, None)
    assert "CANARY" not in json.dumps(data.export())


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["default", "content", "veto"])
async def test_chat_input_capture_shares_one_budget_without_changing_the_request(
    exporter, stream: bool, mode: str
) -> None:
    from agents import OpenAIChatCompletionsModel
    from agents.decorators import tool
    from tests.models.test_openai_chatcompletions import _minimal_chat_completion

    native = SpanProcessorForTests()
    add_trace_processor(native)

    @tool
    def lookup() -> str:
        """Return a value."""
        return "ok"

    lookup.description = "tool-description " + "t" * 40_000
    prompt = "user-message " + "i" * 40_000
    requests = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        payload = _minimal_chat_completion("done").model_dump(mode="json")
        if stream:
            payload.update(
                object="chat.completion.chunk",
                choices=[
                    {
                        "index": 0,
                        "delta": payload["choices"][0]["message"],
                        "finish_reason": "stop",
                    }
                ],
            )
            return httpx2.Response(
                200,
                content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json=payload)

    async with AsyncOpenAI(
        api_key="test",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        agent = Agent(
            name="worker", model=OpenAIChatCompletionsModel("fixture", client), tools=[lookup]
        )
        config = RunConfig(
            tracing={} if mode == "default" else {"operation_detail": "content"},
            trace_include_sensitive_data=mode != "veto",
        )
        if stream:
            result = Runner.run_streamed(agent, prompt, run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, prompt, run_config=config)
    assert result.final_output == "done"
    assert requests[0]["messages"][-1]["content"] == prompt
    assert requests[0]["tools"][0]["function"]["description"] == lookup.description
    data = next(
        span.span_data for span in native.get_ordered_spans() if span.span_data.type == "generation"
    )
    attributes = dict(
        next(
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "chat"
        ).attributes
    )
    captured = {"input": data.input, "model_config": data.model_config, "tools": data.tools}
    exported = {
        key: value
        for key, value in attributes.items()
        if key in {"gen_ai.input.messages", "gen_ai.system_instructions", "gen_ai.tool.definitions"}
    }
    if mode == "default":
        assert data.input[-1]["content"] == prompt
        assert data.tools[0]["function"]["description"] == lookup.description
        assert exported == {}
    elif mode == "veto":
        assert all(value is None for value in captured.values())
        assert exported == {}
    else:
        assert len(json.dumps(captured, ensure_ascii=False).encode()) <= CONTENT_LIMIT
        assert len(json.dumps(exported, ensure_ascii=False).encode()) <= CONTENT_LIMIT
        assert "user-message" in json.dumps(captured)
        assert "truncated" in json.dumps(captured)
        assert data.tools is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["default", "content", "metadata", "veto"])
async def test_any_llm_responses_capture_bounds_sdk_and_otel_without_changing_provider_data(
    exporter, monkeypatch, stream: bool, mode: str
) -> None:
    pytest.importorskip("any_llm")
    from any_llm.providers.openai import base as any_llm_openai

    from agents import ModelSettings
    from agents.extensions.models.any_llm_model import AnyLLMModel
    from agents.tracing.span_data import ResponseSpanData
    from tests.models.test_any_llm_model import _response, _responses_output

    native = SpanProcessorForTests()
    add_trace_processor(native)
    input_secret, output_secret = "INPUT_CREDENTIAL_CANARY", "OUTPUT_CREDENTIAL_CANARY"
    prompt = "UNSENT_INPUT_CANARY"
    wire_input = [
        {"role": "user", "content": json.dumps({"api_key": input_secret, "query": "input-visible"})}
    ]
    wire_instructions = "wire-instructions-visible " + "i" * 40_000
    long_output = "answer-visible " + "o" * 40_000
    response = _response(json.dumps({"password": output_secret, "result": "output-visible"}))
    content = response.output[0].content
    assert content is not None
    content.extend(_responses_output(long_output)[0].content)
    payload = response.model_dump(mode="json")
    requests = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if stream:
            event = {"type": "response.completed", "response": payload, "sequence_number": 0}
            return httpx2.Response(
                200,
                content=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json=payload)

    async with AsyncOpenAI(
        api_key="test",
        base_url="https://provider.example/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler), trust_env=False),
    ) as client:
        # Keep the real AnyLLM provider and replace only its HTTP client.
        monkeypatch.setattr(any_llm_openai, "AsyncOpenAI", lambda **kwargs: client)
        agent = Agent(
            name="worker",
            model=AnyLLMModel("openai/fixture", api="responses", api_key="test"),
            instructions="UNSENT_INSTRUCTIONS_CANARY",
            model_settings=ModelSettings(
                extra_body={"input": wire_input, "instructions": wire_instructions}
            ),
        )
        config = RunConfig(
            tracing={}
            if mode == "default"
            else {"operation_detail": "metadata" if mode == "metadata" else "content"},
            trace_include_sensitive_data=mode != "veto",
        )
        if stream:
            result = Runner.run_streamed(agent, prompt, run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, prompt, run_config=config)
    assert len(requests) == 1
    assert requests[0]["input"] == wire_input
    assert requests[0]["instructions"] == wire_instructions
    assert long_output in result.final_output and output_secret in result.final_output
    data = next(
        span.span_data
        for span in native.get_ordered_spans()
        if span.span_data.type in {"response", "generation"}
    )
    if isinstance(data, ResponseSpanData):
        captured_input = data.input
        captured_output = (
            [item.model_dump(mode="json") for item in data.response.output]
            if data.response
            else None
        )
    else:
        captured_input = {"input": data.input, "model_config": data.model_config}
        captured_output = data.output
    attributes = dict(
        next(
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "chat"
        ).attributes
    )
    exported_input = {
        key: attributes[key]
        for key in ("gen_ai.input.messages", "gen_ai.system_instructions")
        if key in attributes
    }
    exported_output = attributes.get("gen_ai.output.messages")
    if mode == "default":
        assert isinstance(data, ResponseSpanData)
        assert data.input == [{"role": "user", "content": prompt}]
        assert long_output in json.dumps(captured_output)
        assert not exported_input and exported_output is None
    elif mode in {"metadata", "veto"}:
        assert data.input is None and captured_output is None
        assert not exported_input and exported_output is None
    else:
        for value in (captured_input, captured_output, exported_input, exported_output):
            encoded = json.dumps(value, ensure_ascii=False)
            assert len(encoded.encode()) <= CONTENT_LIMIT
            assert input_secret not in encoded and output_secret not in encoded
            assert "UNSENT_" not in encoded and "truncated" in encoded
        for value in (captured_input, exported_input):
            assert "input-visible" in json.dumps(value)
            assert "wire-instructions-visible" in json.dumps(value)
        assert "output-visible" in json.dumps(captured_output)

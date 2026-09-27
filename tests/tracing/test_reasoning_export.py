from __future__ import annotations

import copy
import json
from collections import UserDict, UserList
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from agents import Agent, OpenAIChatCompletionsModel, RunConfig, Runner
from agents.tracing import generation_span, trace
from agents.tracing.processors import BackendSpanExporter
from agents.tracing.span_data import FunctionSpanData, GenerationSpanData
from agents.tracing.spans import SpanImpl
from tests.testing_processor import fetch_ordered_spans


@pytest.fixture
def captured_export(monkeypatch):
    payloads: list[dict[str, Any]] = []

    def post(self, url, **kwargs):
        payloads.append(copy.deepcopy(kwargs["json"]))
        return httpx2.Response(200)

    monkeypatch.setattr(httpx2.Client, "post", post)
    exporter = BackendSpanExporter(api_key="test-key", max_retries=0)
    try:
        yield exporter, payloads
    finally:
        exporter.close()


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("field", ["reasoning", "reasoning_content", "thinking_blocks"])
@pytest.mark.allow_call_model_methods
async def test_runner_reasoning_is_omitted_from_export_but_preserved_for_replay(
    monkeypatch, captured_export, streamed, field
):
    secret = "PRIVATE_REASONING_SENTINEL"
    value: Any = (
        [{"type": "thinking", "thinking": secret, "signature": "test-signature"}]
        if field == "thinking_blocks"
        else secret
    )
    client = AsyncOpenAI(api_key="test-key", base_url="https://provider.example/v1")
    model = OpenAIChatCompletionsModel(
        model="test-model", openai_client=client, should_replay_reasoning_content=lambda _: True
    )
    common = {"id": "test-completion", "created": 0, "model": "test-model"}
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    if streamed:
        chunks = [
            ChatCompletionChunk(
                **common,
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": {"role": "assistant", field: value}}],
            ),
            ChatCompletionChunk(
                **common,
                object="chat.completion.chunk",
                choices=[
                    {"index": 0, "delta": {"content": "Visible answer"}, "finish_reason": "stop"}
                ],
                usage=usage,
            ),
        ]

        async def stream():
            for chunk in chunks:
                yield chunk

        completion = stream()
    else:
        completion = ChatCompletion(
            **common,
            object="chat.completion",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Visible answer", field: value},
                }
            ],
            usage=usage,
        )

    create = AsyncMock(return_value=completion)
    monkeypatch.setattr(client.chat.completions, "create", create)
    agent = Agent(name="Test", model=model)
    config = RunConfig(trace_include_sensitive_data=True)
    try:
        if streamed:
            result = Runner.run_streamed(agent, "Question", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, "Question", run_config=config)

        assert result.final_output == "Visible answer"
        replay = result.to_input_list()
        assert secret in json.dumps(replay)
        spans = fetch_ordered_spans()
        generation = next(span for span in spans if span.span_data.type == "generation")
        original = copy.deepcopy(generation.export())
        assert secret in json.dumps(original)

        exporter, payloads = captured_export
        exporter.export([generation])
        sent = payloads[-1]["data"][0]["span_data"]
        assert secret not in json.dumps(sent)
        assert "Visible answer" in json.dumps(sent["output"])
        assert sent["usage"]["output_tokens"] == 3
        assert generation.export() == original
        assert result.to_input_list() == replay

        # Exercise the actual next-turn converter and exported generation input.
        create.return_value = ChatCompletion(
            **common,
            object="chat.completion",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Next answer"},
                }
            ],
        )
        await Runner.run(agent, replay + [{"role": "user", "content": "Next"}], run_config=config)
        next_generation = [
            span for span in fetch_ordered_spans() if span.span_data.type == "generation"
        ][-1]
        before = copy.deepcopy(next_generation.export())
        # Direct Chat Completions does not replay native thinking blocks; LiteLLM does.
        assert (secret in json.dumps(create.call_args.kwargs["messages"])) is (
            field != "thinking_blocks"
        )
        exporter.export([next_generation])
        assert secret not in json.dumps(payloads[-1])
        assert "Visible answer" in json.dumps(payloads[-1])
        assert next_generation.export() == before
    finally:
        await client.close()


@pytest.mark.parametrize("custom_endpoint", [False, True])
def test_export_filter_preserves_user_tool_and_custom_export_data(captured_export, custom_endpoint):
    exporter, payloads = captured_export
    if custom_endpoint:
        exporter.endpoint = "https://trace.example/ingest"
    user_data = {"type": "thinking", "thinking": "user supplied", "reasoning": "tool supplied"}
    assistant = {
        "role": "assistant",
        "reasoning": "PRIVATE_REASONING_SENTINEL",
        "thinking": "PRIVATE_THINKING_SENTINEL",
        "content": [
            {"type": "thinking", "thinking": "PRIVATE_INLINE_SENTINEL"},
            {"type": "text", "text": "Visible answer"},
        ],
        "tool_calls": [{"id": "call_test", "function": {"arguments": json.dumps(user_data)}}],
    }
    data = GenerationSpanData(
        input=[{"role": "user", "content": [user_data]}, assistant],
        output=[assistant],
        usage={"input_tokens": 1, "output_tokens": 2, "reasoning_tokens": 1},
    )
    span = SpanImpl(
        trace_id="trace_test",
        span_id="span_test",
        parent_id=None,
        processor=Mock(),
        span_data=data,
        tracing_api_key=None,
    )
    original = copy.deepcopy(span.export())
    exporter.export([span])
    sent = payloads[-1]["data"][0]["span_data"]
    assert ("PRIVATE_REASONING_SENTINEL" in json.dumps(sent)) is custom_endpoint
    assert ("PRIVATE_THINKING_SENTINEL" in json.dumps(sent)) is custom_endpoint
    assert ("PRIVATE_INLINE_SENTINEL" in json.dumps(sent)) is custom_endpoint
    assert sent["input"][0]["content"] == [user_data]
    assert sent["output"][0]["tool_calls"] == assistant["tool_calls"]
    assert "reasoning_tokens" in json.dumps(sent["usage"])
    assert span.export() == original

    tool = SpanImpl(
        trace_id="trace_test",
        span_id="span_tool",
        parent_id=None,
        processor=Mock(),
        span_data=FunctionSpanData(name="test", input=None, output=json.dumps(user_data)),
        tracing_api_key=None,
    )
    exporter.export([tool])
    assert payloads[-1]["data"][0]["span_data"]["output"] == json.dumps(user_data)


@pytest.mark.parametrize("sequence_type,mapping_type", [(tuple, dict), (UserList, UserDict)])
def test_public_generation_span_filters_declared_message_sequences(
    captured_export, sequence_type, mapping_type
):
    secret = "PRIVATE_SEQUENCE_REASONING_SENTINEL"
    assistant = mapping_type(role="assistant", content="Visible answer", reasoning_content=secret)
    reasoning = mapping_type(type="reasoning", summary=[{"type": "summary_text", "text": secret}])
    inputs = sequence_type(
        [
            mapping_type(role="user", content="Question"),
            mapping_type(role="tool", content="Tool result", tool_call_id="call_test"),
            assistant,
            reasoning,
        ]
    )
    outputs = sequence_type([assistant, reasoning])
    with trace("Test"), generation_span(input=inputs, output=outputs) as span:
        original = copy.deepcopy(span.export())
        exporter, payloads = captured_export
        exporter.export([span])

        sent = payloads[-1]["data"][0]["span_data"]
        assert secret not in json.dumps(sent)
        assert sent["input"] == [
            {"role": "user", "content": "Question"},
            {"role": "tool", "content": "Tool result", "tool_call_id": "call_test"},
            {"role": "assistant", "content": "Visible answer"},
        ]
        assert sent["output"] == [{"role": "assistant", "content": "Visible answer"}]
        assert span.export() == original
        assert span.span_data.input is inputs
        assert span.span_data.output is outputs


def test_export_filter_keeps_absent_generation_data_absent(captured_export):
    exporter, payloads = captured_export
    span = SpanImpl(
        trace_id="trace_test",
        span_id="span_test",
        parent_id=None,
        processor=Mock(),
        span_data=GenerationSpanData(),
        tracing_api_key=None,
    )
    exporter.export([span])
    data = payloads[-1]["data"][0]["span_data"]
    assert data["input"] is None
    assert data["output"] is None

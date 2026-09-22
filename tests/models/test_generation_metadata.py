from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import litellm
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, RunConfig, Runner, trace
from agents.extensions.models.litellm_model import LitellmModel
from agents.models.interface import Model
from tests.testing_processor import fetch_ordered_spans, fetch_traces


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["openai", "litellm", "any-llm"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("include_data", [False, True])
async def test_generation_metadata_respects_sensitive_capture(
    monkeypatch: pytest.MonkeyPatch, adapter: str, stream: bool, include_data: bool
) -> None:
    metadata = {"request_label": "synthetic-request-metadata-sentinel"}
    settings = ModelSettings(temperature=0.25, metadata=metadata)
    request_calls: list[dict[str, Any]] = []

    async def chunks() -> AsyncIterator[ChatCompletionChunk]:
        yield ChatCompletionChunk.model_validate(
            {
                "id": "chatcmpl_test",
                "created": 0,
                "model": "test-model",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    async def completion(**kwargs: Any) -> Any:
        request_calls.append(kwargs)
        if kwargs["stream"]:
            return chunks()
        payload = {
            "id": "chatcmpl_test",
            "created": 0,
            "model": "test-model",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        if adapter == "litellm":
            return litellm.ModelResponse(**payload)
        return ChatCompletion.model_validate(payload)

    model: Model
    if adapter == "openai":
        client = SimpleNamespace(
            base_url="https://example.com/v1",
            chat=SimpleNamespace(completions=SimpleNamespace(create=completion)),
        )
        model = OpenAIChatCompletionsModel("test-model", cast(AsyncOpenAI, client))
    elif adapter == "litellm":
        monkeypatch.setattr(litellm, "acompletion", completion)
        model = LitellmModel("test-model")
    else:
        pytest.importorskip("any_llm", reason="any-llm-sdk requires Python 3.11+.")
        from agents.extensions.models.any_llm_model import AnyLLM, AnyLLMModel

        provider = SimpleNamespace(SUPPORTS_RESPONSES=False, acompletion=completion)
        monkeypatch.setattr(AnyLLM, "create", lambda *args, **kwargs: provider)
        model = AnyLLMModel("openai/test-model")

    agent = Agent(name="test", model=model, model_settings=settings)
    trace_metadata = {"metadata": "intentional-trace-label"}
    config = RunConfig(trace_include_sensitive_data=include_data, trace_metadata=trace_metadata)
    if stream:
        result = Runner.run_streamed(agent, "hello", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "hello", run_config=config)
    assert result.final_output == "ok"

    assert len(request_calls) == 1
    assert request_calls[0]["metadata"] == metadata
    assert settings.metadata == metadata
    assert metadata == {"request_label": "synthetic-request-metadata-sentinel"}
    assert trace_metadata == {"metadata": "intentional-trace-label"}
    traces = fetch_traces()
    assert len(traces) == 1
    exported_trace = traces[0].export()
    assert exported_trace is not None
    assert exported_trace["metadata"] == trace_metadata

    spans = fetch_ordered_spans()
    generations = [span for span in spans if span.span_data.type == "generation"]
    assert len(generations) == 1
    exported = generations[0].export()
    assert exported is not None
    config_data = exported["span_data"]["model_config"]
    assert config_data["temperature"] == 0.25
    if include_data:
        assert config_data["metadata"] == metadata
    else:
        assert "metadata" not in config_data
        assert "synthetic-request-metadata-sentinel" not in json.dumps(
            [span.export() for span in spans]
        )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_caller_authored_trace_metadata_remains_explicit() -> None:
    async def completion(**kwargs: Any) -> ChatCompletion:
        return ChatCompletion.model_validate(
            {
                "id": "chatcmpl_test",
                "created": 0,
                "model": "test-model",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    client = SimpleNamespace(
        base_url="https://example.com/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=completion)),
    )
    agent = Agent(
        name="test", model=OpenAIChatCompletionsModel("test-model", cast(AsyncOpenAI, client))
    )
    metadata = {"metadata": "caller-owned-label"}
    with trace("caller-owned", metadata=metadata):
        await Runner.run(agent, "hello", run_config=RunConfig(trace_include_sensitive_data=False))
    exported = fetch_traces()[0].export()
    assert exported is not None
    assert exported["metadata"] == metadata == {"metadata": "caller-owned-label"}

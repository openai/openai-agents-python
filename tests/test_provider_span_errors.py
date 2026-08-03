"""Every model provider must record a failed model call on its own span.

`Span.__exit__` finishes a span without attaching an exception, so a provider that
does not annotate its span exports a failed model call that is indistinguishable
from a successful one. `OpenAIResponsesModel` has always annotated its span; these
tests pin the same behavior for the other providers.
"""

from __future__ import annotations

from typing import Any

import pytest
from openai import AsyncOpenAI

from agents import ModelSettings, ModelTracing, OpenAIChatCompletionsModel, trace

from .testing_processor import fetch_ordered_spans


class _Boom(Exception):
    pass


def _span_error(span_filter: str) -> dict[str, Any] | None:
    for span in fetch_ordered_spans():
        if span.span_data.type == span_filter and span.error is not None:
            return dict(span.error)
    return None


async def _drain(agen: Any) -> None:
    async for _ in agen:
        pass


def _chatcompletions_model() -> OpenAIChatCompletionsModel:
    return OpenAIChatCompletionsModel(
        model="gpt-4", openai_client=AsyncOpenAI(api_key="test", base_url="http://localhost:1")
    )


def _call_kwargs() -> dict[str, Any]:
    return {
        "system_instructions": None,
        "input": "hi",
        "model_settings": ModelSettings(),
        "tools": [],
        "output_schema": None,
        "handoffs": [],
        "tracing": ModelTracing.ENABLED,
        "previous_response_id": None,
        "conversation_id": None,
        "prompt": None,
    }


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chatcompletions_get_response_records_span_error(monkeypatch) -> None:
    model = _chatcompletions_model()

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise _Boom("upstream exploded")

    monkeypatch.setattr(model, "_fetch_response", boom)
    with trace(workflow_name="test"):
        with pytest.raises(_Boom):
            await model.get_response(**_call_kwargs())

    error = _span_error("generation")
    assert error is not None, "generation span carried no error"
    assert error["message"] == "Error getting response"
    assert "upstream exploded" in error["data"]["error"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chatcompletions_stream_response_records_span_error(monkeypatch) -> None:
    model = _chatcompletions_model()

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise _Boom("stream exploded")

    monkeypatch.setattr(model, "_fetch_response", boom)
    with trace(workflow_name="test"):
        with pytest.raises(_Boom):
            await _drain(model.stream_response(**_call_kwargs()))

    error = _span_error("generation")
    assert error is not None, "generation span carried no error"
    assert error["message"] == "Error streaming response"
    assert "stream exploded" in error["data"]["error"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chatcompletions_span_error_is_redacted_without_sensitive_data(monkeypatch) -> None:
    """With tracing data disabled the exception text must not reach the span."""
    model = _chatcompletions_model()

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise _Boom("secret-connection-string")

    monkeypatch.setattr(model, "_fetch_response", boom)
    kwargs = _call_kwargs()
    kwargs["tracing"] = ModelTracing.ENABLED_WITHOUT_DATA
    with trace(workflow_name="test"):
        with pytest.raises(_Boom):
            await model.get_response(**kwargs)

    error = _span_error("generation")
    assert error is not None
    assert "secret-connection-string" not in error["data"]["error"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_litellm_get_response_records_span_error(monkeypatch) -> None:
    pytest.importorskip("litellm")
    from agents.extensions.models.litellm_model import LitellmModel

    model = LitellmModel(model="gpt-4", api_key="test")

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise _Boom("litellm exploded")

    monkeypatch.setattr(model, "_fetch_response", boom)
    with trace(workflow_name="test"):
        with pytest.raises(_Boom):
            await model.get_response(**_call_kwargs())

    error = _span_error("generation")
    assert error is not None, "generation span carried no error"
    assert error["message"] == "Error getting response"
    assert "litellm exploded" in error["data"]["error"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_litellm_stream_response_records_span_error(monkeypatch) -> None:
    pytest.importorskip("litellm")
    from agents.extensions.models.litellm_model import LitellmModel

    model = LitellmModel(model="gpt-4", api_key="test")

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise _Boom("litellm stream exploded")

    monkeypatch.setattr(model, "_fetch_response", boom)
    with trace(workflow_name="test"):
        with pytest.raises(_Boom):
            await _drain(model.stream_response(**_call_kwargs()))

    error = _span_error("generation")
    assert error is not None, "generation span carried no error"
    assert error["message"] == "Error streaming response"
    assert "litellm stream exploded" in error["data"]["error"]


def _any_llm_model() -> Any:
    from agents.extensions.models.any_llm_model import AnyLLMModel

    return AnyLLMModel(model="openai/gpt-4", api_key="test")


_ANY_LLM_BASE_KWARGS: dict[str, Any] = {
    "system_instructions": None,
    "input": "hi",
    "model_settings": ModelSettings(),
    "tools": [],
    "output_schema": None,
    "handoffs": [],
    "tracing": ModelTracing.ENABLED,
    "prompt": None,
}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "fetch", "span_type", "message", "streaming"),
    [
        (
            "_get_response_via_responses",
            "_fetch_responses_response",
            "response",
            "Error getting response",
            False,
        ),
        (
            "_stream_response_via_responses",
            "_fetch_responses_response",
            "response",
            "Error streaming response",
            True,
        ),
        (
            "_get_response_via_chat",
            "_fetch_chat_response",
            "generation",
            "Error getting response",
            False,
        ),
        (
            "_stream_response_via_chat",
            "_fetch_chat_response",
            "generation",
            "Error streaming response",
            True,
        ),
    ],
)
async def test_any_llm_records_span_error(
    monkeypatch, method: str, fetch: str, span_type: str, message: str, streaming: bool
) -> None:
    pytest.importorskip("any_llm")
    model = _any_llm_model()

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise _Boom("any_llm exploded")

    monkeypatch.setattr(model, fetch, boom)
    kwargs = dict(_ANY_LLM_BASE_KWARGS)
    if "via_responses" in method:
        kwargs.update({"previous_response_id": None, "conversation_id": None})

    with trace(workflow_name="test"):
        with pytest.raises(_Boom):
            if streaming:
                await _drain(getattr(model, method)(**kwargs))
            else:
                await getattr(model, method)(**kwargs)

    error = _span_error(span_type)
    assert error is not None, f"{span_type} span carried no error"
    assert error["message"] == message
    assert "any_llm exploded" in error["data"]["error"]

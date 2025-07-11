from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta
from openai.types.completion_usage import (
    CompletionTokensDetails,
    CompletionUsage,
    PromptTokensDetails,
)
from openai.types.responses import (
    Response,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
)

from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider


# Helper functions to create test objects consistently
def create_content_delta(content: str) -> dict[str, Any]:
    """Create a delta dictionary with regular content"""
    return {"content": content, "role": None, "function_call": None, "tool_calls": None}


def create_reasoning_delta(content: str) -> dict[str, Any]:
    """Create a delta dictionary with reasoning content. The Only difference is reasoning_content"""
    return {
        "content": None,
        "role": None,
        "function_call": None,
        "tool_calls": None,
        "reasoning_content": content,
    }


def create_chunk(delta: dict[str, Any], include_usage: bool = False) -> ChatCompletionChunk:
    """Create a ChatCompletionChunk with the given delta"""
    # Create a ChoiceDelta object from the dictionary
    delta_obj = ChoiceDelta(
        content=delta.get("content"),
        role=delta.get("role"),
        function_call=delta.get("function_call"),
        tool_calls=delta.get("tool_calls"),
    )

    # Add reasoning_content attribute dynamically if present in the delta
    if "reasoning_content" in delta:
        # Use direct assignment for the reasoning_content attribute
        delta_obj_any = cast(Any, delta_obj)
        delta_obj_any.reasoning_content = delta["reasoning_content"]

    # Create the chunk
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="deepseek is usually expected",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=delta_obj)],
    )

    if include_usage:
        chunk.usage = CompletionUsage(
            completion_tokens=4,
            prompt_tokens=2,
            total_tokens=6,
            completion_tokens_details=CompletionTokensDetails(reasoning_tokens=2),
            prompt_tokens_details=PromptTokensDetails(cached_tokens=0),
        )

    return chunk


async def create_fake_stream(
    chunks: list[ChatCompletionChunk],
) -> AsyncIterator[ChatCompletionChunk]:
    for chunk in chunks:
        yield chunk


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_yields_events_for_reasoning_content(monkeypatch) -> None:
    """
    Validate that when a model streams reasoning content,
    `stream_response` emits the appropriate sequence of events including
    `response.reasoning_summary_text.delta` events for each chunk of the reasoning content and
    constructs a completed response with a `ResponseReasoningItem` part.
    """
    # Create test chunks
    chunks = [
        # Reasoning content chunks
        create_chunk(create_reasoning_delta("Let me think")),
        create_chunk(create_reasoning_delta(" about this")),
        # Regular content chunks
        create_chunk(create_content_delta("The answer")),
        create_chunk(create_content_delta(" is 42"), include_usage=True),
    ]

    async def patched_fetch_response(self, *args, **kwargs):
        resp = Response(
            id="resp-id",
            created_at=0,
            model="fake-model",
            object="response",
            output=[],
            tool_choice="none",
            tools=[],
            parallel_tool_calls=False,
        )
        return resp, create_fake_stream(chunks)

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    output_events = []
    async for event in model.stream_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        prompt=None,
    ):
        output_events.append(event)

    # verify reasoning content events were emitted
    reasoning_delta_events = [
        e for e in output_events if e.type == "response.reasoning_summary_text.delta"
    ]
    assert len(reasoning_delta_events) == 2
    assert reasoning_delta_events[0].delta == "Let me think"
    assert reasoning_delta_events[1].delta == " about this"

    # verify regular content events were emitted
    content_delta_events = [e for e in output_events if e.type == "response.output_text.delta"]
    assert len(content_delta_events) == 2
    assert content_delta_events[0].delta == "The answer"
    assert content_delta_events[1].delta == " is 42"

    # verify the final response contains both types of content
    response_event = output_events[-1]
    assert response_event.type == "response.completed"
    assert len(response_event.response.output) == 2

    # first item should be reasoning
    assert isinstance(response_event.response.output[0], ResponseReasoningItem)
    assert response_event.response.output[0].summary[0].text == "Let me think about this"

    # second item should be message with text
    assert isinstance(response_event.response.output[1], ResponseOutputMessage)
    assert isinstance(response_event.response.output[1].content[0], ResponseOutputText)
    assert response_event.response.output[1].content[0].text == "The answer is 42"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_with_reasoning_content(monkeypatch) -> None:
    """
    Test that when a model returns reasoning content in addition to regular content,
    `get_response` properly includes both in the response output.
    """
    # create a message with reasoning content
    msg = ChatCompletionMessage(
        role="assistant",
        content="The answer is 42",
    )
    # Use dynamic attribute for reasoning_content
    # We need to cast to Any to avoid mypy errors since reasoning_content is not a defined attribute
    msg_with_reasoning = cast(Any, msg)
    msg_with_reasoning.reasoning_content = "Let me think about this question carefully"

    # create a choice with the message
    mock_choice = {
        "index": 0,
        "finish_reason": "stop",
        "message": msg_with_reasoning,
        "delta": None,
    }

    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="deepseek is expected",
        object="chat.completion",
        choices=[mock_choice],  # type: ignore[list-item]
        usage=CompletionUsage(
            completion_tokens=10,
            prompt_tokens=5,
            total_tokens=15,
            completion_tokens_details=CompletionTokensDetails(reasoning_tokens=6),
            prompt_tokens_details=PromptTokensDetails(cached_tokens=0),
        ),
    )

    async def patched_fetch_response(self, *args, **kwargs):
        return chat

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    resp = await model.get_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        prompt=None,
    )

    # should have produced a reasoning item and a message with text content
    assert len(resp.output) == 2

    # first output should be the reasoning item
    assert isinstance(resp.output[0], ResponseReasoningItem)
    assert resp.output[0].summary[0].text == "Let me think about this question carefully"

    # second output should be the message with text content
    assert isinstance(resp.output[1], ResponseOutputMessage)
    assert isinstance(resp.output[1].content[0], ResponseOutputText)
    assert resp.output[1].content[0].text == "The answer is 42"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_with_empty_reasoning_content(monkeypatch) -> None:
    """
    Test that when a model streams empty reasoning content,
    the response still processes correctly without errors.
    """
    # create test chunks with empty reasoning content
    chunks = [
        create_chunk(create_reasoning_delta("")),
        create_chunk(create_content_delta("The answer is 42"), include_usage=True),
    ]

    async def patched_fetch_response(self, *args, **kwargs):
        resp = Response(
            id="resp-id",
            created_at=0,
            model="fake-model",
            object="response",
            output=[],
            tool_choice="none",
            tools=[],
            parallel_tool_calls=False,
        )
        return resp, create_fake_stream(chunks)

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    output_events = []
    async for event in model.stream_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        prompt=None,
    ):
        output_events.append(event)

    # verify the final response contains the content
    response_event = output_events[-1]
    assert response_event.type == "response.completed"

    # should only have the message, not an empty reasoning item
    assert len(response_event.response.output) == 1
    assert isinstance(response_event.response.output[0], ResponseOutputMessage)
    assert isinstance(response_event.response.output[0].content[0], ResponseOutputText)
    assert response_event.response.output[0].content[0].text == "The answer is 42"

from __future__ import annotations

from typing import Any

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion_chunk import Choice
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


# Define our own ChoiceDelta since the import is causing issues
class ChoiceDelta:
    def __init__(self, content=None, role=None, function_call=None, tool_calls=None):
        self.content = content
        self.role = role
        self.function_call = function_call
        self.tool_calls = tool_calls
        # We'll add reasoning_content attribute dynamically later


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_yields_events_for_reasoning_content(monkeypatch) -> None:
    """
    Validate that when a model streams reasoning content,
    `stream_response` emits the appropriate sequence of events including
    `response.reasoning_summary_text.delta` events for each chunk of the reasoning content and
    constructs a completed response with a `ResponseReasoningItem` part.
    """
    # Simulate reasoning content coming in two pieces
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(content=None, role=None, function_call=None, tool_calls=None),  # type: ignore
            )
        ],
    )
    chunk1.choices[0].delta.reasoning_content = "Let me think"  # type: ignore[attr-defined]

    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(content=None, role=None, function_call=None, tool_calls=None),  # type: ignore
            )
        ],
    )
    chunk2.choices[0].delta.reasoning_content = " about this"  # type: ignore[attr-defined]

    # Then regular content in two pieces
    chunk3 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(
                    content="The answer", role=None, function_call=None, tool_calls=None  # type: ignore
                ),
            )
        ],
    )

    chunk4 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(content=" is 42", role=None, function_call=None, tool_calls=None),  # type: ignore
            )
        ],
        usage=CompletionUsage(
            completion_tokens=4,
            prompt_tokens=2,
            total_tokens=6,
            completion_tokens_details=CompletionTokensDetails(reasoning_tokens=2),
            prompt_tokens_details=PromptTokensDetails(cached_tokens=0),
        ),
    )

    async def fake_stream():
        for c in (chunk1, chunk2, chunk3, chunk4):
            yield c

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
        return resp, fake_stream()

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
    ):
        output_events.append(event)

    # Verify reasoning content events were emitted
    reasoning_delta_events = [
        e for e in output_events if e.type == "response.reasoning_summary_text.delta"
    ]
    assert len(reasoning_delta_events) == 2
    assert reasoning_delta_events[0].delta == "Let me think"
    assert reasoning_delta_events[1].delta == " about this"

    # Verify regular content events were emitted
    content_delta_events = [e for e in output_events if e.type == "response.output_text.delta"]
    assert len(content_delta_events) == 2
    assert content_delta_events[0].delta == "The answer"
    assert content_delta_events[1].delta == " is 42"

    # Verify the final response contains both types of content
    response_event = output_events[-1]
    assert response_event.type == "response.completed"
    assert len(response_event.response.output) == 2

    # First item should be reasoning
    assert isinstance(response_event.response.output[0], ResponseReasoningItem)
    assert response_event.response.output[0].summary[0].text == "Let me think about this"

    # Second item should be message with text
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
    # Create a mock completion with reasoning content
    msg = ChatCompletionMessage(
        role="assistant",
        content="The answer is 42",
    )
    # Add reasoning_content attribute dynamically
    msg.reasoning_content = "Let me think about this question carefully"  # type: ignore[attr-defined]

    # Using a dict directly to avoid type errors
    mock_choice: dict[str, Any] = {
        "index": 0,
        "finish_reason": "stop",
        "message": msg,
        "delta": None
    }

    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
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
    )

    # Should have produced a reasoning item and a message with text content
    assert len(resp.output) == 2

    # First output should be the reasoning item
    assert isinstance(resp.output[0], ResponseReasoningItem)
    assert resp.output[0].summary[0].text == "Let me think about this question carefully"

    # Second output should be the message with text content
    assert isinstance(resp.output[1], ResponseOutputMessage)
    assert isinstance(resp.output[1].content[0], ResponseOutputText)
    assert resp.output[1].content[0].text == "The answer is 42"

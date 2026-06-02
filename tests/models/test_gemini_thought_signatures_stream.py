"""
Test for Gemini thought signatures in streaming function calls.

Validates that thought signatures are captured from streaming chunks
and included in the final function call events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import (
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.responses import Response

from agents.models.chatcmpl_stream_handler import ChatCmplStreamHandler

# ========== Helper Functions ==========


def create_tool_call_delta(
    index: int,
    tool_call_id: str | None = None,
    function_name: str | None = None,
    arguments: str | None = None,
    provider_specific_fields: dict[str, Any] | None = None,
    extra_content: dict[str, Any] | None = None,
) -> ChoiceDeltaToolCall:
    """Create a tool call delta for streaming."""
    function = ChoiceDeltaToolCallFunction(
        name=function_name,
        arguments=arguments,
    )

    delta = ChoiceDeltaToolCall(
        index=index,
        id=tool_call_id,
        type="function" if tool_call_id else None,
        function=function,
    )

    # Add provider_specific_fields (litellm format)
    if provider_specific_fields:
        delta_any = cast(Any, delta)
        delta_any.provider_specific_fields = provider_specific_fields

    # Add extra_content (Google chatcmpl format)
    if extra_content:
        delta_any = cast(Any, delta)
        delta_any.extra_content = extra_content

    return delta


def create_chunk(
    tool_calls: list[ChoiceDeltaToolCall] | None = None,
    content: str | None = None,
    include_usage: bool = False,
) -> ChatCompletionChunk:
    """Create a ChatCompletionChunk for testing."""
    delta = ChoiceDelta(
        content=content,
        role="assistant" if content or tool_calls else None,
        tool_calls=tool_calls,
    )

    chunk = ChatCompletionChunk(
        id="chunk-id-123",
        created=1,
        model="gemini/gemini-3-pro",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=delta, finish_reason=None)],
    )

    if include_usage:
        from openai.types.completion_usage import CompletionUsage

        chunk.usage = CompletionUsage(
            completion_tokens=10,
            prompt_tokens=5,
            total_tokens=15,
        )

    return chunk


def create_final_chunk() -> ChatCompletionChunk:
    """Create a final chunk with finish_reason='tool_calls'."""
    return ChatCompletionChunk(
        id="chunk-id-456",
        created=1,
        model="gemini/gemini-3-pro",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(), finish_reason="tool_calls")],
    )


async def create_fake_stream(
    chunks: list[ChatCompletionChunk],
) -> AsyncIterator[ChatCompletionChunk]:
    """Create an async iterator from chunks."""
    for chunk in chunks:
        yield chunk


def create_mock_response() -> Response:
    """Create a mock Response object."""
    return Response(
        id="resp-id",
        created_at=0,
        model="gemini/gemini-3-pro",
        object="response",
        output=[],
        tool_choice="auto",
        tools=[],
        parallel_tool_calls=False,
    )


# ========== Tests ==========


@pytest.mark.asyncio
async def test_stream_captures_litellmprovider_specific_fields_thought_signature():
    """Test streaming captures thought_signature from litellm's provider_specific_fields."""
    chunks = [
        create_chunk(
            tool_calls=[
                create_tool_call_delta(
                    index=0,
                    tool_call_id="call_stream_1",
                    function_name="get_weather",
                    provider_specific_fields={"thought_signature": "litellm_sig_123"},
                )
            ]
        ),
        create_chunk(tool_calls=[create_tool_call_delta(index=0, arguments='{"city": "Tokyo"}')]),
        create_final_chunk(),
    ]

    response = create_mock_response()
    stream = create_fake_stream(chunks)

    events = []
    async for event in ChatCmplStreamHandler.handle_stream(
        response,
        stream,  # type: ignore[arg-type]
        model="gemini/gemini-3-pro",
    ):
        events.append(event)

    # Find function call done event
    done_events = [e for e in events if e.type == "response.output_item.done"]
    func_done = [
        e for e in done_events if hasattr(e.item, "type") and e.item.type == "function_call"
    ]
    assert len(func_done) == 1

    provider_data = func_done[0].item.model_dump().get("provider_data", {})
    assert provider_data.get("thought_signature") == "litellm_sig_123"
    assert provider_data["model"] == "gemini/gemini-3-pro"
    assert provider_data["response_id"] == "chunk-id-123"


@pytest.mark.asyncio
async def test_stream_captures_google_extra_content_thought_signature():
    """Test streaming captures thought_signature from Google's extra_content format."""
    chunks = [
        create_chunk(
            tool_calls=[
                create_tool_call_delta(
                    index=0,
                    tool_call_id="call_stream_2",
                    function_name="search",
                    extra_content={"google": {"thought_signature": "google_sig_456"}},
                )
            ]
        ),
        create_chunk(tool_calls=[create_tool_call_delta(index=0, arguments='{"query": "test"}')]),
        create_final_chunk(),
    ]

    response = create_mock_response()
    stream = create_fake_stream(chunks)

    events = []
    async for event in ChatCmplStreamHandler.handle_stream(
        response,
        stream,  # type: ignore[arg-type]
        model="gemini/gemini-3-pro",
    ):
        events.append(event)

    done_events = [e for e in events if e.type == "response.output_item.done"]
    func_done = [
        e for e in done_events if hasattr(e.item, "type") and e.item.type == "function_call"
    ]
    assert len(func_done) == 1

    provider_data = func_done[0].item.model_dump().get("provider_data", {})
    assert provider_data.get("thought_signature") == "google_sig_456"
    assert provider_data["model"] == "gemini/gemini-3-pro"
    assert provider_data["response_id"] == "chunk-id-123"

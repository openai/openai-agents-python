from __future__ import annotations

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage, Choice, ChoiceDelta
from openai.types.completion_usage import CompletionUsage, CompletionTokensDetails, PromptTokensDetails
from openai.types.responses import (
    Response,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseReasoningSummaryTextDeltaEvent,
)

from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider


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
        choices=[Choice(index=0, delta=ChoiceDelta(reasoning_content="Let me think"))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(reasoning_content=" about this"))],
    )
    # Then regular content in two pieces
    chunk3 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="The answer"))],
    )
    chunk4 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content=" is 42"))],
        usage=CompletionUsage(
            completion_tokens=4, 
            prompt_tokens=2, 
            total_tokens=6,
            completion_tokens_details=CompletionTokensDetails(
                reasoning_tokens=2
            ),
            prompt_tokens_details=PromptTokensDetails(
                cached_tokens=0
            ),
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
    
    # Expect sequence as followed: created, reasoning item added, reasoning summary part added,
    # two reasoning summary text delta events, reasoning summary part done, reasoning item done,
    # output item added, content part added, two text delta events, content part done, 
    # output item done, completed
    assert len(output_events) == 13
    assert output_events[0].type == "response.created"
    assert output_events[1].type == "response.output_item.added"
    assert output_events[2].type == "response.reasoning_summary_part.added"
    assert output_events[3].type == "response.reasoning_summary_text.delta"
    assert output_events[3].delta == "Let me think"
    assert output_events[4].type == "response.reasoning_summary_text.delta"
    assert output_events[4].delta == " about this"
    assert output_events[5].type == "response.reasoning_summary_part.done"
    assert output_events[6].type == "response.output_item.done"
    assert output_events[7].type == "response.output_item.added"
    assert output_events[8].type == "response.content_part.added"
    assert output_events[9].type == "response.output_text.delta"
    assert output_events[9].delta == "The answer"
    assert output_events[10].type == "response.output_text.delta"
    assert output_events[10].delta == " is 42"
    assert output_events[11].type == "response.content_part.done"
    assert output_events[12].type == "response.completed"
    
    completed_resp = output_events[12].response
    assert len(completed_resp.output) == 2
    assert isinstance(completed_resp.output[0], ResponseReasoningItem)
    assert completed_resp.output[0].content == "Let me think about this"
    assert isinstance(completed_resp.output[1], ResponseOutputMessage)
    assert len(completed_resp.output[1].content) == 1
    assert isinstance(completed_resp.output[1].content[0], ResponseOutputText)
    assert completed_resp.output[1].content[0].text == "The answer is 42"
    assert completed_resp.usage.output_tokens == 4
    assert completed_resp.usage.input_tokens == 2
    assert completed_resp.usage.total_tokens == 6
    assert completed_resp.usage.output_tokens_details.reasoning_tokens == 2
    assert completed_resp.usage.input_tokens_details.cached_tokens == 0


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
        reasoning_content="Let me think about this question carefully"
    )
    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
        object="chat.completion",
        choices=[Choice(index=0, finish_reason="stop", message=msg)],
        usage=CompletionUsage(
            completion_tokens=10,
            prompt_tokens=5,
            total_tokens=15,
            completion_tokens_details=CompletionTokensDetails(
                reasoning_tokens=6
            ),
            prompt_tokens_details=PromptTokensDetails(
                cached_tokens=0
            ),
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
    assert resp.output[0].content == "Let me think about this question carefully"
    
    # Second output should be the message with text content
    assert isinstance(resp.output[1], ResponseOutputMessage)
    assert len(resp.output[1].content) == 1
    assert isinstance(resp.output[1].content[0], ResponseOutputText)
    assert resp.output[1].content[0].text == "The answer is 42"
    
    # Usage should be preserved from underlying ChatCompletion.usage
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 10
    assert resp.usage.total_tokens == 15
    assert resp.usage.output_tokens_details.reasoning_tokens == 6
    assert resp.usage.input_tokens_details.cached_tokens == 0 
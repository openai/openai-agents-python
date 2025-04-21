from collections.abc import AsyncIterator

import pytest
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
)

from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_yields_events_for_text_content(monkeypatch) -> None:
    """
    Validate that `stream_response` emits the correct sequence of events when
    streaming a simple assistant message consisting of plain text content.
    We simulate two chunks of text returned from the chat completion stream.
    """
    # Create two chunks that will be emitted by the fake stream.
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="He"))],
    )
    # Mark last chunk with usage so stream_response knows this is final.
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="llo"))],
        usage=CompletionUsage(completion_tokens=5, prompt_tokens=7, total_tokens=12),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for c in (chunk1, chunk2):
            yield c

    # Patch _fetch_response to inject our fake stream
    async def patched_fetch_response(self, *args, **kwargs):
        # `_fetch_response` is expected to return a Response skeleton and the async stream
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
    # We expect a response.created, then a response.output_item.added, content part added,
    # two content delta events (for "He" and "llo"), a content part done, the assistant message
    # output_item.done, and finally response.completed.
    # There should be 8 events in total.
    assert len(output_events) == 8
    # First event indicates creation.
    assert output_events[0].type == "response.created"
    # The output item added and content part added events should mark the assistant message.
    assert output_events[1].type == "response.output_item.added"
    assert output_events[2].type == "response.content_part.added"
    # Two text delta events.
    assert output_events[3].type == "response.output_text.delta"
    assert output_events[3].delta == "He"
    assert output_events[4].type == "response.output_text.delta"
    assert output_events[4].delta == "llo"
    # After streaming, the content part and item should be marked done.
    assert output_events[5].type == "response.content_part.done"
    assert output_events[6].type == "response.output_item.done"
    # Last event indicates completion of the stream.
    assert output_events[7].type == "response.completed"
    # The completed response should have one output message with full text.
    completed_resp = output_events[7].response
    assert isinstance(completed_resp.output[0], ResponseOutputMessage)
    assert isinstance(completed_resp.output[0].content[0], ResponseOutputText)
    assert completed_resp.output[0].content[0].text == "Hello"

    assert completed_resp.usage, "usage should not be None"
    assert completed_resp.usage.input_tokens == 7
    assert completed_resp.usage.output_tokens == 5
    assert completed_resp.usage.total_tokens == 12


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_yields_events_for_refusal_content(monkeypatch) -> None:
    """
    Validate that when the model streams a refusal string instead of normal content,
    `stream_response` emits the appropriate sequence of events including
    `response.refusal.delta` events for each chunk of the refusal message and
    constructs a completed assistant message with a `ResponseOutputRefusal` part.
    """
    # Simulate refusal text coming in two pieces, like content but using the `refusal`
    # field on the delta rather than `content`.
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(refusal="No"))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(refusal="Thanks"))],
        usage=CompletionUsage(completion_tokens=2, prompt_tokens=2, total_tokens=4),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for c in (chunk1, chunk2):
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
    # Expect sequence similar to text: created, output_item.added, content part added,
    # two refusal delta events, content part done, output_item.done, completed.
    assert len(output_events) == 8
    assert output_events[0].type == "response.created"
    assert output_events[1].type == "response.output_item.added"
    assert output_events[2].type == "response.content_part.added"
    assert output_events[3].type == "response.refusal.delta"
    assert output_events[3].delta == "No"
    assert output_events[4].type == "response.refusal.delta"
    assert output_events[4].delta == "Thanks"
    assert output_events[5].type == "response.content_part.done"
    assert output_events[6].type == "response.output_item.done"
    assert output_events[7].type == "response.completed"
    completed_resp = output_events[7].response
    assert isinstance(completed_resp.output[0], ResponseOutputMessage)
    refusal_part = completed_resp.output[0].content[0]
    assert isinstance(refusal_part, ResponseOutputRefusal)
    assert refusal_part.refusal == "NoThanks"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_yields_events_for_tool_call(monkeypatch) -> None:
    """
    Validate that `stream_response` emits the correct sequence of events when
    the model is streaming a function/tool call instead of plain text.
    The function call will be split across two chunks.
    """
    # Simulate a single tool call whose ID stays constant and function name/args built over chunks.
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_", arguments="arg1"),
        type="function",
    )
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="func", arguments="arg2"),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta1]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta2]))],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for c in (chunk1, chunk2):
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
    # Sequence should be: response.created, then after loop we expect function call-related events:
    # one response.output_item.added for function call, a response.function_call_arguments.delta,
    # a response.output_item.done, and finally response.completed.
    assert output_events[0].type == "response.created"
    # The next three events are about the tool call.
    assert output_events[1].type == "response.output_item.added"
    # The added item should be a ResponseFunctionToolCall.
    added_fn = output_events[1].item
    assert isinstance(added_fn, ResponseFunctionToolCall)
    assert added_fn.name == "my_func"  # Name should be concatenation of both chunks.
    assert added_fn.arguments == "arg1arg2"
    assert output_events[2].type == "response.function_call_arguments.delta"
    assert output_events[2].delta == "arg1arg2"
    assert output_events[3].type == "response.output_item.done"
    assert output_events[4].type == "response.completed"
    assert output_events[2].delta == "arg1arg2"
    assert output_events[3].type == "response.output_item.done"
    assert output_events[4].type == "response.completed"
    assert added_fn.name == "my_func"  # Name should be concatenation of both chunks.
    assert added_fn.arguments == "arg1arg2"
    assert output_events[2].type == "response.function_call_arguments.delta"
    assert output_events[2].delta == "arg1arg2"
    assert output_events[3].type == "response.output_item.done"
    assert output_events[4].type == "response.completed"

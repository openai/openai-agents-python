import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import (
    CompletionTokensDetails,
    CompletionUsage,
    PromptTokensDetails,
)
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseRefusalDeltaEvent,
)

from agents.extensions.models.litellm_model import LitellmModel
from agents.extensions.models.litellm_provider import LitellmProvider
from agents.items import TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing


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
        usage=CompletionUsage(
            completion_tokens=5,
            prompt_tokens=7,
            total_tokens=12,
            completion_tokens_details=CompletionTokensDetails(reasoning_tokens=2),
            prompt_tokens_details=PromptTokensDetails(cached_tokens=6),
        ),
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

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)
    model = LitellmProvider().get_model("gpt-4")
    output_events = []
    async for event in model.stream_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(preserve_raw_usage=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
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
    assert completed_resp.usage.input_tokens_details.cached_tokens == 6
    assert completed_resp.usage.output_tokens_details.reasoning_tokens == 2
    # LiteLLM has already normalized usage before the Agents adapter receives this chunk, so
    # omitted-versus-null provenance is unavailable and no raw snapshot should be attached.
    assert not hasattr(completed_resp, "_agents_sdk_raw_usage")


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

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)
    model = LitellmProvider().get_model("gpt-4")
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
        conversation_id=None,
        prompt=None,
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
    # Simulate a single tool call with complete function name in first chunk
    # and arguments split across chunks (reflecting real API behavior)
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments="arg1"),
        type="function",
    )
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name=None, arguments="arg2"),
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

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)
    model = LitellmProvider().get_model("gpt-4")
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
        conversation_id=None,
        prompt=None,
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
    assert added_fn.name == "my_func"  # Name should be complete from first chunk
    assert added_fn.arguments == ""  # Arguments start empty
    assert output_events[2].type == "response.function_call_arguments.delta"
    assert output_events[2].delta == "arg1"  # First argument chunk
    assert output_events[3].type == "response.function_call_arguments.delta"
    assert output_events[3].delta == "arg2"  # Second argument chunk
    assert output_events[4].type == "response.output_item.done"
    assert output_events[5].type == "response.completed"
    # Final function call should have complete arguments
    final_fn = output_events[4].item
    assert isinstance(final_fn, ResponseFunctionToolCall)
    assert final_fn.name == "my_func"
    assert final_fn.arguments == "arg1arg2"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_yields_real_time_function_call_arguments(monkeypatch) -> None:
    """
    Validate that LiteLLM `stream_response` also emits function call arguments in real-time
    as they are received, ensuring consistent behavior across model providers.
    """
    # Simulate realistic chunks: name first, then arguments incrementally
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        id="litellm-call-456",
        function=ChoiceDeltaToolCallFunction(name="generate_code", arguments=""),
        type="function",
    )
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='{"language": "'),
        type="function",
    )
    tool_call_delta3 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='python", "task": "'),
        type="function",
    )
    tool_call_delta4 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='hello world"}'),
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
    )
    chunk3 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta3]))],
    )
    chunk4 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta4]))],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
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

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)
    model = LitellmProvider().get_model("gpt-4")
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
        conversation_id=None,
        prompt=None,
    ):
        output_events.append(event)

    # Extract events by type
    function_args_delta_events = [
        e for e in output_events if e.type == "response.function_call_arguments.delta"
    ]
    output_item_added_events = [e for e in output_events if e.type == "response.output_item.added"]

    # Verify we got real-time streaming (3 argument delta events)
    assert len(function_args_delta_events) == 3
    assert len(output_item_added_events) == 1

    # Verify the deltas were streamed correctly
    expected_deltas = ['{"language": "', 'python", "task": "', 'hello world"}']
    for i, delta_event in enumerate(function_args_delta_events):
        assert delta_event.delta == expected_deltas[i]

    # Verify function call metadata
    added_event = output_item_added_events[0]
    assert isinstance(added_event.item, ResponseFunctionToolCall)
    assert added_event.item.name == "generate_code"
    assert added_event.item.call_id == "litellm-call-456"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_synthesizes_refusal_on_content_filter(monkeypatch) -> None:
    """A stream that terminates with finish_reason == "content_filter" and no
    emitted content (as Anthropic-on-Bedrock does via LiteLLM) must synthesize a
    ResponseOutputRefusal so the completed response carries an explicit refusal
    rather than an empty assistant turn.

    Mirrors the real Bedrock chunk shape: an empty-string content delta followed
    by a terminal content_filter chunk with no content. The empty "" delta must
    not open a text content part; the synthesized refusal must be the only
    content part, at the same index in the stream and in response.completed.
    """
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(role="assistant", content=""))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(), finish_reason="content_filter")],
        usage=CompletionUsage(
            completion_tokens=0,
            prompt_tokens=7,
            total_tokens=7,
        ),
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

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)
    model = LitellmProvider().get_model("gpt-4")
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
        conversation_id=None,
        prompt=None,
    ):
        output_events.append(event)

    types = [e.type for e in output_events]
    # Coherent refusal sequence: the message + refusal part are opened, a refusal
    # delta is emitted, and the parts/message are closed before completion.
    assert "response.output_item.added" in types
    assert "response.content_part.added" in types
    assert "response.refusal.delta" in types
    assert types[-1] == "response.completed"
    assert "response.output_item.done" in types

    # The refusal delta carries a non-empty message.
    refusal_deltas = [e for e in output_events if e.type == "response.refusal.delta"]
    assert refusal_deltas and refusal_deltas[0].delta

    # Event coherence: the assistant message is announced exactly once, and every
    # content part that is opened is also closed.
    assert types.count("response.output_item.added") == 1
    assert types.count("response.content_part.added") == types.count("response.content_part.done")

    # The empty "" content delta must NOT open a text content part: no text part
    # events and no output_text.delta are emitted at all.
    assert "response.output_text.delta" not in types
    added_parts = [e for e in output_events if e.type == "response.content_part.added"]
    assert len(added_parts) == 1
    assert isinstance(added_parts[0].part, ResponseOutputRefusal)

    # The completed response contains exactly one content part: the refusal.
    completed_event = output_events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    completed_resp = completed_event.response
    assert isinstance(completed_resp.output[0], ResponseOutputMessage)
    assert len(completed_resp.output[0].content) == 1
    refusal_part = completed_resp.output[0].content[0]
    assert isinstance(refusal_part, ResponseOutputRefusal)
    assert refusal_part.refusal

    # The refusal's streamed content_index matches its position in the completed
    # response (0), so raw-event replay and the final response stay aligned.
    assert added_parts[0].content_index == 0
    assert refusal_deltas[0].content_index == 0
    done_parts = [e for e in output_events if e.type == "response.content_part.done"]
    assert len(done_parts) == 1
    assert done_parts[0].content_index == 0


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_content_filter_does_not_clobber_text(monkeypatch) -> None:
    """A content_filter finish_reason that arrives AFTER real text was streamed
    must not synthesize a refusal (the text stands)."""
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="answer"))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(), finish_reason="content_filter")],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=7, total_tokens=8),
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

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)
    model = LitellmProvider().get_model("gpt-4")
    output_events = [
        event
        async for event in model.stream_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    assert "response.refusal.delta" not in [e.type for e in output_events]
    completed_event = output_events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    completed_resp = completed_event.response
    assert isinstance(completed_resp.output[0], ResponseOutputMessage)
    assert isinstance(completed_resp.output[0].content[0], ResponseOutputText)
    assert completed_resp.output[0].content[0].text == "answer"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_content_filter_refusal_after_reasoning(monkeypatch) -> None:
    """A content_filter turn preceded by reasoning must still place the
    synthesized refusal at content_index 0 of the assistant message. Reasoning
    is a *separate* output item (it shifts the message's output_index, not its
    content_index), so the refusal — the sole content part — stays at
    content_index 0 in both the stream and response.completed."""
    reasoning_delta = ChoiceDelta(role="assistant", content=None)
    # reasoning_content is a provider extra field the handler reads via hasattr.
    reasoning_delta.reasoning_content = "thinking..."  # type: ignore[attr-defined]
    chunk_reasoning = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=reasoning_delta)],
    )
    chunk_empty = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content=""))],
    )
    chunk_filter = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(), finish_reason="content_filter")],
        usage=CompletionUsage(completion_tokens=0, prompt_tokens=7, total_tokens=7),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for c in (chunk_reasoning, chunk_empty, chunk_filter):
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

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)
    model = LitellmProvider().get_model("gpt-4")
    output_events = [
        event
        async for event in model.stream_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    # A reasoning item was produced as a separate output item.
    completed_event = output_events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    completed_resp = completed_event.response
    assert isinstance(completed_resp.output[0], ResponseReasoningItem)
    assistant_msg = completed_resp.output[1]
    assert isinstance(assistant_msg, ResponseOutputMessage)
    # The refusal is the sole content part of the assistant message, at index 0.
    assert len(assistant_msg.content) == 1
    assert isinstance(assistant_msg.content[0], ResponseOutputRefusal)

    # The assistant message's output_index is 1 (after the reasoning item), and
    # every refusal event uses that output_index and content_index 0 — matching
    # the refusal's position in response.completed.
    added = [
        e
        for e in output_events
        if isinstance(e, ResponseContentPartAddedEvent)
        and isinstance(e.part, ResponseOutputRefusal)
    ]
    deltas = [e for e in output_events if isinstance(e, ResponseRefusalDeltaEvent)]
    assert len(added) == 1
    assert added[0].content_index == 0
    assert added[0].output_index == 1
    assert deltas and all(d.content_index == 0 and d.output_index == 1 for d in deltas)
    # The empty "" delta still opens no text part.
    assert "response.output_text.delta" not in [e.type for e in output_events]


class _ClosableChatStream:
    """A provider stream that records closes.

    This mirrors litellm's `CustomStreamWrapper`, which exposes `aclose` and no `close`.
    """

    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self._chunks = list(chunks)
        self.aclose_calls = 0

    def __aiter__(self) -> "_ClosableChatStream":
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _BlockingChatStream(_ClosableChatStream):
    """Yields its chunks and then blocks so the consumer can be cancelled mid-stream."""

    def __init__(self, chunks: list[ChatCompletionChunk], blocked: asyncio.Event) -> None:
        super().__init__(chunks)
        self._blocked = blocked

    async def __anext__(self) -> ChatCompletionChunk:
        if self._chunks:
            return self._chunks.pop(0)
        self._blocked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _SlowCloseChatStream(_ClosableChatStream):
    """Blocks in `aclose` until released, mirroring a provider close that waits on transport I/O."""

    def __init__(
        self,
        chunks: list[ChatCompletionChunk],
        blocked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(chunks)
        self._blocked = blocked
        self._release = release
        self.aclose_completed = 0

    async def __anext__(self) -> ChatCompletionChunk:
        if self._chunks:
            return self._chunks.pop(0)
        self._blocked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.aclose_calls += 1
        await self._release.wait()
        self.aclose_completed += 1


class _CloseSignalingChatStream(_ClosableChatStream):
    """Exhausts normally, then signals from `aclose` and blocks until released.

    Unlike `_SlowCloseChatStream` this does not block in `__anext__`, so the consumer reaches
    the cleanup `finally` on its own and a test can cancel while that close is in flight.
    """

    def __init__(
        self,
        chunks: list[ChatCompletionChunk],
        close_started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(chunks)
        self._close_started = close_started
        self._release = release
        self.aclose_completed = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self._close_started.set()
        await self._release.wait()
        self.aclose_completed += 1


class _FailingCloseChatStream(_ClosableChatStream):
    """Raises from `aclose` after recording the cleanup attempt."""

    async def aclose(self) -> None:
        self.aclose_calls += 1
        raise RuntimeError("close-failure")


def _text_chunk(text: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content=text))],
    )


def _patch_fetch_response(monkeypatch, provider_stream: _ClosableChatStream) -> None:
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
        return resp, provider_stream

    monkeypatch.setattr(LitellmModel, "_fetch_response", patched_fetch_response)


def _stream_response(model: Model) -> AsyncIterator[TResponseStreamEvent]:
    return model.stream_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_closes_provider_stream_on_explicit_aclose(monkeypatch) -> None:
    """Closing the returned generator early must release the provider stream."""
    provider_stream = _ClosableChatStream([_text_chunk("He"), _text_chunk("llo")])
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")

    stream_agen = cast(Any, _stream_response(model))
    async for _event in stream_agen:
        break
    await stream_agen.aclose()

    assert provider_stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_closes_provider_stream_on_normal_exhaustion(monkeypatch) -> None:
    """Consuming the stream to completion must also release the provider stream."""
    provider_stream = _ClosableChatStream([_text_chunk("He"), _text_chunk("llo")])
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")

    async for _event in _stream_response(model):
        pass

    assert provider_stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_ignores_close_failure_after_terminal_event(monkeypatch) -> None:
    """A completed response must remain successful when provider cleanup fails."""
    provider_stream = _FailingCloseChatStream([_text_chunk("Hello")])
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")

    output_events = [event async for event in _stream_response(model)]

    assert output_events[-1].type == "response.completed"
    assert provider_stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_ignores_close_failure_when_closed_at_terminal_event(
    monkeypatch,
) -> None:
    """Terminal state must be recorded before yielding the completed event."""
    provider_stream = _FailingCloseChatStream([_text_chunk("Hello")])
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")
    stream_agen = cast(Any, _stream_response(model))

    async for event in stream_agen:
        if event.type == "response.completed":
            break
    await stream_agen.aclose()

    assert provider_stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_propagates_close_failure_before_terminal_event(monkeypatch) -> None:
    """Cleanup failures before completion remain observable by the caller."""
    provider_stream = _FailingCloseChatStream([_text_chunk("Hello")])
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")
    stream_agen = cast(Any, _stream_response(model))

    first_event = await anext(stream_agen)
    assert first_event.type == "response.created"

    with pytest.raises(RuntimeError, match="close-failure"):
        await stream_agen.aclose()

    assert provider_stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_closes_provider_stream_after_cancellation(monkeypatch) -> None:
    """Cancelling the consumer unwinds into the `finally` and releases the provider stream.

    Closing the already-finished generator afterwards is a no-op, so the stream is closed once.
    """
    blocked = asyncio.Event()
    provider_stream = _BlockingChatStream([_text_chunk("He")], blocked)
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")

    stream_agen = cast(Any, _stream_response(model))

    async def consume() -> None:
        async for _event in stream_agen:
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()

    await stream_agen.aclose()

    assert provider_stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_does_not_block_cancellation_on_slow_close(monkeypatch) -> None:
    """A provider close that waits on transport I/O must not delay cancellation."""
    blocked = asyncio.Event()
    release = asyncio.Event()
    provider_stream = _SlowCloseChatStream([_text_chunk("He")], blocked, release)
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")

    stream_agen = cast(Any, _stream_response(model))

    async def consume() -> None:
        async for _event in stream_agen:
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert provider_stream.aclose_calls == 1
        assert provider_stream.aclose_completed == 0

        release.set()
        for _ in range(200):
            if provider_stream.aclose_completed == 1:
                break
            await asyncio.sleep(0.01)
        assert provider_stream.aclose_completed == 1
    finally:
        release.set()
        task.cancel()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_lets_in_flight_close_finish_after_cancellation(
    monkeypatch,
) -> None:
    """Cancelling during the cleanup `aclose` continues that close instead of abandoning it."""
    close_started = asyncio.Event()
    release = asyncio.Event()
    provider_stream = _CloseSignalingChatStream([_text_chunk("He")], close_started, release)
    _patch_fetch_response(monkeypatch, provider_stream)
    model = LitellmProvider().get_model("gpt-4")

    stream_agen = cast(Any, _stream_response(model))

    async def consume() -> None:
        async for _event in stream_agen:
            pass

    task = asyncio.create_task(consume())
    try:
        # The stream exhausts on its own, so the consumer reaches the cleanup `finally`
        # and suspends inside the provider close.
        await asyncio.wait_for(close_started.wait(), timeout=5)
        assert provider_stream.aclose_calls == 1
        assert provider_stream.aclose_completed == 0

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        # The cancelled consumer must not have started a second close.
        assert provider_stream.aclose_calls == 1
        assert provider_stream.aclose_completed == 0

        release.set()
        for _ in range(200):
            if provider_stream.aclose_completed == 1:
                break
            await asyncio.sleep(0.01)

        assert provider_stream.aclose_calls == 1
        assert provider_stream.aclose_completed == 1
    finally:
        release.set()
        task.cancel()

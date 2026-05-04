from collections.abc import AsyncIterator

import pytest
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
    ChoiceLogprobs,
)
from openai.types.chat.chat_completion_token_logprob import (
    ChatCompletionTokenLogprob,
    TopLogprob,
)
from openai.types.completion_usage import (
    CompletionTokensDetails,
    CompletionUsage,
    PromptTokensDetails,
)
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
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
        usage=CompletionUsage(
            completion_tokens=5,
            prompt_tokens=7,
            total_tokens=12,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=2),
            completion_tokens_details=CompletionTokensDetails(reasoning_tokens=3),
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
    assert completed_resp.usage.input_tokens_details.cached_tokens == 2
    assert completed_resp.usage.output_tokens_details.reasoning_tokens == 3


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_includes_logprobs(monkeypatch) -> None:
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(content="Hi"),
                logprobs=ChoiceLogprobs(
                    content=[
                        ChatCompletionTokenLogprob(
                            token="Hi",
                            logprob=-0.5,
                            bytes=[1],
                            top_logprobs=[TopLogprob(token="Hi", logprob=-0.5, bytes=[1])],
                        )
                    ]
                ),
            )
        ],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(content=" there"),
                logprobs=ChoiceLogprobs(
                    content=[
                        ChatCompletionTokenLogprob(
                            token=" there",
                            logprob=-0.25,
                            bytes=[2],
                            top_logprobs=[TopLogprob(token=" there", logprob=-0.25, bytes=[2])],
                        )
                    ]
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=5,
            prompt_tokens=7,
            total_tokens=12,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=2),
            completion_tokens_details=CompletionTokensDetails(reasoning_tokens=3),
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
        conversation_id=None,
        prompt=None,
    ):
        output_events.append(event)

    text_delta_events = [
        event for event in output_events if event.type == "response.output_text.delta"
    ]
    assert len(text_delta_events) == 2
    assert [lp.token for lp in text_delta_events[0].logprobs] == ["Hi"]
    assert [lp.token for lp in text_delta_events[1].logprobs] == [" there"]

    completed_event = next(event for event in output_events if event.type == "response.completed")
    assert isinstance(completed_event, ResponseCompletedEvent)
    completed_resp = completed_event.response
    assert isinstance(completed_resp.output[0], ResponseOutputMessage)
    text_part = completed_resp.output[0].content[0]
    assert isinstance(text_part, ResponseOutputText)
    assert text_part.text == "Hi there"
    assert text_part.logprobs is not None
    assert [lp.token for lp in text_part.logprobs] == ["Hi", " there"]


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
    # and arguments split across chunks (reflecting real OpenAI API behavior)
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
    Validate that `stream_response` emits function call arguments in real-time as they
    are received, not just at the end. This test simulates the real OpenAI API behavior
    where function name comes first, then arguments are streamed incrementally.
    """
    # Simulate realistic OpenAI API chunks: name first, then arguments incrementally
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        id="tool-call-123",
        function=ChoiceDeltaToolCallFunction(name="write_file", arguments=""),
        type="function",
    )
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='{"filename": "'),
        type="function",
    )
    tool_call_delta3 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='test.py", "content": "'),
        type="function",
    )
    tool_call_delta4 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='print(hello)"}'),
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
        conversation_id=None,
        prompt=None,
    ):
        output_events.append(event)

    # Extract events by type
    created_events = [e for e in output_events if e.type == "response.created"]
    output_item_added_events = [e for e in output_events if e.type == "response.output_item.added"]
    function_args_delta_events = [
        e for e in output_events if e.type == "response.function_call_arguments.delta"
    ]
    output_item_done_events = [e for e in output_events if e.type == "response.output_item.done"]
    completed_events = [e for e in output_events if e.type == "response.completed"]

    # Verify event structure
    assert len(created_events) == 1
    assert len(output_item_added_events) == 1
    assert len(function_args_delta_events) == 3  # Three incremental argument chunks
    assert len(output_item_done_events) == 1
    assert len(completed_events) == 1

    # Verify the function call started as soon as we had name and ID
    added_event = output_item_added_events[0]
    assert isinstance(added_event.item, ResponseFunctionToolCall)
    assert added_event.item.name == "write_file"
    assert added_event.item.call_id == "tool-call-123"
    assert added_event.item.arguments == ""  # Should be empty at start

    # Verify real-time argument streaming
    expected_deltas = ['{"filename": "', 'test.py", "content": "', 'print(hello)"}']
    for i, delta_event in enumerate(function_args_delta_events):
        assert delta_event.delta == expected_deltas[i]
        assert delta_event.item_id == "__fake_id__"  # FAKE_RESPONSES_ID
        assert delta_event.output_index == 0

    # Verify completion event has full arguments
    done_event = output_item_done_events[0]
    assert isinstance(done_event.item, ResponseFunctionToolCall)
    assert done_event.item.name == "write_file"
    assert done_event.item.arguments == '{"filename": "test.py", "content": "print(hello)"}'

    # Verify final response
    completed_event = completed_events[0]
    function_call_output = completed_event.response.output[0]
    assert isinstance(function_call_output, ResponseFunctionToolCall)
    assert function_call_output.name == "write_file"
    assert function_call_output.arguments == '{"filename": "test.py", "content": "print(hello)"}'


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_fallback_tool_calls_use_distinct_output_indexes(monkeypatch) -> None:
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(name="first_tool", arguments='{"a": 1}'),
        type="function",
    )
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=1,
        function=ChoiceDeltaToolCallFunction(name="second_tool", arguments='{"b": 2}'),
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
        for chunk in (chunk1, chunk2):
            yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        response = Response(
            id="resp-id",
            created_at=0,
            model="fake-model",
            object="response",
            output=[],
            tool_choice="none",
            tools=[],
            parallel_tool_calls=False,
        )
        return response, fake_stream()

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
        conversation_id=None,
        prompt=None,
    ):
        output_events.append(event)

    added_events = [event for event in output_events if event.type == "response.output_item.added"]
    delta_events = [
        event for event in output_events if event.type == "response.function_call_arguments.delta"
    ]
    done_events = [event for event in output_events if event.type == "response.output_item.done"]

    assert [event.output_index for event in added_events] == [0, 1]
    assert [event.output_index for event in delta_events] == [0, 1]
    assert [event.output_index for event in done_events] == [0, 1]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_mixed_tool_calls_use_final_output_indexes(monkeypatch) -> None:
    fallback_tool_call = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(name="first_tool", arguments='{"a": 1}'),
        type="function",
    )
    streamed_tool_call = ChoiceDeltaToolCall(
        index=1,
        id="second-tool-call-id",
        function=ChoiceDeltaToolCallFunction(name="second_tool", arguments='{"b": 2}'),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[fallback_tool_call]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_tool_call]))],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in (chunk1, chunk2):
            yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        response = Response(
            id="resp-id",
            created_at=0,
            model="fake-model",
            object="response",
            output=[],
            tool_choice="none",
            tools=[],
            parallel_tool_calls=False,
        )
        return response, fake_stream()

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
        conversation_id=None,
        prompt=None,
    ):
        output_events.append(event)

    added_events = [event for event in output_events if event.type == "response.output_item.added"]
    delta_events = [
        event for event in output_events if event.type == "response.function_call_arguments.delta"
    ]
    done_events = [event for event in output_events if event.type == "response.output_item.done"]
    completed_event = next(event for event in output_events if event.type == "response.completed")

    added_event_indexes = {}
    for event in added_events:
        assert isinstance(event.item, ResponseFunctionToolCall)
        added_event_indexes[event.item.name] = event.output_index

    done_event_indexes = {}
    for event in done_events:
        assert isinstance(event.item, ResponseFunctionToolCall)
        done_event_indexes[event.item.name] = event.output_index

    completed_output_names = []
    for output in completed_event.response.output:
        assert isinstance(output, ResponseFunctionToolCall)
        completed_output_names.append(output.name)

    assert added_event_indexes == {
        "first_tool": 0,
        "second_tool": 1,
    }
    assert {event.delta: event.output_index for event in delta_events} == {
        '{"a": 1}': 0,
        '{"b": 2}': 1,
    }
    assert done_event_indexes == {
        "first_tool": 0,
        "second_tool": 1,
    }
    assert completed_output_names == ["first_tool", "second_tool"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_text_before_mixed_tool_calls_offsets_tool_indexes(
    monkeypatch,
) -> None:
    fallback_tool_call = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(name="first_tool", arguments='{"a": 1}'),
        type="function",
    )
    streamed_tool_call = ChoiceDeltaToolCall(
        index=1,
        id="second-tool-call-id",
        function=ChoiceDeltaToolCallFunction(name="second_tool", arguments='{"b": 2}'),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="Preparing tools"))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[fallback_tool_call]))],
    )
    chunk3 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_tool_call]))],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in (chunk1, chunk2, chunk3):
            yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        response = Response(
            id="resp-id",
            created_at=0,
            model="fake-model",
            object="response",
            output=[],
            tool_choice="none",
            tools=[],
            parallel_tool_calls=False,
        )
        return response, fake_stream()

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
        conversation_id=None,
        prompt=None,
    ):
        output_events.append(event)

    added_events = [event for event in output_events if event.type == "response.output_item.added"]
    delta_events = [
        event for event in output_events if event.type == "response.function_call_arguments.delta"
    ]
    done_events = [event for event in output_events if event.type == "response.output_item.done"]
    completed_event = next(event for event in output_events if event.type == "response.completed")

    added_tool_indexes = {}
    for event in added_events:
        if isinstance(event.item, ResponseFunctionToolCall):
            added_tool_indexes[event.item.name] = event.output_index

    done_tool_indexes = {}
    for event in done_events:
        if isinstance(event.item, ResponseFunctionToolCall):
            done_tool_indexes[event.item.name] = event.output_index

    assert added_tool_indexes == {"first_tool": 1, "second_tool": 2}
    assert {event.delta: event.output_index for event in delta_events} == {
        '{"a": 1}': 1,
        '{"b": 2}': 2,
    }
    assert done_tool_indexes == {"first_tool": 1, "second_tool": 2}
    assert isinstance(completed_event.response.output[0], ResponseOutputMessage)
    completed_tool_outputs = completed_event.response.output[1:]
    completed_tool_names = []
    for output in completed_tool_outputs:
        assert isinstance(output, ResponseFunctionToolCall)
        completed_tool_names.append(output.name)
    assert completed_tool_names == ["first_tool", "second_tool"]

from collections.abc import AsyncIterator
from typing import Any, cast

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
    ResponseReasoningItem,
)

from agents.model_settings import ModelSettings
from agents.models.chatcmpl_stream_handler import ChatCmplStreamHandler
from agents.models.fake_id import is_fake_responses_id
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

    # Verify all events reference the same synthetic output message ID.
    msg_id = output_events[1].item.id  # response.output_item.added
    assert is_fake_responses_id(msg_id)
    assert output_events[2].item_id == msg_id  # response.content_part.added
    assert output_events[3].item_id == msg_id  # first response.output_text.delta
    assert output_events[4].item_id == msg_id  # second response.output_text.delta
    assert output_events[5].item_id == msg_id  # response.content_part.done
    assert output_events[6].item.id == msg_id  # response.output_item.done
    # Exactly one output message in the completed response, carrying the same ID.
    assert len(completed_resp.output) == 1
    assert completed_resp.output[0].id == msg_id


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

    # Verify all events reference the same synthetic output message ID.
    msg_id = output_events[1].item.id  # response.output_item.added
    assert is_fake_responses_id(msg_id)
    assert output_events[2].item_id == msg_id  # response.content_part.added
    assert output_events[3].item_id == msg_id  # first response.refusal.delta
    assert output_events[4].item_id == msg_id  # second response.refusal.delta
    assert output_events[5].item_id == msg_id  # response.content_part.done
    assert output_events[6].item.id == msg_id  # response.output_item.done
    # Exactly one output message in the completed response, carrying the same ID.
    assert len(completed_resp.output) == 1
    assert completed_resp.output[0].id == msg_id


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

    # Verify all function-call events share the same synthetic item ID.
    fn_id = output_events[1].item.id  # response.output_item.added
    assert is_fake_responses_id(fn_id)
    assert output_events[2].item_id == fn_id  # first response.function_call_arguments.delta
    assert output_events[3].item_id == fn_id  # second response.function_call_arguments.delta
    assert output_events[4].item.id == fn_id  # response.output_item.done
    # The single completed output also carries the same ID.
    completed_resp = output_events[5].response
    assert completed_resp.output[0].id == fn_id


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

    # Verify real-time argument streaming with a consistent ID across all events.
    fn_id = added_event.item.id
    assert is_fake_responses_id(fn_id)
    expected_deltas = ['{"filename": "', 'test.py", "content": "', 'print(hello)"}']
    for i, delta_event in enumerate(function_args_delta_events):
        assert delta_event.delta == expected_deltas[i]
        assert delta_event.item_id == fn_id  # every delta references the same item ID
        assert delta_event.output_index == 0

    # Verify completion event has full arguments and consistent ID.
    done_event = output_item_done_events[0]
    assert isinstance(done_event.item, ResponseFunctionToolCall)
    assert done_event.item.name == "write_file"
    assert done_event.item.arguments == '{"filename": "test.py", "content": "print(hello)"}'
    assert done_event.item.id == fn_id  # done event carries the same item ID

    # Verify final response and consistent ID.
    completed_event = completed_events[0]
    function_call_output = completed_event.response.output[0]
    assert isinstance(function_call_output, ResponseFunctionToolCall)
    assert function_call_output.name == "write_file"
    assert function_call_output.arguments == '{"filename": "test.py", "content": "print(hello)"}'
    assert function_call_output.id == fn_id  # completed response carries the same ID


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_multiple_tool_calls_have_distinct_ids(monkeypatch) -> None:
    """
    Validate that two parallel tool calls each receive their own unique synthetic ID,
    and that events for different calls never share an ID.
    """
    tool_call_0_chunk1 = ChoiceDeltaToolCall(
        index=0,
        id="call-id-0",
        function=ChoiceDeltaToolCallFunction(name="func_a", arguments=""),
        type="function",
    )
    tool_call_0_chunk2 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='{"x": 1}'),
        type="function",
    )
    tool_call_1_chunk1 = ChoiceDeltaToolCall(
        index=1,
        id="call-id-1",
        function=ChoiceDeltaToolCallFunction(name="func_b", arguments=""),
        type="function",
    )
    tool_call_1_chunk2 = ChoiceDeltaToolCall(
        index=1,
        function=ChoiceDeltaToolCallFunction(arguments='{"y": 2}'),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(tool_calls=[tool_call_0_chunk1, tool_call_1_chunk1]),
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
                delta=ChoiceDelta(tool_calls=[tool_call_0_chunk2, tool_call_1_chunk2]),
            )
        ],
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

    added_events = [e for e in output_events if e.type == "response.output_item.added"]
    done_events = [e for e in output_events if e.type == "response.output_item.done"]
    delta_events = [e for e in output_events if e.type == "response.function_call_arguments.delta"]

    assert len(added_events) == 2
    assert len(done_events) == 2
    assert len(delta_events) == 2

    # Map function name → synthetic item ID from the added events.
    id_by_name: dict[str, str] = {e.item.name: e.item.id for e in added_events}
    assert set(id_by_name.keys()) == {"func_a", "func_b"}
    fn_a_id = id_by_name["func_a"]
    fn_b_id = id_by_name["func_b"]

    # Both IDs must be synthetic and distinct from each other.
    assert is_fake_responses_id(fn_a_id)
    assert is_fake_responses_id(fn_b_id)
    assert fn_a_id != fn_b_id

    # Each done event must carry its own call's ID.
    done_by_name: dict[str, str] = {e.item.name: e.item.id for e in done_events}
    assert done_by_name["func_a"] == fn_a_id
    assert done_by_name["func_b"] == fn_b_id

    # All argument deltas must reference one of the two call IDs (both must appear).
    delta_item_ids = {e.item_id for e in delta_events}
    assert delta_item_ids == {fn_a_id, fn_b_id}

    # The completed response output carries both IDs.
    completed_resp = next(e for e in output_events if e.type == "response.completed").response
    completed_ids = {
        item.id for item in completed_resp.output if isinstance(item, ResponseFunctionToolCall)
    }
    assert completed_ids == {fn_a_id, fn_b_id}


@pytest.mark.asyncio
async def test_stream_response_reasoning_and_text_have_distinct_ids() -> None:
    """
    Validate that a stream containing both a reasoning item and a text message assigns
    distinct synthetic IDs to each, and that all events for each item consistently
    use that item's ID.
    """
    # Chunk 1: reasoning content delta (provider extension field).
    delta1 = ChoiceDelta()
    cast(Any, delta1).reasoning_content = "thinking about it"
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=delta1)],
    )
    # Chunk 2: regular text content delta.
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="Hello"))],
        usage=CompletionUsage(completion_tokens=2, prompt_tokens=2, total_tokens=4),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for c in (chunk1, chunk2):
            yield c

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
    events = []
    async for event in ChatCmplStreamHandler.handle_stream(
        response,
        fake_stream(),  # type: ignore[arg-type]
    ):
        events.append(event)

    added_events = [e for e in events if e.type == "response.output_item.added"]
    reasoning_added = next(e for e in added_events if e.item.type == "reasoning")
    message_added = next(e for e in added_events if e.item.type == "message")

    reasoning_id = reasoning_added.item.id
    message_id = message_added.item.id

    # Both IDs must be synthetic and distinct from each other.
    assert is_fake_responses_id(reasoning_id)
    assert is_fake_responses_id(message_id)
    assert reasoning_id != message_id

    # All reasoning delta events reference reasoning_id.
    reasoning_delta_events = [
        e
        for e in events
        if e.type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta")
    ]
    assert reasoning_delta_events, "expected at least one reasoning delta"
    for e in reasoning_delta_events:
        assert e.item_id == reasoning_id

    # All text delta events reference message_id.
    text_delta_events = [e for e in events if e.type == "response.output_text.delta"]
    assert text_delta_events
    for e in text_delta_events:
        assert e.item_id == message_id

    # Content part added/done events reference message_id.
    content_part_events = [
        e for e in events if e.type in ("response.content_part.added", "response.content_part.done")
    ]
    for e in content_part_events:
        assert e.item_id == message_id

    # The completed response carries both items with matching IDs.
    completed = next(e for e in events if e.type == "response.completed").response
    reasoning_out = next(
        item for item in completed.output if isinstance(item, ResponseReasoningItem)
    )
    message_out = next(item for item in completed.output if isinstance(item, ResponseOutputMessage))
    assert reasoning_out.id == reasoning_id
    assert message_out.id == message_id

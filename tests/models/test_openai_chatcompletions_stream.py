import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from openai.types.chat.chat_completion import ChatCompletion, Choice as ChatCompletionChoice
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
    ChoiceLogprobs,
)
from openai.types.chat.chat_completion_message import ChatCompletionMessage
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

from agents import Agent, Runner, function_tool, trace
from agents.exceptions import AgentsException, ModelBehaviorError, UserError
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter
from agents.models.chatcmpl_stream_handler import (
    ChatCmplStreamHandler,
    Part,
    SequenceNumber,
    StreamingState,
    _BufferedToolCall,
    _merge_buffered_metadata,
    _StreamOutputLayout,
)
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider
from tests.testing_processor import fetch_ordered_spans
from tests.utils.simple_session import SimpleListSession


async def _empty_chat_completion_stream() -> AsyncIterator[ChatCompletionChunk]:
    chunks: list[ChatCompletionChunk] = []
    for chunk in chunks:
        yield chunk


def _empty_response() -> Response:
    return Response(
        id="resp-id",
        created_at=0,
        model="fake-model",
        object="response",
        output=[],
        tool_choice="none",
        tools=[],
        parallel_tool_calls=False,
    )


async def _completion_stream(
    *chunks: ChatCompletionChunk,
) -> AsyncIterator[ChatCompletionChunk]:
    for chunk in chunks:
        yield chunk


async def _collect_handler_events(
    *chunks: ChatCompletionChunk,
    model: str | None = None,
) -> list[Any]:
    return [
        event
        async for event in ChatCmplStreamHandler.handle_stream(
            _empty_response(), cast(Any, _completion_stream(*chunks)), model=model
        )
    ]


async def _collect_buffered_tool_call_chunks(
    *chunks: ChatCompletionChunk,
) -> list[ChatCompletionChunk]:
    return [
        chunk
        async for chunk in ChatCmplStreamHandler.buffer_tool_call_stream(
            _completion_stream(*chunks)
        )
    ]


def _url_citation(
    url: str = "https://example.com/weather",
    title: str = "Weather",
    start_index: int = 0,
    end_index: int = 22,
) -> dict[str, Any]:
    return {
        "type": "url_citation",
        "url_citation": {
            "start_index": start_index,
            "end_index": end_index,
            "url": url,
            "title": title,
        },
    }


def _annotated_chunk(
    delta_payload: dict[str, Any], finish_reason: str | None = None
) -> ChatCompletionChunk:
    # `annotations` is not a declared field on ChoiceDelta, so it is built through
    # model_validate to reach the object the same way a provider payload does.
    return ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta.model_validate(delta_payload),
                finish_reason=cast(Any, finish_reason),
            )
        ],
    )


def _streamed_annotations(events: list[Any]) -> list[dict[str, Any]]:
    completed = cast(ResponseCompletedEvent, events[-1])
    message = cast(ResponseOutputMessage, completed.response.output[0])
    text_part = cast(ResponseOutputText, message.content[0])
    return [annotation.model_dump() for annotation in text_part.annotations]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("use_dictionary", [False, True], ids=["model-settings", "dictionary"])
async def test_stream_response_forwards_dictionary_agent_model_settings(
    use_dictionary: bool,
) -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="gpt-5.4-mini",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(role="assistant", content="ok"),
                finish_reason="stop",
            )
        ],
    )

    class DummyCompletions:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def create(self, **kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
            self.kwargs = kwargs
            return _completion_stream(chunk)

    class DummyClient:
        def __init__(self, completions: DummyCompletions) -> None:
            self.chat = type("_Chat", (), {"completions": completions})()
            self.base_url = httpx.URL("https://api.openai.com/v1/")

    completions = DummyCompletions()
    model = OpenAIChatCompletionsModel(
        model="gpt-5.4-mini", openai_client=cast(Any, DummyClient(completions))
    )
    settings: dict[str, Any] = {
        "reasoning": {"effort": "low"},
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        "prompt_cache_retention": "24h",
        "verbosity": "low",
        "store": False,
        "temperature": 0.0,
        "top_p": 1.0,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "max_tokens": 64,
        "parallel_tool_calls": False,
        "include_usage": False,
    }
    agent = Agent(
        name="test",
        model=model,
        model_settings=settings if use_dictionary else ModelSettings(**settings),
    )

    events = [
        event
        async for event in model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=agent.model_settings,
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]

    assert any(event.type == "response.completed" for event in events)
    assert completions.kwargs["reasoning_effort"] == "low"
    assert completions.kwargs["prompt_cache_options"] == settings["prompt_cache_options"]
    assert completions.kwargs["prompt_cache_retention"] == "24h"
    assert completions.kwargs["verbosity"] == "low"
    assert completions.kwargs["store"] is False
    assert completions.kwargs["temperature"] == 0.0
    assert completions.kwargs["top_p"] == 1.0
    assert completions.kwargs["frequency_penalty"] == 0.0
    assert completions.kwargs["presence_penalty"] == 0.0
    assert completions.kwargs["max_tokens"] == 64
    assert completions.kwargs["parallel_tool_calls"] is False
    assert completions.kwargs["stream"] is True
    assert completions.kwargs["stream_options"] == {"include_usage": False}


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
            prompt_tokens_details=PromptTokensDetails.model_validate(
                {"cached_tokens": 2, "cache_write_tokens": 4}
            ),
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
    assert getattr(completed_resp.usage.input_tokens_details, "cache_write_tokens", None) == 4
    assert completed_resp.usage.output_tokens_details.reasoning_tokens == 3


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_close_closes_provider_stream_with_async_close(
    monkeypatch,
) -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="Hi"))],
    )

    class ClosableChatStream:
        def __init__(self) -> None:
            self._yielded = False
            self.close_calls = 0

        def __aiter__(self) -> "ClosableChatStream":
            return self

        async def __anext__(self) -> ChatCompletionChunk:
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return chunk

        async def close(self) -> None:
            self.close_calls += 1

    provider_stream = ClosableChatStream()

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), provider_stream

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")

    stream = model.stream_response(
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
    stream_agen = cast(Any, stream)

    event = await stream_agen.__anext__()
    assert event.type == "response.created"

    await stream_agen.aclose()

    assert provider_stream.close_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_lets_in_flight_close_finish_after_cancellation(
    monkeypatch,
) -> None:
    """Cancelling during the cleanup `aclose` continues that close instead of abandoning it."""
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="Hi"))],
    )

    class CloseSignalingChatStream:
        """Exhausts normally, then signals from `aclose` and blocks until released."""

        def __init__(self, close_started: asyncio.Event, release: asyncio.Event) -> None:
            self._yielded = False
            self._close_started = close_started
            self._release = release
            self.aclose_calls = 0
            self.aclose_completed = 0

        def __aiter__(self) -> "CloseSignalingChatStream":
            return self

        async def __anext__(self) -> ChatCompletionChunk:
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return chunk

        async def aclose(self) -> None:
            self.aclose_calls += 1
            self._close_started.set()
            await self._release.wait()
            self.aclose_completed += 1

    close_started = asyncio.Event()
    release = asyncio.Event()
    provider_stream = CloseSignalingChatStream(close_started, release)

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), provider_stream

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")

    stream_agen = cast(
        Any,
        model.stream_response(
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
        ),
    )

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


@pytest.mark.asyncio
async def test_stream_handler_filters_multiple_choices_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="openai.agents")
    chunks = [
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=1, delta=ChoiceDelta(content="ignored-first"))],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[
                Choice(index=0, delta=ChoiceDelta(content="kept")),
                Choice(index=1, delta=ChoiceDelta(content="ignored-second")),
            ],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=2, delta=ChoiceDelta(content="ignored-third"))],
            usage=CompletionUsage(completion_tokens=1, prompt_tokens=2, total_tokens=3),
        ),
    ]

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in chunks:
            yield chunk

    events = [
        event
        async for event in ChatCmplStreamHandler.handle_stream(
            _empty_response(), cast(Any, fake_stream())
        )
    ]

    text_delta_events = [event for event in events if event.type == "response.output_text.delta"]
    assert [event.delta for event in text_delta_events] == ["kept"]
    completed_event = next(event for event in events if event.type == "response.completed")
    assert isinstance(completed_event, ResponseCompletedEvent)
    assert isinstance(completed_event.response.output[0], ResponseOutputMessage)
    text_part = completed_event.response.output[0].content[0]
    assert isinstance(text_part, ResponseOutputText)
    assert text_part.text == "kept"
    assert completed_event.response.usage
    assert completed_event.response.usage.total_tokens == 3

    choice_warnings = [
        record
        for record in caplog.records
        if "multiple choices or nonzero choice indexes" in record.getMessage()
    ]
    assert len(choice_warnings) == 1


@pytest.mark.asyncio
async def test_stream_handler_keeps_empty_choice_usage_chunks() -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[],
        usage=CompletionUsage.model_validate(
            {
                "completion_tokens": 1,
                "prompt_tokens": 2,
                "total_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
        ),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    events = [
        event
        async for event in ChatCmplStreamHandler.handle_stream(
            _empty_response(), cast(Any, fake_stream()), preserve_raw_usage=True
        )
    ]

    assert [event.type for event in events] == ["response.created", "response.completed"]
    completed_event = events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    assert completed_event.response.output == []
    assert completed_event.response.usage
    assert completed_event.response.usage.total_tokens == 3
    assert cast(Any, completed_event.response)._agents_sdk_raw_usage == {
        "completion_tokens": 1,
        "prompt_tokens": 2,
        "total_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 0},
    }


@pytest.mark.asyncio
async def test_stream_handler_rejects_multiple_choices_in_strict_mode() -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(index=0, delta=ChoiceDelta(content="first")),
            Choice(index=1, delta=ChoiceDelta(content="second")),
        ],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    with pytest.raises(UserError, match="multiple choices or nonzero"):
        async for _ in ChatCmplStreamHandler.handle_stream(
            _empty_response(), cast(Any, fake_stream()), strict_feature_validation=True
        ):
            pass


@pytest.mark.asyncio
async def test_stream_handler_rejects_nonzero_choice_index_in_strict_mode() -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=1, delta=ChoiceDelta(content="second"))],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    with pytest.raises(UserError, match="multiple choices or nonzero"):
        async for _ in ChatCmplStreamHandler.handle_stream(
            _empty_response(), cast(Any, fake_stream()), strict_feature_validation=True
        ):
            pass


@pytest.mark.asyncio
async def test_buffer_tool_call_stream_merges_provider_metadata() -> None:
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments='{"a":'),
        type="function",
    )
    tool_call_delta1_any = cast(Any, tool_call_delta1)
    tool_call_delta1_any.provider_specific_fields = {
        "nested": {"keep": "provider", "stable": {"value": 1}},
        "replace": "old",
    }
    tool_call_delta1_any.extra_content = {
        "google": {"thought_signature": "sig-1", "stable": {"value": "kept"}}
    }
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=0,
        id=None,
        function=ChoiceDeltaToolCallFunction(name=None, arguments="1}"),
        type="function",
    )
    tool_call_delta2_any = cast(Any, tool_call_delta2)
    tool_call_delta2_any.provider_specific_fields = {
        "nested": {"stable": {}, "new": "provider"},
        "replace": "new",
    }
    tool_call_delta2_any.extra_content = {"google": {"stable": {}, "new": "extra"}}
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

    buffered_chunks = await _collect_buffered_tool_call_chunks(chunk1, chunk2)

    assert len(buffered_chunks) == 1
    buffered_delta = buffered_chunks[0].choices[0].delta
    assert buffered_delta.tool_calls
    buffered_tool_call = buffered_delta.tool_calls[0]
    assert buffered_tool_call.function
    assert buffered_tool_call.function.arguments == '{"a":1}'
    assert cast(Any, buffered_tool_call).provider_specific_fields == {
        "nested": {"keep": "provider", "stable": {"value": 1}, "new": "provider"},
        "replace": "new",
    }
    assert cast(Any, buffered_tool_call).extra_content == {
        "google": {"thought_signature": "sig-1", "stable": {"value": "kept"}, "new": "extra"}
    }


def test_stream_handler_internal_part_stores_text_and_type() -> None:
    part = Part(text="hello", type="output_text")

    assert part.text == "hello"
    assert part.type == "output_text"


def test_merge_buffered_metadata_keeps_existing_scalar_when_empty_dict_arrives() -> None:
    merged = _merge_buffered_metadata(
        {"stable": "keep-me"},
        {"stable": {}, "new": {}},
    )

    assert merged == {"stable": "keep-me", "new": {}}


def test_stream_output_layout_rejects_unknown_function_call_index() -> None:
    layout = _StreamOutputLayout()

    with pytest.raises(KeyError, match="Function call index 9 has not been tracked"):
        layout.function_call_output_index(StreamingState(), 9)


@pytest.mark.parametrize(
    ("buffered_call", "message"),
    [
        (
            _BufferedToolCall(index=0, name="my_func"),
            "without a tool call id",
        ),
        (
            _BufferedToolCall(index=0, call_id="tool-id"),
            "without a function name",
        ),
    ],
)
def test_buffered_tool_call_delta_requires_id_and_name(
    buffered_call: _BufferedToolCall,
    message: str,
) -> None:
    with pytest.raises(ModelBehaviorError, match=message):
        ChatCmplStreamHandler._buffered_tool_call_delta(buffered_call)


def test_function_call_item_omits_provider_data_when_absent() -> None:
    function_call = ResponseFunctionToolCall(
        id="fake-id",
        call_id="call-id",
        arguments="",
        name="my_func",
        type="function_call",
    )

    item = ChatCmplStreamHandler._function_call_item(
        StreamingState(),
        function_call,
        arguments="{}",
    )

    assert item.arguments == "{}"
    assert "provider_data" not in item.model_dump()


def test_finish_reasoning_summary_part_clears_invalid_active_index() -> None:
    reasoning_item = ResponseReasoningItem(id="fake-id", summary=[], type="reasoning")
    state = StreamingState(
        reasoning_content_index_and_output=(0, reasoning_item),
        active_reasoning_summary_index=0,
    )

    events = list(ChatCmplStreamHandler._finish_reasoning_summary_part(state, SequenceNumber()))

    assert events == []
    assert state.active_reasoning_summary_index is None


@pytest.mark.asyncio
async def test_audio_delta_raises_like_the_sync_path() -> None:
    """Audio output must fail loudly on the streamed path, matching the sync converter."""
    chunk = _annotated_chunk({"content": "partial", "audio": {"id": "audio-1", "transcript": "hi"}})

    with pytest.raises(AgentsException, match="Audio is not currently supported"):
        await _collect_handler_events(chunk)


@pytest.mark.asyncio
async def test_buffered_audio_only_delta_raises_instead_of_completing_empty() -> None:
    """Tool-call buffering must not swallow an audio-only delta into a silent empty run."""
    audio_chunk = _annotated_chunk({"audio": {"id": "audio-1", "transcript": "hi"}})

    buffered = ChatCmplStreamHandler.buffer_tool_call_stream(_completion_stream(audio_chunk))
    with pytest.raises(AgentsException, match="Audio is not currently supported"):
        async for _ in ChatCmplStreamHandler.handle_stream(_empty_response(), cast(Any, buffered)):
            pass


@pytest.mark.asyncio
async def test_buffer_tool_call_stream_preserves_empty_choice_chunks() -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[],
    )

    buffered_chunks = await _collect_buffered_tool_call_chunks(chunk)

    assert buffered_chunks == [chunk]


@pytest.mark.asyncio
async def test_buffer_tool_call_stream_keeps_passthrough_index_passthrough() -> None:
    custom_tool_call_delta = ChoiceDeltaToolCall.model_construct(
        index=0,
        id="custom-id",
        type="custom",
    )
    function_tool_call_delta = ChoiceDeltaToolCall(
        index=0,
        id="function-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments="{}"),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[custom_tool_call_delta]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[function_tool_call_delta]))],
    )

    buffered_chunks = await _collect_buffered_tool_call_chunks(chunk1, chunk2)

    assert len(buffered_chunks) == 2
    assert buffered_chunks[0].choices[0].delta.tool_calls == [custom_tool_call_delta]
    assert buffered_chunks[1].choices[0].delta.tool_calls == [function_tool_call_delta]


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (None, False),
        (ChoiceDelta(), False),
        (ChoiceDelta(content="text"), True),
        (ChoiceDelta.model_construct(refusal="blocked"), True),
        (ChoiceDelta.model_construct(reasoning_content="summary"), True),
        (ChoiceDelta.model_construct(reasoning="scratchpad"), True),
        (ChoiceDelta.model_construct(thinking_blocks=[{"thinking": "hidden"}]), True),
        (ChoiceDelta.model_construct(audio={"id": "audio-1"}), True),
    ],
)
def test_stream_handler_detects_passthrough_delta_shapes(
    delta: ChoiceDelta | None,
    expected: bool,
) -> None:
    assert ChatCmplStreamHandler._delta_has_passthrough_output(delta) is expected


@pytest.mark.asyncio
async def test_stream_handler_ignores_choice_without_delta() -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice.model_construct(index=0, delta=None)],
    )

    events = await _collect_handler_events(chunk)

    assert [event.type for event in events] == ["response.created", "response.completed"]
    completed_event = events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    assert completed_event.response.output == []


@pytest.mark.asyncio
async def test_stream_handler_converts_third_party_reasoning_text() -> None:
    reasoning_delta1 = ChoiceDelta.model_construct(reasoning="think ")
    reasoning_delta2 = ChoiceDelta.model_construct(reasoning="hard")
    chunks = [
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=reasoning_delta1)],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=reasoning_delta2)],
        ),
    ]

    events = await _collect_handler_events(*chunks, model="third-party")

    reasoning_delta_events = [
        event for event in events if event.type == "response.reasoning_text.delta"
    ]
    assert [event.delta for event in reasoning_delta_events] == ["think ", "hard"]

    reasoning_done_event = next(
        event
        for event in events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseReasoningItem)
    )
    reasoning_done_item = cast(ResponseReasoningItem, reasoning_done_event.item)
    assert reasoning_done_item.content
    assert cast(Any, reasoning_done_item.content[0]).text == "think hard"

    completed_event = next(event for event in events if event.type == "response.completed")
    assert isinstance(completed_event, ResponseCompletedEvent)
    completed_reasoning_item = completed_event.response.output[0]
    assert isinstance(completed_reasoning_item, ResponseReasoningItem)
    assert completed_reasoning_item.content
    assert cast(Any, completed_reasoning_item.content[0]).text == "think hard"
    assert completed_reasoning_item.model_dump().get("provider_data") == {
        "model": "third-party",
        "response_id": "chunk-id",
    }


@pytest.mark.asyncio
async def test_stream_handler_preserves_thinking_blocks_with_reasoning_summary() -> None:
    delta = ChoiceDelta.model_construct(
        reasoning_content="summary",
        thinking_blocks=[
            {"thinking": "hidden one ", "signature": "sig-1"},
            {"thinking": "hidden two", "signature": "sig-2"},
        ],
    )
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=delta)],
    )

    events = await _collect_handler_events(chunk)

    completed_event = next(event for event in events if event.type == "response.completed")
    reasoning_item = completed_event.response.output[0]
    assert isinstance(reasoning_item, ResponseReasoningItem)
    assert reasoning_item.summary[0].text == "summary"
    assert reasoning_item.content
    # Preserve the released normalized projection while provider_data retains exact blocks.
    assert [cast(Any, part).text for part in reasoning_item.content] == ["hidden one hidden two"]
    assert reasoning_item.encrypted_content == "sig-2"
    assert cast(Any, reasoning_item).provider_data["thinking_blocks"] == [
        {"type": "thinking", "thinking": "hidden one ", "signature": "sig-1"},
        {"type": "thinking", "thinking": "hidden two", "signature": "sig-2"},
    ]


def _thinking_chunk(**delta_kwargs: Any) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="anthropic/claude-4-opus",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta.model_construct(**delta_kwargs))],
    )


@pytest.mark.asyncio
async def test_stream_handler_segments_thinking_blocks_at_signature_deltas() -> None:
    """A streamed block ends at its signature_delta, so each block keeps its own signature.

    Anthropic streams a thinking block as a run of `thinking_delta` chunks terminated by one
    `signature_delta`. Accumulating the text into a single scalar merged interleaved blocks and
    kept only the last signature, which cannot be verified against the merged text on replay.
    """
    events = await _collect_handler_events(
        _thinking_chunk(reasoning_content="summary"),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "step ", "signature": ""}]
        ),
        _thinking_chunk(thinking_blocks=[{"type": "thinking", "thinking": "one", "signature": ""}]),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "SIG-1"}]
        ),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "step two", "signature": ""}]
        ),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "SIG-2"}]
        ),
    )

    completed_event = next(event for event in events if event.type == "response.completed")
    reasoning_item = completed_event.response.output[0]
    assert isinstance(reasoning_item, ResponseReasoningItem)
    assert cast(Any, reasoning_item).provider_data["thinking_blocks"] == [
        {"type": "thinking", "thinking": "step one", "signature": "SIG-1"},
        {"type": "thinking", "thinking": "step two", "signature": "SIG-2"},
    ]
    assert [cast(Any, part).text for part in (reasoning_item.content or [])] == ["step onestep two"]
    assert reasoning_item.encrypted_content == "SIG-2"


@pytest.mark.asyncio
async def test_stream_handler_finalizes_thinking_blocks_before_item_done() -> None:
    """The done event must carry the finalized item at the time it is yielded."""
    snapshots = [
        event.model_dump()
        async for event in ChatCmplStreamHandler.handle_stream(
            _empty_response(),
            cast(
                Any,
                _completion_stream(
                    _thinking_chunk(reasoning_content="summary"),
                    _thinking_chunk(
                        thinking_blocks=[
                            {"type": "thinking", "thinking": "hidden", "signature": ""}
                        ]
                    ),
                    _thinking_chunk(
                        thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "SIG"}]
                    ),
                ),
            ),
        )
    ]

    done_event = next(
        event
        for event in snapshots
        if event["type"] == "response.output_item.done" and event["item"]["type"] == "reasoning"
    )
    completed_event = next(event for event in snapshots if event["type"] == "response.completed")
    completed_reasoning_item = completed_event["response"]["output"][0]

    assert done_event["item"] == completed_reasoning_item
    assert done_event["item"]["provider_data"]["thinking_blocks"] == [
        {"type": "thinking", "thinking": "hidden", "signature": "SIG"}
    ]


@pytest.mark.asyncio
async def test_stream_handler_preserves_redacted_thinking_without_summary() -> None:
    """LiteLLM emits redacted blocks without reasoning_content, so they need their own item."""
    events = await _collect_handler_events(
        _thinking_chunk(thinking_blocks=[{"type": "redacted_thinking", "data": "BLOB"}]),
        _thinking_chunk(content="visible answer"),
    )

    reasoning_done_event = next(
        event
        for event in events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseReasoningItem)
    )
    assert reasoning_done_event.output_index == 0
    assert cast(Any, reasoning_done_event.item).provider_data["thinking_blocks"] == [
        {"type": "redacted_thinking", "data": "BLOB"}
    ]

    completed_event = next(event for event in events if event.type == "response.completed")
    assert [item.type for item in completed_event.response.output] == ["reasoning", "message"]
    assert cast(Any, completed_event.response.output[0]).provider_data["thinking_blocks"] == [
        {"type": "redacted_thinking", "data": "BLOB"}
    ]

    text_events = [
        event
        for event in events
        if event.type
        in {
            "response.content_part.added",
            "response.output_text.delta",
            "response.content_part.done",
        }
    ]
    assert text_events
    assert all(event.output_index == 1 and event.content_index == 0 for event in text_events)


@pytest.mark.asyncio
async def test_stream_handler_places_refusal_after_redacted_thinking_item() -> None:
    events = await _collect_handler_events(
        _thinking_chunk(thinking_blocks=[{"type": "redacted_thinking", "data": "BLOB"}]),
        _thinking_chunk(refusal="blocked"),
    )

    refusal_events = [
        event
        for event in events
        if event.type
        in {
            "response.content_part.added",
            "response.refusal.delta",
            "response.content_part.done",
        }
    ]
    assert refusal_events
    assert all(event.output_index == 1 and event.content_index == 0 for event in refusal_events)

    completed_event = next(event for event in events if event.type == "response.completed")
    message = completed_event.response.output[1]
    assert isinstance(message, ResponseOutputMessage)
    assert message.content == [ResponseOutputRefusal(refusal="blocked", type="refusal")]


@pytest.mark.asyncio
async def test_stream_handler_keeps_redacted_block_metadata_opaque() -> None:
    """Additional redacted-block keys must not become normalized thinking fields."""
    redacted_block = {
        "type": "redacted_thinking",
        "data": "BLOB",
        "thinking": {"opaque": True},
        "signature": {"opaque": True},
    }

    events = await _collect_handler_events(_thinking_chunk(thinking_blocks=[redacted_block]))

    done_event = next(
        event
        for event in events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseReasoningItem)
    )
    reasoning_item = cast(ResponseReasoningItem, done_event.item)
    assert cast(Any, reasoning_item).provider_data["thinking_blocks"] == [redacted_block]
    assert reasoning_item.content is None
    assert reasoning_item.encrypted_content is None


@pytest.mark.asyncio
async def test_stream_handler_emits_complete_signed_thinking_only_lifecycle() -> None:
    """Signed thinking without a summary must produce one complete reasoning item."""
    snapshots = [
        event.model_dump()
        async for event in ChatCmplStreamHandler.handle_stream(
            _empty_response(),
            cast(
                Any,
                _completion_stream(
                    _thinking_chunk(
                        thinking_blocks=[
                            {"type": "thinking", "thinking": "hidden", "signature": ""}
                        ]
                    ),
                    _thinking_chunk(
                        thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "SIG"}]
                    ),
                ),
            ),
        )
    ]

    assert [event["type"] for event in snapshots] == [
        "response.created",
        "response.output_item.added",
        "response.reasoning_text.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert snapshots[1]["output_index"] == 0
    assert snapshots[2]["output_index"] == 0
    assert snapshots[2]["text"] == "hidden"

    done_item = snapshots[3]["item"]
    assert snapshots[3]["output_index"] == 0
    assert done_item["content"] == [{"text": "hidden", "type": "reasoning_text"}]
    assert done_item["encrypted_content"] == "SIG"
    assert done_item["provider_data"]["thinking_blocks"] == [
        {"type": "thinking", "thinking": "hidden", "signature": "SIG"}
    ]
    assert snapshots[4]["response"]["output"] == [done_item]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("thinking", {"opaque": True}),
        ("signature", {"opaque": True}),
        ("thinking", None),
        ("signature", None),
    ],
)
async def test_stream_handler_rejects_non_string_thinking_fields(
    field_name: str, field_value: Any
) -> None:
    block: dict[str, Any] = {"type": "thinking", "thinking": "", "signature": ""}
    block[field_name] = field_value

    with pytest.raises(
        ModelBehaviorError,
        match=rf"Expected streamed thinking block field '{field_name}' to be a string, "
        rf"got {type(field_value).__name__}",
    ):
        await _collect_handler_events(_thinking_chunk(thinking_blocks=[block]))


@pytest.mark.asyncio
async def test_stream_handler_ignores_explicit_null_thinking_block_type() -> None:
    events = await _collect_handler_events(
        _thinking_chunk(
            thinking_blocks=[
                {"type": None, "thinking": "hidden", "signature": "SIG"},
            ]
        )
    )

    completed_event = next(event for event in events if event.type == "response.completed")
    assert completed_event.response.output == []


@pytest.mark.asyncio
async def test_stream_handler_preserves_redacted_thinking_blocks() -> None:
    """A redacted_thinking block carries neither text nor signature and was dropped entirely.

    Anthropic requires redacted blocks to be replayed unmodified during tool-use continuation,
    and the normalized content/encrypted_content pair cannot represent them.
    """
    events = await _collect_handler_events(
        _thinking_chunk(reasoning_content="summary"),
        _thinking_chunk(thinking_blocks=[{"type": "redacted_thinking", "data": "REDACTED-BLOB"}]),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "visible", "signature": ""}]
        ),
        _thinking_chunk(thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "SIG"}]),
    )

    completed_event = next(event for event in events if event.type == "response.completed")
    reasoning_item = completed_event.response.output[0]
    assert isinstance(reasoning_item, ResponseReasoningItem)
    assert cast(Any, reasoning_item).provider_data["thinking_blocks"] == [
        {"type": "redacted_thinking", "data": "REDACTED-BLOB"},
        {"type": "thinking", "thinking": "visible", "signature": "SIG"},
    ]
    # The redacted block contributes no reasoning_text and no signature.
    assert [cast(Any, part).text for part in (reasoning_item.content or [])] == ["visible"]
    assert reasoning_item.encrypted_content == "SIG"


@pytest.mark.asyncio
async def test_streamed_thinking_blocks_replay_through_the_converter() -> None:
    """End-to-end: a streamed Anthropic response must replay as the same ordered blocks.

    This covers the streaming counterpart of the non-streaming replay path, so a signed block
    survives item serialization and reaches the outbound request unchanged.
    """
    events = await _collect_handler_events(
        _thinking_chunk(reasoning_content="summary"),
        _thinking_chunk(
            thinking_blocks=[
                {
                    "type": "thinking",
                    "thinking": "alpha",
                    "signature": "",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        ),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "SIG-A"}]
        ),
        _thinking_chunk(thinking_blocks=[{"type": "redacted_thinking", "data": "BLOB"}]),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "beta", "signature": ""}]
        ),
        _thinking_chunk(
            thinking_blocks=[{"type": "thinking", "thinking": "", "signature": "SIG-B"}]
        ),
        _thinking_chunk(content="visible answer"),
    )

    completed_event = next(event for event in events if event.type == "response.completed")
    stored_items = [item.model_dump() for item in completed_event.response.output]

    messages = Converter.items_to_messages(
        cast(Any, stored_items),
        model="anthropic/claude-4-opus",
        preserve_thinking_blocks=True,
    )

    assistant_messages = [msg for msg in messages if msg.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    # The complete sequence replays through LiteLLM's native assistant thinking_blocks field,
    # which round-trips redacted blocks and per-block signatures unchanged.
    assert cast(Any, assistant_messages[0])["thinking_blocks"] == [
        {
            "type": "thinking",
            "thinking": "alpha",
            "signature": "SIG-A",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "redacted_thinking", "data": "BLOB"},
        {"type": "thinking", "thinking": "beta", "signature": "SIG-B"},
    ]
    assert assistant_messages[0].get("content") == "visible answer"


@pytest.mark.asyncio
async def test_stream_handler_adds_third_party_reasoning_text_to_summary_item() -> None:
    chunks = [
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[
                Choice(index=0, delta=ChoiceDelta.model_construct(reasoning_content="summary"))
            ],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta.model_construct(reasoning="details"))],
        ),
    ]

    events = await _collect_handler_events(*chunks)

    completed_event = next(event for event in events if event.type == "response.completed")
    reasoning_item = completed_event.response.output[0]
    assert isinstance(reasoning_item, ResponseReasoningItem)
    assert reasoning_item.summary[0].text == "summary"
    assert reasoning_item.content
    assert cast(Any, reasoning_item.content[0]).text == "details"


@pytest.mark.asyncio
async def test_stream_handler_orders_refusal_after_reasoning_and_text() -> None:
    chunks = [
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[
                Choice(index=0, delta=ChoiceDelta.model_construct(reasoning_content="summary"))
            ],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta(content="partial"))],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta.model_construct(refusal="blocked"))],
        ),
    ]

    events = await _collect_handler_events(*chunks)

    completed_event = next(event for event in events if event.type == "response.completed")
    assistant_item = completed_event.response.output[1]
    assert isinstance(assistant_item, ResponseOutputMessage)
    assert isinstance(assistant_item.content[0], ResponseOutputText)
    assert isinstance(assistant_item.content[1], ResponseOutputRefusal)
    assert assistant_item.content[0].text == "partial"
    assert assistant_item.content[1].refusal == "blocked"


@pytest.mark.asyncio
async def test_stream_handler_places_text_after_existing_refusal_part() -> None:
    chunks = [
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta.model_construct(refusal="blocked"))],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta(content="partial"))],
        ),
    ]

    events = await _collect_handler_events(*chunks)

    refusal_part_added = next(
        event
        for event in events
        if event.type == "response.content_part.added"
        and isinstance(event.part, ResponseOutputRefusal)
    )
    assert refusal_part_added.content_index == 0
    text_part_added = next(
        event
        for event in events
        if event.type == "response.content_part.added"
        and isinstance(event.part, ResponseOutputText)
    )
    assert text_part_added.content_index == 1

    completed_event = next(event for event in events if event.type == "response.completed")
    assistant_item = completed_event.response.output[0]
    assert isinstance(assistant_item, ResponseOutputMessage)
    # The completed content must line up with the content indexes announced above: the
    # refusal opened first at index 0 and the text followed at index 1.
    assert isinstance(assistant_item.content[0], ResponseOutputRefusal)
    assert isinstance(assistant_item.content[1], ResponseOutputText)
    assert assistant_item.content[0].refusal == "blocked"
    assert assistant_item.content[1].text == "partial"


@pytest.mark.parametrize(
    "deltas",
    [
        pytest.param(
            [
                ChoiceDelta.model_construct(refusal="blocked"),
                ChoiceDelta.model_construct(content="partial"),
            ],
            id="refusal_then_text",
        ),
        pytest.param(
            [
                ChoiceDelta.model_construct(content="partial"),
                ChoiceDelta.model_construct(refusal="blocked"),
            ],
            id="text_then_refusal",
        ),
    ],
)
@pytest.mark.asyncio
async def test_stream_handler_announces_assistant_message_once_for_text_and_refusal(
    deltas: list[ChoiceDelta],
) -> None:
    """A message holding both a text and a refusal part is announced by a single added event."""
    chunks = [
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=delta)],
        )
        for delta in deltas
    ]

    events = await _collect_handler_events(*chunks)

    message_added = [
        event
        for event in events
        if event.type == "response.output_item.added"
        and isinstance(event.item, ResponseOutputMessage)
    ]
    message_done = [
        event
        for event in events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseOutputMessage)
    ]
    assert len(message_added) == 1
    assert len(message_done) == 1
    assert message_added[0].output_index == message_done[0].output_index

    # The single added event still opens the message before its first content part.
    event_types = [event.type for event in events]
    assert event_types.index("response.output_item.added") < event_types.index(
        "response.content_part.added"
    )
    # Both content parts are still announced, one each.
    part_added = [event for event in events if event.type == "response.content_part.added"]
    assert sorted(event.content_index for event in part_added) == [0, 1]
    assert {event.part.type for event in part_added} == {"output_text", "refusal"}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_passes_strict_validation_to_stream_handler(monkeypatch) -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=1, delta=ChoiceDelta(content="ignored"))],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        strict_feature_validation=True,
    ).get_model("gpt-4")

    with pytest.raises(UserError, match="multiple choices or nonzero"):
        async for _event in model.stream_response(
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
            pass


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_response_id", "conversation_id", "expected_param"),
    [
        ("resp_123", None, "previous_response_id"),
        (None, "conv_123", "conversation_id"),
    ],
)
async def test_stream_response_warns_and_ignores_server_managed_conversation_state_by_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    previous_response_id: str | None,
    conversation_id: str | None,
    expected_param: str,
) -> None:
    called = False

    async def patched_fetch_response(self, *args, **kwargs):
        nonlocal called
        called = True
        return _empty_response(), _empty_chat_completion_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    caplog.set_level(logging.WARNING, logger="openai.agents")

    async for _event in model.stream_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
        prompt=None,
    ):
        pass

    assert expected_param in caplog.text
    assert "Ignoring unsupported server-managed conversation state" in caplog.text
    assert called is True


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_warns_and_ignores_prompt_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured_prompt: Any = None

    async def patched_fetch_response(self, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = kwargs.get("prompt")
        return _empty_response(), _empty_chat_completion_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    caplog.set_level(logging.WARNING, logger="openai.agents")

    async for _ in model.stream_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=cast(Any, {"id": "pmpt_123"}),
    ):
        pass

    assert "Reusable prompts are only supported by the Responses API" in caplog.text
    assert "Ignoring `prompt`" in caplog.text
    assert captured_prompt is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_response_id", "conversation_id", "expected_param"),
    [
        ("resp_123", None, "previous_response_id"),
        (None, "conv_123", "conversation_id"),
    ],
)
async def test_stream_response_rejects_server_managed_conversation_state_in_strict_mode(
    monkeypatch: pytest.MonkeyPatch,
    previous_response_id: str | None,
    conversation_id: str | None,
    expected_param: str,
) -> None:
    called = False

    async def patched_fetch_response(self, *args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("_fetch_response should not be called")

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        strict_feature_validation=True,
    ).get_model("gpt-4")

    with pytest.raises(UserError, match="server-managed conversation state") as exc_info:
        async for _event in model.stream_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=None,
        ):
            pass

    assert expected_param in str(exc_info.value)
    assert called is False


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_rejects_prompt_in_strict_mode(monkeypatch) -> None:
    async def patched_fetch_response(self, *args, **kwargs):
        raise AssertionError("_fetch_response should not run when prompt is unsupported")

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        strict_feature_validation=True,
    ).get_model("gpt-4")

    with pytest.raises(UserError, match="Reusable prompts"):
        async for _ in model.stream_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=cast(Any, {"id": "pmpt_123"}),
        ):
            pass


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
async def test_stream_response_accumulates_logprobs_across_many_deltas(monkeypatch) -> None:
    # Each content delta carries its own logprobs, and the streamed output text part must
    # accumulate all of them in order across the whole stream.
    tokens = ["a", "b", "c", "d", "e"]

    def make_chunk(token: str) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[
                Choice(
                    index=0,
                    delta=ChoiceDelta(content=token),
                    logprobs=ChoiceLogprobs(
                        content=[
                            ChatCompletionTokenLogprob(
                                token=token,
                                logprob=-0.5,
                                bytes=[1],
                                top_logprobs=[TopLogprob(token=token, logprob=-0.5, bytes=[1])],
                            )
                        ]
                    ),
                )
            ],
        )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for token in tokens:
            yield make_chunk(token)

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

    completed_event = next(event for event in output_events if event.type == "response.completed")
    assert isinstance(completed_event, ResponseCompletedEvent)
    completed_resp = completed_event.response
    assert isinstance(completed_resp.output[0], ResponseOutputMessage)
    text_part = completed_resp.output[0].content[0]
    assert isinstance(text_part, ResponseOutputText)
    assert text_part.text == "".join(tokens)
    assert text_part.logprobs is not None
    assert [lp.token for lp in text_part.logprobs] == tokens


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
async def test_stream_response_buffers_tool_call_deltas_when_enabled(monkeypatch) -> None:
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments="arg1"),
        type="function",
    )
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=0,
        id=None,
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
        for chunk in (chunk1, chunk2):
            yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

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

    argument_delta_events = [
        event for event in output_events if event.type == "response.function_call_arguments.delta"
    ]
    assert len(argument_delta_events) == 1
    assert argument_delta_events[0].delta == "arg1arg2"

    done_event = next(event for event in output_events if event.type == "response.output_item.done")
    final_fn = done_event.item
    assert isinstance(final_fn, ResponseFunctionToolCall)
    assert final_fn.call_id == "tool-id"
    assert final_fn.name == "my_func"
    assert final_fn.arguments == "arg1arg2"

    completed_event = next(event for event in output_events if event.type == "response.completed")
    assert isinstance(completed_event, ResponseCompletedEvent)
    assert completed_event.response.usage
    assert completed_event.response.usage.total_tokens == 2


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_tool_call_before_text_replays_as_single_assistant_session_message() -> None:
    tool_call_delta = ChoiceDeltaToolCall(
        index=0,
        id="call_lookup_status",
        function=ChoiceDeltaToolCallFunction(name="lookup_status", arguments="{}"),
        type="function",
    )
    tool_first_chunk = ChatCompletionChunk(
        id="chunk-tool",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta]))],
    )
    later_text_chunk = ChatCompletionChunk(
        id="chunk-text",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(content="I'll look that up first."),
            )
        ],
        usage=CompletionUsage(completion_tokens=5, prompt_tokens=5, total_tokens=10),
    )
    final_text_chunk = ChatCompletionChunk(
        id="chunk-final",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="first run done"))],
        usage=CompletionUsage(completion_tokens=3, prompt_tokens=7, total_tokens=10),
    )

    async def first_turn_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield tool_first_chunk
        yield later_text_chunk

    async def final_turn_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield final_text_chunk

    class DummyCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            call_number = len(self.calls)

            if kwargs["stream"] is True:
                if call_number == 1:
                    return first_turn_stream()
                if call_number == 2:
                    return final_turn_stream()
                raise AssertionError(f"Unexpected streamed call {call_number}")

            return ChatCompletion(
                id="resp-id",
                created=0,
                model="fake",
                object="chat.completion",
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        finish_reason="stop",
                        message=ChatCompletionMessage(
                            role="assistant",
                            content="second run done",
                        ),
                    )
                ],
                usage=None,
            )

    class DummyClient:
        def __init__(self, completions: DummyCompletions) -> None:
            self.chat = type("_Chat", (), {"completions": completions})()
            self.base_url = "http://fake"

    def lookup_status() -> str:
        return "lookup result"

    completions = DummyCompletions()
    model = OpenAIChatCompletionsModel(
        model="gpt-4",
        openai_client=DummyClient(completions),  # type: ignore[arg-type]
        buffer_streamed_tool_calls=True,
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[function_tool(lookup_status, name_override="lookup_status")],
    )
    session = SimpleListSession()

    first_result = Runner.run_streamed(agent, input="first question", session=session)
    async for _ in first_result.stream_events():
        pass

    assert first_result.final_output == "first run done"
    await Runner.run(agent, input="second question", session=session)

    assert len(completions.calls) == 3
    replayed_messages = completions.calls[2]["messages"]
    assert [message["role"] for message in replayed_messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]

    assistant_with_tool = cast(dict[str, Any], replayed_messages[1])
    assert assistant_with_tool["content"] == "I'll look that up first."
    assert len(assistant_with_tool["tool_calls"]) == 1
    tool_call = assistant_with_tool["tool_calls"][0]
    assert tool_call["id"] == "call_lookup_status"
    assert tool_call["function"] == {"name": "lookup_status", "arguments": "{}"}

    tool_message = cast(dict[str, Any], replayed_messages[2])
    assert tool_message["tool_call_id"] == "call_lookup_status"
    assert tool_message["content"] == "lookup result"
    assert replayed_messages[3]["content"] == "first run done"
    assert replayed_messages[4]["content"] == "second question"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_buffers_tool_call_usage_chunk_without_replay(
    monkeypatch,
) -> None:
    tool_call_delta = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments="arg1"),
        type="function",
    )
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta]))],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

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

    argument_delta_events = [
        event for event in output_events if event.type == "response.function_call_arguments.delta"
    ]
    assert len(argument_delta_events) == 1
    assert argument_delta_events[0].delta == "arg1"

    function_done_events = [
        event
        for event in output_events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseFunctionToolCall)
    ]
    assert len(function_done_events) == 1

    completed_event = next(event for event in output_events if event.type == "response.completed")
    assert isinstance(completed_event, ResponseCompletedEvent)
    assert completed_event.response.usage
    assert completed_event.response.usage.total_tokens == 2


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_buffers_tool_call_provider_fields(monkeypatch) -> None:
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments=None),
        type="function",
    )
    cast(Any, tool_call_delta1).provider_specific_fields = {"thought_signature": "thought-sig"}
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=0,
        id=None,
        function=ChoiceDeltaToolCallFunction(name=None, arguments="arg1"),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="gemini/gemini-3-pro",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta1]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="gemini/gemini-3-pro",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta2]))],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in (chunk1, chunk2):
            yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        buffer_streamed_tool_calls=True,
    ).get_model("gemini/gemini-3-pro")

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

    function_done_events = [
        event
        for event in output_events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseFunctionToolCall)
    ]
    assert len(function_done_events) == 1
    provider_data = function_done_events[0].item.model_dump().get("provider_data", {})
    assert provider_data["thought_signature"] == "thought-sig"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_buffered_tool_calls_raise_for_missing_tool_call_delta(
    monkeypatch,
) -> None:
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(), finish_reason="tool_calls")],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

    with pytest.raises(ModelBehaviorError, match="finish_reason='tool_calls'"):
        async for _event in model.stream_response(
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
            pass


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_tool_calls_preserve_nonzero_choice_validation(monkeypatch) -> None:
    tool_call_delta = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments="arg"),
        type="function",
    )
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=1, delta=ChoiceDelta(tool_calls=[tool_call_delta]))],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        strict_feature_validation=True,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

    with pytest.raises(UserError, match="multiple choices or nonzero choice indexes"):
        async for _event in model.stream_response(
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
            pass


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_tool_calls_do_not_merge_nonzero_choice_tool_call_indexes(
    monkeypatch,
) -> None:
    choice_zero_tool_call = ChoiceDeltaToolCall(
        index=0,
        id="choice-zero-tool-id",
        function=ChoiceDeltaToolCallFunction(name="choice_zero_func", arguments="choice-zero"),
        type="function",
    )
    choice_one_tool_call = ChoiceDeltaToolCall(
        index=0,
        id="choice-one-tool-id",
        function=ChoiceDeltaToolCallFunction(name="choice_one_func", arguments="choice-one"),
        type="function",
    )
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(index=0, delta=ChoiceDelta(tool_calls=[choice_zero_tool_call])),
            Choice(index=1, delta=ChoiceDelta(tool_calls=[choice_one_tool_call])),
        ],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

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

    function_done_events = [
        event
        for event in output_events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseFunctionToolCall)
    ]
    assert len(function_done_events) == 1
    final_fn = function_done_events[0].item
    assert isinstance(final_fn, ResponseFunctionToolCall)
    assert final_fn.call_id == "choice-zero-tool-id"
    assert final_fn.name == "choice_zero_func"
    assert final_fn.arguments == "choice-zero"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_tool_calls_preserve_custom_tool_call_strict_error(
    monkeypatch,
) -> None:
    custom_tool_call_delta = ChoiceDeltaToolCall.model_construct(
        index=0,
        id="tool-call-123",
        type="custom",
    )
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(tool_calls=[custom_tool_call_delta]),
                finish_reason="tool_calls",
            )
        ],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        strict_feature_validation=True,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

    with pytest.raises(UserError, match="Custom tool calls are not supported"):
        async for _event in model.stream_response(
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
            pass


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_tool_calls_ignore_custom_tool_call_by_default(monkeypatch) -> None:
    custom_tool_call_delta = ChoiceDeltaToolCall.model_construct(
        index=0,
        id="tool-call-123",
        type="custom",
    )
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(tool_calls=[custom_tool_call_delta]),
                finish_reason="tool_calls",
            )
        ],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

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

    completed_event = next(event for event in output_events if event.type == "response.completed")
    assert isinstance(completed_event, ResponseCompletedEvent)
    assert completed_event.response.output == []


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_with_custom_tool_call_raises_in_strict_mode(monkeypatch) -> None:
    custom_tool_call_delta = ChoiceDeltaToolCall.model_construct(
        index=0,
        id="tool-call-123",
        type="custom",
    )
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[custom_tool_call_delta]))],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

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
    model = OpenAIProvider(use_responses=False, strict_feature_validation=True).get_model("gpt-4")

    with pytest.raises(UserError, match="Custom tool calls are not supported"):
        async for _event in model.stream_response(
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
            pass


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_ignores_custom_tool_call_chunks_by_default(monkeypatch) -> None:
    custom_tool_call_delta = ChoiceDeltaToolCall.model_construct(
        index=0,
        id="tool-call-123",
        type="custom",
    )
    omitted_type_tool_call_delta = ChoiceDeltaToolCall.model_construct(
        index=0,
        function=ChoiceDeltaToolCallFunction(name="custom_tool", arguments="payload"),
    )
    chunks = [
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[custom_tool_call_delta]))],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[omitted_type_tool_call_delta]))],
        ),
        ChatCompletionChunk(
            id="chunk-id",
            created=1,
            model="fake",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta(content="done"))],
        ),
    ]

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in chunks:
            yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")

    events = []
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
        events.append(event)

    function_call_events = []
    for event in events:
        item = getattr(event, "item", None)
        if isinstance(item, ResponseFunctionToolCall):
            function_call_events.append(event)
    assert function_call_events == []
    completed_event = events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    assert all(
        not isinstance(item, ResponseFunctionToolCall) for item in completed_event.response.output
    )
    assert len(completed_event.response.output) == 1
    message = completed_event.response.output[0]
    assert isinstance(message, ResponseOutputMessage)
    assert len(message.content) == 1
    assert isinstance(message.content[0], ResponseOutputText)
    assert message.content[0].text == "done"


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
async def test_fallback_function_calls_have_unique_output_indexes(monkeypatch) -> None:
    tool_call_delta1 = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(
            name="first_tool",
            arguments='{"a": 1}',
        ),
        type="function",
    )
    tool_call_delta2 = ChoiceDeltaToolCall(
        index=1,
        function=ChoiceDeltaToolCallFunction(
            name="second_tool",
            arguments='{"b": 2}',
        ),
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

    added_indexes = [
        event.output_index for event in output_events if event.type == "response.output_item.added"
    ]
    delta_indexes = [
        event.output_index
        for event in output_events
        if event.type == "response.function_call_arguments.delta"
    ]
    done_indexes = [
        event.output_index for event in output_events if event.type == "response.output_item.done"
    ]

    assert added_indexes == [0, 1]
    assert delta_indexes == [0, 1]
    assert done_indexes == [0, 1]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_fallback_function_call_keeps_index_before_streamed_call(monkeypatch) -> None:
    fallback_first = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(
            name="fallback_first",
            arguments='{"a": 1}',
        ),
        type="function",
    )
    streamed_second_start = ChoiceDeltaToolCall(
        index=1,
        id="tool-call-2",
        function=ChoiceDeltaToolCallFunction(
            name="streamed_second",
            arguments="",
        ),
        type="function",
    )
    streamed_second_args = ChoiceDeltaToolCall(
        index=1,
        function=ChoiceDeltaToolCallFunction(arguments='{"b": 2}'),
        type="function",
    )

    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[fallback_first]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_second_start]))],
    )
    chunk3 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_second_args]))],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for c in (chunk1, chunk2, chunk3):
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

    completed = next(
        event.response for event in output_events if event.type == "response.completed"
    )
    assert [
        item.name for item in completed.output if isinstance(item, ResponseFunctionToolCall)
    ] == [
        "fallback_first",
        "streamed_second",
    ]

    added_by_name = {
        event.item.name: event.output_index
        for event in output_events
        if event.type == "response.output_item.added"
        and isinstance(event.item, ResponseFunctionToolCall)
    }
    delta_indexes = [
        event.output_index
        for event in output_events
        if event.type == "response.function_call_arguments.delta"
    ]
    done_by_name = {
        event.item.name: event.output_index
        for event in output_events
        if event.type == "response.output_item.done"
        and isinstance(event.item, ResponseFunctionToolCall)
    }

    assert added_by_name == {"fallback_first": 0, "streamed_second": 1}
    assert delta_indexes == [1, 0]
    assert done_by_name == {"streamed_second": 1, "fallback_first": 0}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_fallback_function_call_before_text_uses_final_output_index(
    monkeypatch,
) -> None:
    fallback_call = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(name="first_tool", arguments='{"a": 1}'),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[fallback_call]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="answer"))],
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

    added_message_event = next(
        event for event in added_events if isinstance(event.item, ResponseOutputMessage)
    )
    added_tool_event = next(
        event for event in added_events if isinstance(event.item, ResponseFunctionToolCall)
    )
    done_message_event = next(
        event for event in done_events if isinstance(event.item, ResponseOutputMessage)
    )
    done_tool_event = next(
        event for event in done_events if isinstance(event.item, ResponseFunctionToolCall)
    )

    assert added_message_event.output_index == 0
    assert added_tool_event.output_index == 1
    assert [event.output_index for event in delta_events] == [1]
    assert done_message_event.output_index == 0
    assert done_tool_event.output_index == 1
    assert isinstance(completed_event.response.output[0], ResponseOutputMessage)
    assert isinstance(completed_event.response.output[1], ResponseFunctionToolCall)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_streamed_function_call_before_text_keeps_realtime_order(
    monkeypatch,
) -> None:
    streamed_call_start = ChoiceDeltaToolCall(
        index=0,
        id="tool-call-1",
        function=ChoiceDeltaToolCallFunction(name="first_tool", arguments=""),
        type="function",
    )
    streamed_call_args = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(arguments='{"a": 1}'),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_call_start]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_call_args]))],
    )
    chunk3 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="answer"))],
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

    added_message_event = next(
        event for event in added_events if isinstance(event.item, ResponseOutputMessage)
    )
    added_tool_event = next(
        event for event in added_events if isinstance(event.item, ResponseFunctionToolCall)
    )
    done_message_event = next(
        event for event in done_events if isinstance(event.item, ResponseOutputMessage)
    )
    done_tool_event = next(
        event for event in done_events if isinstance(event.item, ResponseFunctionToolCall)
    )

    assert added_tool_event.output_index == 0
    assert added_message_event.output_index == 1
    assert [event.output_index for event in delta_events] == [0]
    assert done_tool_event.output_index == 0
    assert done_message_event.output_index == 1
    assert isinstance(completed_event.response.output[0], ResponseFunctionToolCall)
    assert isinstance(completed_event.response.output[1], ResponseOutputMessage)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_mixed_function_calls_before_text_keep_tracked_order(
    monkeypatch,
) -> None:
    fallback_first = ChoiceDeltaToolCall(
        index=0,
        function=ChoiceDeltaToolCallFunction(name="fallback_first", arguments='{"a": 1}'),
        type="function",
    )
    streamed_second_start = ChoiceDeltaToolCall(
        index=1,
        id="tool-call-2",
        function=ChoiceDeltaToolCallFunction(name="streamed_second", arguments=""),
        type="function",
    )
    streamed_second_args = ChoiceDeltaToolCall(
        index=1,
        function=ChoiceDeltaToolCallFunction(arguments='{"b": 2}'),
        type="function",
    )
    chunk1 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[fallback_first]))],
    )
    chunk2 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_second_start]))],
    )
    chunk3 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(tool_calls=[streamed_second_args]))],
    )
    chunk4 = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="answer"))],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in (chunk1, chunk2, chunk3, chunk4):
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
    completed_event = next(event for event in output_events if event.type == "response.completed")

    added_message_event = next(
        event for event in added_events if isinstance(event.item, ResponseOutputMessage)
    )
    added_tool_indexes = {
        event.item.name: event.output_index
        for event in added_events
        if isinstance(event.item, ResponseFunctionToolCall)
    }

    assert added_tool_indexes == {"streamed_second": 1, "fallback_first": 0}
    assert added_message_event.output_index == 2
    assert {event.delta: event.output_index for event in delta_events} == {
        '{"b": 2}': 1,
        '{"a": 1}': 0,
    }
    assert isinstance(completed_event.response.output[0], ResponseFunctionToolCall)
    assert isinstance(completed_event.response.output[1], ResponseFunctionToolCall)
    assert isinstance(completed_event.response.output[2], ResponseOutputMessage)


async def _buffered_stream_events(monkeypatch, chunks: list[ChatCompletionChunk]) -> list[Any]:
    """Run the given chunks through the Chat Completions model with tool-call
    buffering enabled, returning the streamed events."""

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in chunks:
            yield chunk

    async def patched_fetch_response(self, *args, **kwargs):
        return _empty_response(), fake_stream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(
        use_responses=False,
        buffer_streamed_tool_calls=True,
    ).get_model("gpt-4")

    return [
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


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_stream_synthesizes_refusal_on_content_filter(monkeypatch) -> None:
    """With tool-call buffering enabled, a stream that terminates with
    finish_reason == "content_filter" and no emitted content must still
    synthesize a ResponseOutputRefusal.

    The buffering layer only forwarded choices whose delta carried output, so the
    terminal empty-delta chunk was dropped before the handler could see the
    finish_reason, turning a safety block into a silently empty turn.
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
        usage=CompletionUsage(completion_tokens=0, prompt_tokens=7, total_tokens=7),
    )

    output_events = await _buffered_stream_events(monkeypatch, [chunk1, chunk2])

    types = [e.type for e in output_events]
    assert "response.refusal.delta" in types
    assert types[-1] == "response.completed"

    refusal_deltas = [e for e in output_events if e.type == "response.refusal.delta"]
    assert refusal_deltas and refusal_deltas[0].delta

    # The assistant message is announced once and every opened part is closed.
    assert types.count("response.output_item.added") == 1
    assert types.count("response.content_part.added") == types.count("response.content_part.done")

    # The empty "" content delta must not open a text content part.
    assert "response.output_text.delta" not in types
    added_parts = [e for e in output_events if e.type == "response.content_part.added"]
    assert len(added_parts) == 1
    assert isinstance(added_parts[0].part, ResponseOutputRefusal)

    completed_event = output_events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    assistant_msg = completed_event.response.output[0]
    assert isinstance(assistant_msg, ResponseOutputMessage)
    assert len(assistant_msg.content) == 1
    refusal_part = assistant_msg.content[0]
    assert isinstance(refusal_part, ResponseOutputRefusal)
    assert refusal_part.refusal

    # Streamed content_index matches the refusal's position in the completed response.
    assert added_parts[0].content_index == 0
    assert refusal_deltas[0].content_index == 0


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_stream_content_filter_does_not_clobber_text(monkeypatch) -> None:
    """A content_filter finish_reason arriving after real text was streamed must
    not synthesize a refusal, even with buffering enabled."""
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

    output_events = await _buffered_stream_events(monkeypatch, [chunk1, chunk2])

    assert "response.refusal.delta" not in [e.type for e in output_events]
    completed_event = output_events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    assistant_msg = completed_event.response.output[0]
    assert isinstance(assistant_msg, ResponseOutputMessage)
    text_part = assistant_msg.content[0]
    assert isinstance(text_part, ResponseOutputText)
    assert text_part.text == "answer"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_buffered_stream_content_filter_refusal_after_reasoning(monkeypatch) -> None:
    """A buffered content_filter turn preceded by reasoning still places the
    synthesized refusal at content_index 0 of the assistant message, which is
    output_index 1 (the reasoning item is a separate output item)."""
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

    output_events = await _buffered_stream_events(
        monkeypatch, [chunk_reasoning, chunk_empty, chunk_filter]
    )

    completed_event = output_events[-1]
    assert isinstance(completed_event, ResponseCompletedEvent)
    completed_resp = completed_event.response
    assert isinstance(completed_resp.output[0], ResponseReasoningItem)
    assistant_msg = completed_resp.output[1]
    assert isinstance(assistant_msg, ResponseOutputMessage)
    assert len(assistant_msg.content) == 1
    assert isinstance(assistant_msg.content[0], ResponseOutputRefusal)

    added = [
        e
        for e in output_events
        if e.type == "response.content_part.added" and isinstance(e.part, ResponseOutputRefusal)
    ]
    deltas = [e for e in output_events if e.type == "response.refusal.delta"]
    assert len(added) == 1
    assert added[0].content_index == 0
    assert added[0].output_index == 1
    assert deltas and all(d.content_index == 0 and d.output_index == 1 for d in deltas)
    assert "response.output_text.delta" not in [e.type for e in output_events]


def _chunk_with(choices: list[Choice], usage: CompletionUsage | None = None):
    return ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=choices,
        usage=usage,
    )


@pytest.mark.asyncio
async def test_buffer_tool_call_stream_forwards_content_filter_finish_reason() -> None:
    """The buffering layer must forward a content-filtered terminal choice even
    though its delta is empty, so the finish_reason reaches the handler instead
    of being swallowed. The delta is stripped, preserving buffering semantics."""
    chunks = [
        _chunk_with([Choice(index=0, delta=ChoiceDelta(content=""))]),
        _chunk_with([Choice(index=0, delta=ChoiceDelta(), finish_reason="content_filter")]),
    ]

    async def source() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in chunks:
            yield chunk

    buffered = [c async for c in ChatCmplStreamHandler.buffer_tool_call_stream(source())]

    terminal_choices = [
        choice
        for chunk in buffered
        for choice in chunk.choices
        if choice.finish_reason == "content_filter"
    ]
    assert len(terminal_choices) == 1
    # The forwarded copy carries no delta output.
    assert not ChatCmplStreamHandler._delta_has_passthrough_output(terminal_choices[0].delta)


@pytest.mark.asyncio
async def test_buffer_tool_call_stream_does_not_duplicate_tool_calls_finish() -> None:
    """finish_reason == "tool_calls" is still emitted only by the synthesized
    buffered chunk, so the terminal choice is not forwarded twice."""
    tool_call_delta = ChoiceDeltaToolCall(
        index=0,
        id="tool-id",
        function=ChoiceDeltaToolCallFunction(name="my_func", arguments='{"a": 1}'),
        type="function",
    )
    chunks = [
        _chunk_with([Choice(index=0, delta=ChoiceDelta(tool_calls=[tool_call_delta]))]),
        _chunk_with([Choice(index=0, delta=ChoiceDelta(), finish_reason="tool_calls")]),
    ]

    async def source() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in chunks:
            yield chunk

    buffered = [c async for c in ChatCmplStreamHandler.buffer_tool_call_stream(source())]

    finish_choices = [
        choice
        for chunk in buffered
        for choice in chunk.choices
        if choice.finish_reason == "tool_calls"
    ]
    assert len(finish_choices) == 1
    assert finish_choices[0].delta.tool_calls


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_propagates_request_id(monkeypatch) -> None:
    """The OpenAI request ID must reach the terminal streamed response.

    `Runner` reads `_request_id` off the terminal response to populate
    `ModelResponse.request_id`, so the streamed Chat Completions path has to carry the
    `x-request-id` header from the underlying HTTP response.
    """
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="Hello"))],
    )

    class FakeStream:
        """Mimics `openai.AsyncStream`, which exposes the raw HTTP response."""

        def __init__(self) -> None:
            self.response = httpx.Response(
                200,
                headers={"x-request-id": "req_streamed_456"},
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            )

        def __aiter__(self) -> AsyncIterator[ChatCompletionChunk]:
            async def gen() -> AsyncIterator[ChatCompletionChunk]:
                yield chunk

            return gen()

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
        return resp, FakeStream()

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")

    completed: ResponseCompletedEvent | None = None
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
        if event.type == "response.completed":
            completed = event

    assert completed is not None
    assert getattr(completed.response, "_request_id", None) == "req_streamed_456"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_without_http_response_has_no_request_id(monkeypatch) -> None:
    """Custom clients and test doubles that yield a bare async iterator still stream."""
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="Hello"))],
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

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

    completed: ResponseCompletedEvent | None = None
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
        if event.type == "response.completed":
            completed = event

    assert completed is not None
    assert getattr(completed.response, "_request_id", None) is None


@pytest.mark.asyncio
async def test_stream_handler_keeps_url_citations_on_the_text_delta() -> None:
    """Citations reported alongside the text reach the output text, as when not streaming."""
    events = await _collect_handler_events(
        _annotated_chunk(
            {
                "role": "assistant",
                "content": "It will rain tomorrow.",
                "annotations": [_url_citation()],
            },
            finish_reason="stop",
        )
    )

    assert _streamed_annotations(events) == [
        {
            "type": "url_citation",
            "start_index": 0,
            "end_index": 22,
            "url": "https://example.com/weather",
            "title": "Weather",
        }
    ]


@pytest.mark.asyncio
async def test_stream_handler_keeps_url_citations_reported_after_the_text() -> None:
    """A provider may cite on a later delta, once the text the citation indexes is sent."""
    events = await _collect_handler_events(
        _annotated_chunk({"role": "assistant", "content": "It will rain tomorrow."}),
        _annotated_chunk({"annotations": [_url_citation()]}, finish_reason="stop"),
    )

    assert [annotation["url"] for annotation in _streamed_annotations(events)] == [
        "https://example.com/weather"
    ]


@pytest.mark.asyncio
async def test_stream_handler_accumulates_url_citations_across_deltas() -> None:
    """Citations accumulate rather than replace, as LiteLLM does in `stream_chunk_builder`.

    `delta.annotations` is undocumented, so a provider may spread citations over several
    deltas or report them only on the last one, and accumulating keeps both cases whole.
    A provider repeating its full list on every delta would report duplicates, which is
    the same tradeoff LiteLLM makes.
    """
    events = await _collect_handler_events(
        _annotated_chunk(
            {
                "role": "assistant",
                "content": "It will rain tomorrow.",
                "annotations": [_url_citation()],
            }
        ),
        _annotated_chunk(
            {"annotations": [_url_citation(url="https://example.com/forecast", title="Forecast")]},
            finish_reason="stop",
        ),
    )

    assert [annotation["url"] for annotation in _streamed_annotations(events)] == [
        "https://example.com/weather",
        "https://example.com/forecast",
    ]


@pytest.mark.asyncio
async def test_stream_handler_buffering_keeps_a_citation_only_delta() -> None:
    """Tool call buffering must forward a delta whose only output is a citation."""
    chunks = await _collect_buffered_tool_call_chunks(
        _annotated_chunk({"role": "assistant", "content": "It will rain tomorrow."}),
        _annotated_chunk({"annotations": [_url_citation()]}, finish_reason="stop"),
    )
    events = await _collect_handler_events(*chunks)

    assert [annotation["url"] for annotation in _streamed_annotations(events)] == [
        "https://example.com/weather"
    ]


@pytest.mark.asyncio
async def test_stream_handler_skips_unsupported_annotation_shapes() -> None:
    """An unsupported or incomplete citation is dropped instead of failing the turn."""
    other_type = {"type": "file_citation", "file_citation": {"file_id": "file-1", "index": 0}}
    incomplete = {"type": "url_citation", "url_citation": {"url": "https://example.com/partial"}}
    events = await _collect_handler_events(
        _annotated_chunk(
            {
                "role": "assistant",
                "content": "It will rain tomorrow.",
                "annotations": [other_type, incomplete, _url_citation()],
            },
            finish_reason="stop",
        )
    )

    assert [annotation["url"] for annotation in _streamed_annotations(events)] == [
        "https://example.com/weather"
    ]


@pytest.mark.asyncio
async def test_stream_handler_ignores_annotations_that_are_not_a_sequence() -> None:
    """The streamed field is untyped, so an unexpected shape must not fail the turn."""
    events = await _collect_handler_events(
        _annotated_chunk(
            {"role": "assistant", "content": "It will rain tomorrow.", "annotations": 5},
            finish_reason="stop",
        )
    )

    assert _streamed_annotations(events) == []


@pytest.mark.asyncio
async def test_stream_handler_drops_citations_reported_before_any_text() -> None:
    """Citations index into text, so one reported before any text part opens is dropped."""
    events = await _collect_handler_events(
        _annotated_chunk({"role": "assistant", "annotations": [_url_citation()]}),
        _annotated_chunk({"content": "It will rain tomorrow."}, finish_reason="stop"),
    )

    assert _streamed_annotations(events) == []
    completed = cast(ResponseCompletedEvent, events[-1])
    message = cast(ResponseOutputMessage, completed.response.output[0])
    assert len(message.content) == 1
    assert cast(ResponseOutputText, message.content[0]).text == "It will rain tomorrow."


def _usageless_stream_patch(usage: CompletionUsage | None = None):
    """Patch `_fetch_response` with a stream whose only chunk carries `usage`."""
    chunk = ChatCompletionChunk(
        id="chunk-id",
        created=1,
        model="fake",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="Hello"))],
        usage=usage,
    )

    async def fake_stream() -> AsyncIterator[ChatCompletionChunk]:
        yield chunk

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

    return patched_fetch_response


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_streamed_run_counts_request_when_provider_omits_usage(monkeypatch) -> None:
    """A stream that never carries a usage chunk still made a request.

    Providers without `stream_options.include_usage` finish the stream with no usage payload.
    The run must still report the request, while token counts stay at zero because the
    provider genuinely did not report them.
    """
    monkeypatch.setattr(
        OpenAIChatCompletionsModel, "_fetch_response", _usageless_stream_patch(usage=None)
    )
    agent = Agent(name="test", model=OpenAIProvider(use_responses=False).get_model("gpt-4"))

    result = Runner.run_streamed(agent, "hi")
    completed: ResponseCompletedEvent | None = None
    async for event in result.stream_events():
        raw = getattr(event, "data", None)
        if isinstance(raw, ResponseCompletedEvent):
            completed = raw

    assert result.context_wrapper.usage.requests == 1
    assert result.context_wrapper.usage.total_tokens == 0
    # No usage payload is synthesized, so nothing reports token counts that never arrived.
    assert completed is not None
    assert completed.response.usage is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_streamed_run_does_not_double_count_when_usage_is_present(monkeypatch) -> None:
    """The usage-less path must not add a second request when usage did arrive."""
    monkeypatch.setattr(
        OpenAIChatCompletionsModel,
        "_fetch_response",
        _usageless_stream_patch(
            usage=CompletionUsage(completion_tokens=5, prompt_tokens=7, total_tokens=12)
        ),
    )
    agent = Agent(name="test", model=OpenAIProvider(use_responses=False).get_model("gpt-4"))

    result = Runner.run_streamed(agent, "hi")
    async for _ in result.stream_events():
        pass

    assert result.context_wrapper.usage.requests == 1
    assert result.context_wrapper.usage.total_tokens == 12


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_streamed_span_records_the_request_when_provider_omits_usage(monkeypatch) -> None:
    """Streamed tracing must record the request the same way the non-streaming path does.

    Non-streaming writes a span usage object with `requests: 1` when the provider reports no
    usage. Streaming used to omit span usage entirely, so the run reported one request while
    the model span showed none.
    """
    monkeypatch.setattr(
        OpenAIChatCompletionsModel, "_fetch_response", _usageless_stream_patch(usage=None)
    )
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")

    with trace(workflow_name="test"):
        async for _ in model.stream_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.ENABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ):
            pass

    spans = fetch_ordered_spans()
    generation = next(s for s in spans if s.span_data.type == "generation")
    assert generation.span_data.usage is not None
    assert generation.span_data.usage["requests"] == 1
    # The provider reported no tokens, so every total stays at zero.
    assert generation.span_data.usage["total_tokens"] == 0


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_span_is_recorded_for_a_consumer_that_stops_at_the_terminal_event(
    monkeypatch,
) -> None:
    """A caller that stops at `response.completed` closes the generator.

    Anything recorded only after the yield loop never runs for such a consumer, so the span
    has to be populated before the terminal event is handed out.
    """
    monkeypatch.setattr(
        OpenAIChatCompletionsModel, "_fetch_response", _usageless_stream_patch(usage=None)
    )
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")

    with trace(workflow_name="test"):
        stream = model.stream_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.ENABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
        stream_agen = cast(Any, stream)
        async for event in stream_agen:
            if event.type == "response.completed":
                break  # stop consuming, as a caller watching for the terminal event would
        await stream_agen.aclose()

    generation = next(s for s in fetch_ordered_spans() if s.span_data.type == "generation")
    assert generation.span_data.usage is not None
    assert generation.span_data.usage["requests"] == 1

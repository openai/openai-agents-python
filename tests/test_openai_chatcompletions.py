from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from openai import NOT_GIVEN, AsyncOpenAI
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (  # type: ignore[attr-defined]
    ChatCompletionMessageFunctionToolCall,
    Function,
)
from openai.types.completion_usage import (
    CompletionUsage,
    PromptTokensDetails,
)
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
)

from agents import (
    ModelResponse,
    ModelSettings,
    ModelTracing,
    OpenAIChatCompletionsModel,
    OpenAIProvider,
    generation_span,
)
from agents.models.chatcmpl_helpers import ChatCmplHelpers
from agents.models.fake_id import FAKE_RESPONSES_ID


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_with_text_message(monkeypatch) -> None:
    """
    When the model returns a ChatCompletionMessage with plain text content,
    `get_response` should produce a single `ResponseOutputMessage` containing
    a `ResponseOutputText` with that content, and a `Usage` populated from
    the completion's usage.
    """
    msg = ChatCompletionMessage(role="assistant", content="Hello")
    choice = Choice(index=0, finish_reason="stop", message=msg)
    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
        object="chat.completion",
        choices=[choice],
        usage=CompletionUsage(
            completion_tokens=5,
            prompt_tokens=7,
            total_tokens=12,
            # completion_tokens_details left blank to test default
            prompt_tokens_details=PromptTokensDetails(cached_tokens=3),
        ),
    )

    async def patched_fetch_response(self, *args, **kwargs):
        return chat

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    resp: ModelResponse = await model.get_response(
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
    # Should have produced exactly one output message with one text part
    assert isinstance(resp, ModelResponse)
    assert len(resp.output) == 1
    assert isinstance(resp.output[0], ResponseOutputMessage)
    msg_item = resp.output[0]
    assert len(msg_item.content) == 1
    assert isinstance(msg_item.content[0], ResponseOutputText)
    assert msg_item.content[0].text == "Hello"
    # Usage should be preserved from underlying ChatCompletion.usage
    assert resp.usage.input_tokens == 7
    assert resp.usage.output_tokens == 5
    assert resp.usage.total_tokens == 12
    assert resp.usage.input_tokens_details.cached_tokens == 3
    assert resp.usage.output_tokens_details.reasoning_tokens == 0
    assert resp.response_id is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_with_refusal(monkeypatch) -> None:
    """
    When the model returns a ChatCompletionMessage with a `refusal` instead
    of normal `content`, `get_response` should produce a single
    `ResponseOutputMessage` containing a `ResponseOutputRefusal` part.
    """
    msg = ChatCompletionMessage(role="assistant", refusal="No thanks")
    choice = Choice(index=0, finish_reason="stop", message=msg)
    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
        object="chat.completion",
        choices=[choice],
        usage=None,
    )

    async def patched_fetch_response(self, *args, **kwargs):
        return chat

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    resp: ModelResponse = await model.get_response(
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
    assert len(resp.output) == 1
    assert isinstance(resp.output[0], ResponseOutputMessage)
    refusal_part = resp.output[0].content[0]
    assert isinstance(refusal_part, ResponseOutputRefusal)
    assert refusal_part.refusal == "No thanks"
    # With no usage from the completion, usage defaults to zeros.
    assert resp.usage.requests == 0
    assert resp.usage.input_tokens == 0
    assert resp.usage.output_tokens == 0
    assert resp.usage.input_tokens_details.cached_tokens == 0
    assert resp.usage.output_tokens_details.reasoning_tokens == 0


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_with_tool_call(monkeypatch) -> None:
    """
    If the ChatCompletionMessage includes one or more tool_calls, `get_response`
    should append corresponding `ResponseFunctionToolCall` items after the
    assistant message item with matching name/arguments.
    """
    tool_call = ChatCompletionMessageFunctionToolCall(
        id="call-id",
        type="function",
        function=Function(name="do_thing", arguments="{'x':1}"),
    )
    msg = ChatCompletionMessage(role="assistant", content="Hi", tool_calls=[tool_call])
    choice = Choice(index=0, finish_reason="stop", message=msg)
    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
        object="chat.completion",
        choices=[choice],
        usage=None,
    )

    async def patched_fetch_response(self, *args, **kwargs):
        return chat

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    resp: ModelResponse = await model.get_response(
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
    # Expect a message item followed by a function tool call item.
    assert len(resp.output) == 2
    assert isinstance(resp.output[0], ResponseOutputMessage)
    fn_call_item = resp.output[1]
    assert isinstance(fn_call_item, ResponseFunctionToolCall)
    assert fn_call_item.call_id == "call-id"
    assert fn_call_item.name == "do_thing"
    assert fn_call_item.arguments == "{'x':1}"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_with_no_message(monkeypatch) -> None:
    """If the model returns no message, get_response should return an empty output."""
    msg = ChatCompletionMessage(role="assistant", content="ignored")
    choice = Choice(index=0, finish_reason="content_filter", message=msg)
    choice.message = None  # type: ignore[assignment]
    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
        object="chat.completion",
        choices=[choice],
        usage=None,
    )

    async def patched_fetch_response(self, *args, **kwargs):
        return chat

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    resp: ModelResponse = await model.get_response(
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
    assert resp.output == []


@pytest.mark.asyncio
async def test_fetch_response_non_stream(monkeypatch) -> None:
    """
    Verify that `_fetch_response` builds the correct OpenAI API call when not
    streaming and returns the ChatCompletion object directly. We supply a
    dummy ChatCompletion through a stubbed OpenAI client and inspect the
    captured kwargs.
    """

    # Dummy completions to record kwargs
    class DummyCompletions:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def create(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs
            return chat

    class DummyClient:
        def __init__(self, completions: DummyCompletions) -> None:
            self.chat = type("_Chat", (), {"completions": completions})()
            self.base_url = httpx.URL("http://fake")

    msg = ChatCompletionMessage(role="assistant", content="ignored")
    choice = Choice(index=0, finish_reason="stop", message=msg)
    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
        object="chat.completion",
        choices=[choice],
    )
    completions = DummyCompletions()
    dummy_client = DummyClient(completions)
    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=dummy_client)  # type: ignore
    # Execute the private fetch with a system instruction and simple string input.
    with generation_span(disabled=True) as span:
        result = await model._fetch_response(
            system_instructions="sys",
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            span=span,
            tracing=ModelTracing.DISABLED,
            stream=False,
        )
    assert result is chat
    # Ensure expected args were passed through to OpenAI client.
    kwargs = completions.kwargs
    assert kwargs["stream"] is False
    assert kwargs["store"] is NOT_GIVEN
    assert kwargs["model"] == "gpt-4"
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][0]["content"] == "sys"
    assert kwargs["messages"][1]["role"] == "user"
    # Defaults for optional fields become the NOT_GIVEN sentinel
    assert kwargs["tools"] is NOT_GIVEN
    assert kwargs["tool_choice"] is NOT_GIVEN
    assert kwargs["response_format"] is NOT_GIVEN
    assert kwargs["stream_options"] is NOT_GIVEN


@pytest.mark.asyncio
async def test_fetch_response_stream(monkeypatch) -> None:
    """
    When `stream=True`, `_fetch_response` should return a bare `Response`
    object along with the underlying async stream. The OpenAI client call
    should include `stream_options` to request usage-delimited chunks.
    """

    async def event_stream() -> AsyncIterator[ChatCompletionChunk]:
        if False:  # pragma: no cover
            yield  # pragma: no cover

    class DummyCompletions:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def create(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs
            return event_stream()

    class DummyClient:
        def __init__(self, completions: DummyCompletions) -> None:
            self.chat = type("_Chat", (), {"completions": completions})()
            self.base_url = httpx.URL("http://fake")

    completions = DummyCompletions()
    dummy_client = DummyClient(completions)
    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=dummy_client)  # type: ignore
    with generation_span(disabled=True) as span:
        response, stream = await model._fetch_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            span=span,
            tracing=ModelTracing.DISABLED,
            stream=True,
        )
    # Check OpenAI client was called for streaming
    assert completions.kwargs["stream"] is True
    assert completions.kwargs["store"] is NOT_GIVEN
    assert completions.kwargs["stream_options"] is NOT_GIVEN
    # Response is a proper openai Response
    assert isinstance(response, Response)
    assert response.id == FAKE_RESPONSES_ID
    assert response.model == "gpt-4"
    assert response.object == "response"
    assert response.output == []
    # We returned the async iterator produced by our dummy.
    assert hasattr(stream, "__aiter__")


def test_store_param():
    """Should default to True for OpenAI API calls, and False otherwise."""

    model_settings = ModelSettings()
    client = AsyncOpenAI()
    assert ChatCmplHelpers.get_store_param(client, model_settings) is True, (
        "Should default to True for OpenAI API calls"
    )

    model_settings = ModelSettings(store=False)
    assert ChatCmplHelpers.get_store_param(client, model_settings) is False, (
        "Should respect explicitly set store=False"
    )

    model_settings = ModelSettings(store=True)
    assert ChatCmplHelpers.get_store_param(client, model_settings) is True, (
        "Should respect explicitly set store=True"
    )

    client = AsyncOpenAI(base_url="http://www.notopenai.com")
    model_settings = ModelSettings()
    assert ChatCmplHelpers.get_store_param(client, model_settings) is None, (
        "Should default to None for non-OpenAI API calls"
    )

    model_settings = ModelSettings(store=False)
    assert ChatCmplHelpers.get_store_param(client, model_settings) is False, (
        "Should respect explicitly set store=False"
    )

    model_settings = ModelSettings(store=True)
    assert ChatCmplHelpers.get_store_param(client, model_settings) is True, (
        "Should respect explicitly set store=True"
    )

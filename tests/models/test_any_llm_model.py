from __future__ import annotations

import asyncio
import importlib
import sys
import types as pytypes
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import pytest
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice, ChoiceDelta
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseOutputMessage,
    ResponseOutputRefusal,
)
from openai.types.responses.response_created_event import ResponseCreatedEvent
from openai.types.responses.response_error_event import ResponseErrorEvent
from openai.types.responses.response_failed_event import ResponseFailedEvent
from openai.types.responses.response_incomplete_event import ResponseIncompleteEvent
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)
from openai.types.shared import Reasoning
from pydantic import BaseModel

from agents import (
    Agent,
    Handoff,
    ModelBehaviorError,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    __version__,
    trace,
)
from agents.exceptions import UserError
from agents.models.chatcmpl_helpers import HEADERS_OVERRIDE
from agents.models.fake_id import FAKE_RESPONSES_ID


class FakeAnyLLMProvider:
    def __init__(
        self,
        *,
        supports_responses: bool,
        chat_response: Any | None = None,
        responses_response: Any | None = None,
    ) -> None:
        self.SUPPORTS_RESPONSES = supports_responses
        self.chat_response = chat_response
        self.responses_response = responses_response
        self.chat_calls: list[dict[str, Any]] = []
        self.responses_calls: list[dict[str, Any]] = []
        self.private_responses_calls: list[dict[str, Any]] = []

    async def acompletion(self, **kwargs: Any) -> Any:
        self.chat_calls.append(kwargs)
        return self.chat_response

    async def aresponses(self, **kwargs: Any) -> Any:
        self.responses_calls.append(kwargs)
        return self.responses_response

    async def _aresponses(self, params: Any, **kwargs: Any) -> Any:
        self.private_responses_calls.append({"params": params, "kwargs": kwargs})
        return self.responses_response


def _import_any_llm_module(
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeAnyLLMProvider,
) -> tuple[Any, list[dict[str, Any]]]:
    create_calls: list[dict[str, Any]] = []

    class FakeAnyLLMFactory:
        @staticmethod
        def create(provider_name: str, api_key: str | None = None, api_base: str | None = None):
            create_calls.append(
                {
                    "provider_name": provider_name,
                    "api_key": api_key,
                    "api_base": api_base,
                }
            )
            return provider

    fake_any_llm: Any = pytypes.ModuleType("any_llm")
    fake_any_llm.AnyLLM = FakeAnyLLMFactory

    sys.modules.pop("agents.extensions.models.any_llm_model", None)
    monkeypatch.setitem(sys.modules, "any_llm", fake_any_llm)

    module = importlib.import_module("agents.extensions.models.any_llm_model")
    monkeypatch.setattr(module, "AnyLLM", FakeAnyLLMFactory, raising=True)
    return module, create_calls


def _chat_completion(text: str) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl_123",
        created=0,
        model="fake-model",
        object="chat.completion",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(role="assistant", content=text),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=5,
            prompt_tokens=7,
            total_tokens=12,
            prompt_tokens_details=PromptTokensDetails.model_validate(
                {"cached_tokens": 2, "cache_write_tokens": 4}
            ),
        ),
    )


def _responses_output(text: str) -> list[Any]:
    return [
        ResponseOutputMessage(
            id="msg_123",
            role="assistant",
            status="completed",
            type="message",
            content=[
                ResponseOutputText(
                    text=text,
                    type="output_text",
                    annotations=[],
                    logprobs=[],
                )
            ],
        )
    ]


def _response(text: str, response_id: str = "resp_123") -> Response:
    return Response(
        id=response_id,
        created_at=123,
        model="fake-model",
        object="response",
        output=_responses_output(text),
        tool_choice="none",
        tools=[],
        parallel_tool_calls=False,
        usage=ResponseUsage(
            input_tokens=11,
            output_tokens=13,
            total_tokens=24,
            input_tokens_details=InputTokensDetails.model_validate(
                {"cache_write_tokens": 0, "cached_tokens": 0}
            ),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        ),
    )


def _chat_completion_with_tool_call(*, thought_signature: str) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl_tool_123",
        created=0,
        model="fake-model",
        object="chat.completion",
        choices=[
            Choice(
                index=0,
                finish_reason="tool_calls",
                message=ChatCompletionMessage(
                    role="assistant",
                    content="Calling a tool.",
                    tool_calls=[
                        ChatCompletionMessageFunctionToolCall.model_validate(
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Paris"}',
                                },
                                "extra_content": {
                                    "google": {"thought_signature": thought_signature}
                                },
                            }
                        )
                    ],
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=5,
            prompt_tokens=7,
            total_tokens=12,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=0),
        ),
    )


class GenericChatCompletionPayload(BaseModel):
    id: str
    created: int
    model: str
    object: str
    choices: list[Any]
    usage: Any


class GenericResponsesPayload(BaseModel):
    id: str
    created_at: float
    model: str
    object: str
    output: list[Any]
    parallel_tool_calls: bool
    tool_choice: Any
    tools: list[Any]
    usage: Any


async def _empty_chat_stream() -> AsyncIterator[ChatCompletionChunk]:
    if False:
        yield ChatCompletionChunk(
            id="chunk_123",
            created=0,
            model="fake-model",
            object="chat.completion.chunk",
            choices=[Choice(index=0, delta=ChoiceDelta(), finish_reason=None)],
        )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("override_ua", [None, "test_user_agent"])
async def test_user_agent_header_any_llm_chat(override_ua: str | None, monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")
    expected_ua = override_ua or f"Agents/Python {__version__}"

    if override_ua is not None:
        token = HEADERS_OVERRIDE.set({"User-Agent": override_ua})
    else:
        token = None
    try:
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    finally:
        if token is not None:
            HEADERS_OVERRIDE.reset(token)

    assert provider.chat_calls[0]["extra_headers"]["User-Agent"] == expected_ua


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_preserves_falsy_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    class FalsyReasoning(Reasoning):
        def __bool__(self) -> bool:
            return False

    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    model = module.AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(reasoning=FalsyReasoning(effort="low")),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    assert provider.chat_calls[0]["reasoning_effort"] == "low"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["gemini", "vertexai"])
@pytest.mark.parametrize("options_type", ["unset", "dictionary", "model"])
async def test_any_llm_google_chat_headers_use_http_options(
    monkeypatch: pytest.MonkeyPatch, provider_name: str, options_type: str
) -> None:
    class HttpOptions(BaseModel):
        headers: dict[str, str]
        timeout: int

    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    model = module.AnyLLMModel(model=f"{provider_name}/gemini-2.5-flash")

    extra_args: dict[str, Any] = {}
    configured_options: dict[str, Any] | HttpOptions | None = None
    if options_type == "dictionary":
        configured_options = {"headers": {"X-Existing": "existing"}, "timeout": 1000}
        extra_args["http_options"] = configured_options
    elif options_type == "model":
        configured_options = HttpOptions(headers={"X-Existing": "existing"}, timeout=1000)
        extra_args["http_options"] = configured_options

    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(
            extra_args=extra_args,
            extra_headers={"X-Test-Header": "test"},
        ),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    call = provider.chat_calls[0]
    assert "extra_headers" not in call
    http_options = call["http_options"]
    if isinstance(http_options, BaseModel):
        http_options = http_options.model_dump()
    assert http_options["headers"]["User-Agent"] == f"Agents/Python {__version__}"
    assert http_options["headers"]["X-Test-Header"] == "test"
    if configured_options is not None:
        assert http_options["headers"]["X-Existing"] == "existing"
        assert http_options["timeout"] == 1000
        if isinstance(configured_options, BaseModel):
            assert configured_options.headers == {"X-Existing": "existing"}
        else:
            assert configured_options["headers"] == {"X-Existing": "existing"}


@pytest.mark.parametrize("provider_name", ["gemini", "vertexai"])
@pytest.mark.parametrize("content_type", ["model", "dictionary"])
def test_any_llm_google_provider_normalizes_function_result_roles(
    monkeypatch: pytest.MonkeyPatch, provider_name: str, content_type: str
) -> None:
    class GoogleContent(BaseModel):
        role: str
        parts: list[dict[str, Any]]

    tool_result: dict[str, Any] = {
        "role": "function",
        "parts": [{"function_response": {"name": "get_weather", "response": {"result": "sunny"}}}],
    }
    original_tool_result: GoogleContent | dict[str, Any]
    if content_type == "model":
        original_tool_result = GoogleContent.model_validate(tool_result)
    else:
        original_tool_result = tool_result

    class GoogleProvider(FakeAnyLLMProvider):
        @staticmethod
        def _convert_completion_params(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "model": "gemini-3.6-flash",
                "contents": [
                    GoogleContent(role="user", parts=[{"text": "Check the weather."}]),
                    original_tool_result,
                    GoogleContent(role="model", parts=[{"text": "Done."}]),
                ],
            }

    provider = GoogleProvider(supports_responses=False)
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    model = module.AnyLLMModel(model=f"{provider_name}/gemini-3.6-flash")

    converted = model._get_provider()._convert_completion_params(object())
    contents = converted["contents"]

    assert [
        item.role if isinstance(item, GoogleContent) else item["role"] for item in contents
    ] == [
        "user",
        "user",
        "model",
    ]
    normalized_tool_result = contents[1]
    if isinstance(normalized_tool_result, BaseModel):
        normalized_tool_result = normalized_tool_result.model_dump()
    assert normalized_tool_result["parts"] == tool_result["parts"]
    assert (
        original_tool_result.role
        if isinstance(original_tool_result, GoogleContent)
        else original_tool_result["role"]
    ) == "function"


def test_any_llm_non_google_provider_does_not_normalize_function_result_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonGoogleProvider(FakeAnyLLMProvider):
        @staticmethod
        def _convert_completion_params(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"contents": [{"role": "function", "parts": [{"result": "ok"}]}]}

    provider = NonGoogleProvider(supports_responses=False)
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    model = module.AnyLLMModel(model="openrouter/google/gemini-3.6-flash")

    converted = model._get_provider()._convert_completion_params(object())

    assert converted["contents"][0]["role"] == "function"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_path_is_used_when_responses_are_unsupported(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openrouter/openai/gpt-5.4-mini", api_key="router-key")
    response = await model.get_response(
        system_instructions="You are terse.",
        input="hi",
        model_settings=ModelSettings(preserve_raw_usage=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id="resp_prev",
        conversation_id="conv_123",
        prompt=None,
    )

    assert create_calls == [
        {
            "provider_name": "openrouter",
            "api_key": "router-key",
            "api_base": None,
        }
    ]
    assert len(provider.chat_calls) == 1
    assert provider.responses_calls == []
    assert provider.chat_calls[0]["model"] == "openai/gpt-5.4-mini"
    assert response.response_id is None
    assert response.output[0].content[0].text == "Hello"
    assert response.usage.input_tokens_details.cached_tokens == 2
    assert getattr(response.usage.input_tokens_details, "cache_write_tokens", None) == 4
    assert response.raw_usage is not None
    assert response.raw_usage["prompt_tokens_details"] == {
        "cached_tokens": 2,
        "cache_write_tokens": 4,
    }


def _content_filtered_chat_completion(content: str) -> ChatCompletion:
    completion = _chat_completion(content)
    completion.choices[0].finish_reason = "content_filter"
    return completion


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_path_surfaces_content_filter_refusal(monkeypatch) -> None:
    """A filtered turn must become a refusal instead of an empty output."""
    provider = FakeAnyLLMProvider(
        supports_responses=False,
        chat_response=_content_filtered_chat_completion(""),
    )
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)

    model = module.AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")
    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    refusals = [
        content
        for item in response.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputRefusal)
    ]
    assert refusals, f"expected a refusal item, got: {response.output}"
    assert refusals[0].refusal


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_path_content_filter_keeps_real_content(monkeypatch) -> None:
    """A filtered turn that still carries text keeps the text and gains no refusal."""
    provider = FakeAnyLLMProvider(
        supports_responses=False,
        chat_response=_content_filtered_chat_completion("here is the answer"),
    )
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)

    model = module.AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")
    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    refusals = [
        content
        for item in response.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputRefusal)
    ]
    assert not refusals
    assert response.output[0].content[0].text == "here is the answer"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat_response",
    [
        pytest.param(_chat_completion("Hello").model_dump(), id="dict"),
        pytest.param(
            GenericChatCompletionPayload.model_validate(_chat_completion("Hello").model_dump()),
            id="basemodel",
        ),
    ],
)
async def test_any_llm_chat_path_normalizes_non_stream_payloads(
    monkeypatch,
    chat_response: Any,
) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=chat_response)
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")
    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    assert response.response_id is None
    assert response.output[0].content[0].text == "Hello"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_path_preserves_gemini_tool_call_metadata(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(
        supports_responses=False,
        chat_response=_chat_completion_with_tool_call(thought_signature="sig_123"),
    )
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="gemini/gemini-2.0-flash")
    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    function_calls = [
        item for item in response.output if getattr(item, "type", None) == "function_call"
    ]
    assert len(function_calls) == 1
    provider_data = function_calls[0].model_dump()["provider_data"]
    assert provider_data["model"] == "gemini/gemini-2.0-flash"
    assert provider_data["response_id"] == "chatcmpl_tool_123"
    assert provider_data["thought_signature"] == "sig_123"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_path_is_used_when_supported(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=_response("Hello"))
    module, create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="gpt-5.4-mini", api_key="openai-key")
    response = await model.get_response(
        system_instructions="You are terse.",
        input="hi",
        model_settings=ModelSettings(store=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id="resp_prev",
        conversation_id="conv_123",
        prompt=None,
    )

    assert create_calls == [
        {
            "provider_name": "openai",
            "api_key": "openai-key",
            "api_base": None,
        }
    ]
    assert provider.chat_calls == []
    assert provider.responses_calls == []
    assert len(provider.private_responses_calls) == 1
    params = provider.private_responses_calls[0]["params"]
    kwargs = provider.private_responses_calls[0]["kwargs"]
    assert params.model == "gpt-5.4-mini"
    assert params.previous_response_id == "resp_prev"
    assert params.conversation == "conv_123"
    assert kwargs["extra_headers"]["User-Agent"] == f"Agents/Python {__version__}"
    assert response.response_id == "resp_123"
    assert response.output[0].content[0].text == "Hello"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("payload_type", ["dict", "basemodel"])
async def test_any_llm_responses_path_defaults_missing_cache_write_tokens(
    monkeypatch: pytest.MonkeyPatch, payload_type: str
) -> None:
    response_payload = _response("Hello").model_dump()
    response_payload["usage"]["input_tokens_details"].pop("cache_write_tokens")
    response: Any = response_payload
    if payload_type == "basemodel":
        response = GenericResponsesPayload.model_validate(response_payload)

    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=response)
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    model = module.AnyLLMModel(model="openai/gpt-5.4-mini")

    normalized = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(preserve_raw_usage=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    assert normalized.output[0].content[0].text == "Hello"
    assert normalized.usage.input_tokens_details.cache_write_tokens == 0
    assert "cache_write_tokens" not in response_payload["usage"]["input_tokens_details"]
    assert normalized.raw_usage is not None
    assert "cache_write_tokens" not in normalized.raw_usage["input_tokens_details"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_can_force_chat_completions_when_responses_are_supported(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(
        supports_responses=True,
        chat_response=_chat_completion("Hello from chat"),
        responses_response=_response("Hello from responses"),
    )
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openai/gpt-4.1-mini", api="chat_completions")
    response = await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id="resp_prev",
        conversation_id="conv_123",
        prompt=None,
    )

    assert len(provider.chat_calls) == 1
    assert provider.responses_calls == []
    assert response.response_id is None
    assert response.output[0].content[0].text == "Hello from chat"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_forced_responses_errors_when_provider_does_not_support_it(
    monkeypatch,
) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openrouter/openai/gpt-4.1-mini", api="responses")
    with pytest.raises(UserError, match="does not support the Responses API"):
        await model.get_response(
            system_instructions=None,
            input="hi",
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
async def test_any_llm_stream_uses_chat_handler_when_responses_are_unsupported(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_empty_chat_stream())
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    completed = ResponseCompletedEvent(
        type="response.completed",
        response=_response("Hello from stream"),
        sequence_number=1,
    )

    async def fake_handle_stream(response, stream, model=None):
        assert model == "openrouter/openai/gpt-5.4-mini"
        async for _chunk in stream:
            pass
        yield completed

    monkeypatch.setattr(module.ChatCmplStreamHandler, "handle_stream", fake_handle_stream)

    model = AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")
    events = [
        event
        async for event in model.stream_response(
            system_instructions=None,
            input="hi",
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

    assert [event.type for event in events] == ["response.completed"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_stream_passthrough_uses_responses_when_supported(monkeypatch) -> None:
    async def response_stream() -> AsyncIterator[ResponseCompletedEvent]:
        yield ResponseCompletedEvent(
            type="response.completed",
            response=_response("Hello from responses stream"),
            sequence_number=1,
        )

    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=response_stream())
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openai/gpt-5.4-mini")
    events = [
        event
        async for event in model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id="resp_prev",
            conversation_id="conv_123",
            prompt=None,
        )
    ]

    assert [event.type for event in events] == ["response.completed"]
    assert provider.responses_calls == []
    assert provider.private_responses_calls[0]["params"].previous_response_id == "resp_prev"
    assert provider.private_responses_calls[0]["params"].conversation == "conv_123"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_event_type", "terminal_event_cls"),
    [
        ("response.incomplete", ResponseIncompleteEvent),
        ("response.failed", ResponseFailedEvent),
    ],
)
async def test_any_llm_responses_stream_rejects_failed_terminal_events(
    monkeypatch,
    terminal_event_type: str,
    terminal_event_cls: type[Any],
) -> None:
    async def response_stream() -> AsyncIterator[Any]:
        yield terminal_event_cls(
            type=terminal_event_type,
            response=_response("partial", response_id="resp-terminal"),
            sequence_number=1,
        )

    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=response_stream())
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openai/gpt-5.4-mini")
    events = []
    with pytest.raises(ModelBehaviorError, match=terminal_event_type):
        async for event in model.stream_response(
            system_instructions=None,
            input="hi",
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

    assert len(events) == 1
    assert events[0].type == terminal_event_type
    assert events[0].response.id == "resp-terminal"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_rejects_error_event(monkeypatch) -> None:
    async def response_stream() -> AsyncIterator[ResponseErrorEvent]:
        yield ResponseErrorEvent(
            type="error",
            code="invalid_request_error",
            message="bad request",
            param=None,
            sequence_number=1,
        )

    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=response_stream())
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openai/gpt-5.4-mini")
    events = []
    with pytest.raises(ModelBehaviorError, match="invalid_request_error"):
        async for event in model.stream_response(
            system_instructions=None,
            input="hi",
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

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].code == "invalid_request_error"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_path_passes_transport_kwargs_via_private_provider_api(
    monkeypatch,
) -> None:
    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=_response("Hello"))
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openai/gpt-5.4-mini")
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(
            extra_headers={"X-Test-Header": "test"},
            extra_query={"trace": "1"},
            extra_body={"foo": "bar"},
        ),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    assert provider.responses_calls == []
    assert len(provider.private_responses_calls) == 1
    call = provider.private_responses_calls[0]
    assert call["kwargs"]["extra_headers"]["X-Test-Header"] == "test"
    assert call["kwargs"]["extra_query"] == {"trace": "1"}
    assert call["kwargs"]["extra_body"] == {"foo": "bar"}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_prompt_requests_fail_fast(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=_response("Hello"))
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openai/gpt-5.4-mini")
    with pytest.raises(Exception, match="prompt-managed requests"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt={"id": "pmpt_123"},
        )


def test_any_llm_responses_input_sanitizer_strips_none_fields_from_reasoning_items() -> None:
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )
    from agents.extensions.models.any_llm_model import AnyLLMModel

    model = AnyLLMModel(model="openai/gpt-5.4-mini")
    raw_input = [
        {
            "id": "rid1",
            "summary": [{"text": "why", "type": "summary_text"}],
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "thinking"}],
            "status": None,
            "encrypted_content": None,
        }
    ]

    cleaned = model._sanitize_any_llm_responses_input(raw_input)

    assert cleaned == [
        {
            "id": "rid1",
            "summary": [{"text": "why", "type": "summary_text"}],
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "thinking"}],
        }
    ]

    ResponsesParams = importlib.import_module("any_llm.types.responses").ResponsesParams
    params = ResponsesParams(model="dummy", input=cleaned)
    assert isinstance(params.input, list)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_path_sanitizes_replayed_items_before_validation() -> None:
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )
    from agents.extensions.models.any_llm_model import AnyLLMModel

    class ValidatingProvider:
        SUPPORTS_RESPONSES = True

        def __init__(self) -> None:
            self.private_responses_calls: list[dict[str, Any]] = []

        async def aresponses(self, **kwargs: Any) -> Any:
            raise AssertionError("public aresponses path should not be used in this test")

        async def _aresponses(self, params: Any, **kwargs: Any) -> Response:
            self.private_responses_calls.append({"params": params, "kwargs": kwargs})
            return _response("Hello from sanitized replay")

    class TestAnyLLMModel(AnyLLMModel):
        def __init__(self, provider: ValidatingProvider) -> None:
            super().__init__(model="openai/gpt-5.4-mini", api="responses")
            self._provider = provider

        def _get_provider(self) -> Any:
            return self._provider

    provider = ValidatingProvider()
    model = TestAnyLLMModel(provider)
    tools: list[Tool] = []
    handoffs: list[Handoff[Any, Agent[Any]]] = []
    stream_flag: Literal[False] = False

    replay_input = cast(
        list[TResponseInputItem],
        [
            {"role": "user", "content": "What's the weather in Tokyo?"},
            {
                "id": FAKE_RESPONSES_ID,
                "summary": [
                    {"text": "I should call the weather tool first.", "type": "summary_text"}
                ],
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "thinking"}],
                "status": None,
                "provider_data": {"model": "anthropic/fake-responses-model"},
            },
            {
                "id": FAKE_RESPONSES_ID,
                "arguments": '{"city": "Tokyo"}',
                "call_id": "call_weather_123",
                "name": "get_weather",
                "type": "function_call",
                "status": None,
                "provider_data": {"model": "anthropic/fake-responses-model"},
            },
            {
                "type": "function_call_output",
                "call_id": "call_weather_123",
                "output": "The weather in Tokyo is sunny and 22°C.",
            },
        ],
    )

    response = await model._fetch_responses_response(
        system_instructions=None,
        input=replay_input,
        model_settings=ModelSettings(),
        tools=tools,
        output_schema=None,
        handoffs=handoffs,
        previous_response_id=None,
        conversation_id=None,
        stream=stream_flag,
        prompt=None,
    )

    assert response.id == "resp_123"
    assert len(provider.private_responses_calls) == 1
    params = provider.private_responses_calls[0]["params"]
    assert params.input == [
        {"role": "user", "content": "What's the weather in Tokyo?"},
        {
            "arguments": '{"city": "Tokyo"}',
            "call_id": "call_weather_123",
            "name": "get_weather",
            "type": "function_call",
        },
        {
            "type": "function_call_output",
            "call_id": "call_weather_123",
            "output": "The weather in Tokyo is sunny and 22°C.",
        },
    ]


class _RecordingResponsesProvider:
    """Provider stub that records the params any-llm's private responses API receives."""

    SUPPORTS_RESPONSES = True

    def __init__(self, response: Any) -> None:
        self._response = response
        self.private_responses_calls: list[dict[str, Any]] = []

    async def aresponses(self, **kwargs: Any) -> Any:
        raise AssertionError("public aresponses path should not be used in this test")

    async def _aresponses(self, params: Any, **kwargs: Any) -> Any:
        self.private_responses_calls.append({"params": params, "kwargs": kwargs})
        return self._response


def _model_bound_to_provider(provider: Any) -> Any:
    from agents.extensions.models.any_llm_model import AnyLLMModel

    class _BoundAnyLLMModel(AnyLLMModel):
        def _get_provider(self) -> Any:
            return provider

    return _BoundAnyLLMModel(model="openai/gpt-5.4-mini", api="responses")


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_any_llm_responses_path_sends_reasoning_as_a_mapping(stream: bool) -> None:
    """any-llm types `ResponsesParams.reasoning` as a mapping, so it must not be a pair list."""
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )

    async def response_stream() -> AsyncIterator[ResponseCompletedEvent]:
        yield ResponseCompletedEvent(
            type="response.completed",
            response=_response("Hello"),
            sequence_number=1,
        )

    provider = _RecordingResponsesProvider(response_stream() if stream else _response("Hello"))
    model = _model_bound_to_provider(provider)

    # Building `ResponsesParams` validates the payload, so a pair list fails before the
    # provider is reached.
    result = await cast(Any, model)._fetch_responses_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(reasoning=Reasoning(effort="low", summary="concise")),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=stream,
        prompt=None,
    )
    if stream:
        await result.aclose()

    assert len(provider.private_responses_calls) == 1
    params = provider.private_responses_calls[0]["params"]
    # Unset reasoning fields are dropped, matching how this adapter sanitizes replayed input.
    assert params.reasoning == {"effort": "low", "summary": "concise"}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_path_omits_reasoning_when_unset() -> None:
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )

    provider = _RecordingResponsesProvider(_response("Hello"))
    model = _model_bound_to_provider(provider)

    await cast(Any, model)._fetch_responses_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert len(provider.private_responses_calls) == 1
    assert provider.private_responses_calls[0]["params"].reasoning is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_any_llm_responses_path_forwards_prompt_cache_retention(stream: bool) -> None:
    """`ModelSettings.prompt_cache_retention` must reach the any-llm Responses request."""
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )

    provider = _RecordingResponsesProvider(_response("Hello"))
    model = _model_bound_to_provider(provider)

    await cast(Any, model)._fetch_responses_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(prompt_cache_retention="24h"),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=stream,
        prompt=None,
    )

    assert len(provider.private_responses_calls) == 1
    assert provider.private_responses_calls[0]["params"].prompt_cache_retention == "24h"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_path_omits_prompt_cache_retention_when_unset() -> None:
    """An unset retention stays unset instead of pinning a default on the request."""
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )

    provider = _RecordingResponsesProvider(_response("Hello"))
    model = _model_bound_to_provider(provider)

    await cast(Any, model)._fetch_responses_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        previous_response_id=None,
        conversation_id=None,
        stream=False,
        prompt=None,
    )

    assert len(provider.private_responses_calls) == 1
    assert provider.private_responses_calls[0]["params"].prompt_cache_retention is None


def test_any_llm_provider_passes_api_override() -> None:
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )
    from agents.extensions.models.any_llm_model import AnyLLMModel
    from agents.extensions.models.any_llm_provider import AnyLLMProvider

    provider = AnyLLMProvider(api="chat_completions")
    model = provider.get_model("openai/gpt-4.1-mini")

    assert isinstance(model, AnyLLMModel)
    assert model.api == "chat_completions"


def test_any_llm_provider_reads_default_model_at_call_time(monkeypatch: Any) -> None:
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )
    from agents.extensions.models.any_llm_provider import AnyLLMProvider

    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-4.1")
    provider = AnyLLMProvider()

    assert cast(Any, provider.get_model(None)).model == "openai/gpt-4.1"


def test_any_llm_reasoning_objects_prefer_content_attributes_over_iterable_pairs() -> None:
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )
    from any_llm.types.completion import Reasoning

    from agents.extensions.models.any_llm_model import _extract_any_llm_reasoning_text

    delta = pytypes.SimpleNamespace(reasoning=Reasoning(content="用户"))

    assert _extract_any_llm_reasoning_text(delta) == "用户"


def test_any_llm_split_does_not_duplicate_content_or_thinking(monkeypatch) -> None:
    """Splitting multi-tool assistant messages must not duplicate text/thinking blocks.

    Anthropic's extended thinking API rejects requests that include the same signed
    thinking block more than once, and duplicated assistant text corrupts conversation
    history. Only the first split should retain content, thinking_blocks, and
    reasoning_content; subsequent splits should carry the tool_call alone.
    """
    provider = FakeAnyLLMProvider(supports_responses=False)
    module, _ = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="anthropic/claude-3-5-sonnet")
    messages: list[Any] = [
        {"role": "user", "content": "Search both"},
        {
            "role": "assistant",
            "content": "Looking up both queries.",
            "thinking_blocks": [{"type": "thinking", "thinking": "plan", "signature": "sig_abc"}],
            "reasoning_content": "internal plan",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "s", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "s", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok1"},
        {"role": "tool", "tool_call_id": "call_2", "content": "ok2"},
    ]

    result = model._fix_tool_message_ordering(messages)

    assistants = [m for m in result if m.get("role") == "assistant"]
    assert len(assistants) == 2
    # First split keeps the shared fields.
    assert assistants[0].get("content") == "Looking up both queries."
    assert "thinking_blocks" in assistants[0]
    assert "reasoning_content" in assistants[0]
    # Second split must NOT duplicate them.
    assert "content" not in assistants[1]
    assert "thinking_blocks" not in assistants[1]
    assert "reasoning_content" not in assistants[1]
    # Tool calls are still split one-per-message.
    assert assistants[0]["tool_calls"][0]["id"] == "call_1"
    assert assistants[1]["tool_calls"][0]["id"] == "call_2"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_sets_logprobs_when_top_logprobs_set(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, _ = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openrouter/openai/gpt-5.4-mini", api_key="k")
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(top_logprobs=2),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    # The Chat Completions API rejects top_logprobs unless logprobs is True.
    assert provider.chat_calls[0]["top_logprobs"] == 2
    assert provider.chat_calls[0]["logprobs"] is True


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_omits_logprobs_when_top_logprobs_unset(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, _ = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openrouter/openai/gpt-5.4-mini", api_key="k")
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    assert "logprobs" not in provider.chat_calls[0]


class _ClosableStream:
    """Stands in for the iterator any-llm returns, recording how often it was closed.

    These fakes deliberately cover only the SDK's own boundary. any-llm's exception and
    provider wrappers (`utils/exception_handler.py::_wrap_async_iterator`, the per-provider
    `chunk_iterator` / `_stream_completion_async` generators) delegate with bare
    `async for ... yield` as of 1.11.0 and do not forward `aclose()` to the underlying
    transport, so no assertion here should be read as proving the transport was closed.
    """

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self.aclose_calls = 0

    def __aiter__(self) -> _ClosableStream:
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _FailingCloseStream(_ClosableStream):
    """Raises from `aclose` after recording the cleanup attempt."""

    async def aclose(self) -> None:
        self.aclose_calls += 1
        raise RuntimeError("close-failure")


class _BlockingStream(_ClosableStream):
    """Blocks forever after its chunks are exhausted so the consumer can be cancelled."""

    def __init__(self, chunks: list[Any], blocked: asyncio.Event) -> None:
        super().__init__(chunks)
        self._blocked = blocked

    async def __anext__(self) -> Any:
        if self._chunks:
            return self._chunks.pop(0)
        self._blocked.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _SlowCloseStream(_BlockingStream):
    """Blocks in `aclose` until released, mirroring a close that waits on transport I/O."""

    def __init__(self, chunks: list[Any], blocked: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(chunks, blocked)
        self._release = release
        self.aclose_completed = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        await self._release.wait()
        self.aclose_completed += 1


def _chat_chunk(text: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chunk_123",
        created=0,
        model="fake-model",
        object="chat.completion.chunk",
        choices=[ChunkChoice(index=0, delta=ChoiceDelta(content=text))],
    )


def _completed_event() -> ResponseCompletedEvent:
    return ResponseCompletedEvent(
        type="response.completed",
        response=_response("Hello"),
        sequence_number=1,
    )


def _stream_events(model: Any, tracing: ModelTracing = ModelTracing.DISABLED) -> Any:
    return model.stream_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=tracing,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )


def _chat_module_and_model_with_stream(monkeypatch, stream: _ClosableStream) -> tuple[Any, Any]:
    """Build an AnyLLMModel whose Chat Completions path yields one completed event."""
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=stream)
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)

    async def fake_handle_stream(response, chunk_stream, model=None):
        async for _chunk in chunk_stream:
            pass
        yield _completed_event()

    monkeypatch.setattr(module.ChatCmplStreamHandler, "handle_stream", fake_handle_stream)
    return module, module.AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")


def _chat_model_with_stream(monkeypatch, stream: _ClosableStream) -> Any:
    _module, model = _chat_module_and_model_with_stream(monkeypatch, stream)
    return model


def _responses_module_and_model_with_stream(
    monkeypatch, stream: _ClosableStream
) -> tuple[Any, Any]:
    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=stream)
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)
    return module, module.AnyLLMModel(model="openai/gpt-5.4-mini")


def _responses_model_with_stream(monkeypatch, stream: _ClosableStream) -> Any:
    _module, model = _responses_module_and_model_with_stream(monkeypatch, stream)
    return model


def _capture_spans(monkeypatch, module: Any, factory_name: str) -> list[Any]:
    """Record the live span objects the module creates so tests can read them mid-stream."""
    original = getattr(module, factory_name)
    captured: list[Any] = []

    def recording_factory(*args: Any, **kwargs: Any) -> Any:
        span = original(*args, **kwargs)
        captured.append(span)
        return span

    monkeypatch.setattr(module, factory_name, recording_factory)
    return captured


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_stream_ignores_close_failure_after_terminal_event(monkeypatch) -> None:
    """A completed Chat Completions response stays successful when provider cleanup fails."""
    stream = _FailingCloseStream([_chat_chunk("Hello")])
    model = _chat_model_with_stream(monkeypatch, stream)

    events = [event async for event in _stream_events(model)]

    assert [event.type for event in events] == ["response.completed"]
    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_stream_ignores_close_failure_when_closed_at_terminal_event(
    monkeypatch,
) -> None:
    """Terminal state must be recorded before the completed event is yielded."""
    stream = _FailingCloseStream([_chat_chunk("Hello")])
    model = _chat_model_with_stream(monkeypatch, stream)
    stream_agen = cast(Any, _stream_events(model))

    async for event in stream_agen:
        if event.type == "response.completed":
            break
    await stream_agen.aclose()

    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_stream_closes_provider_stream_after_cancellation(monkeypatch) -> None:
    """Cancelling the consumer must still release the provider stream."""
    blocked = asyncio.Event()
    stream = _BlockingStream([_chat_chunk("He")], blocked)
    model = _chat_model_with_stream(monkeypatch, stream)
    stream_agen = cast(Any, _stream_events(model))

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

    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_stream_does_not_block_cancellation_on_slow_close(monkeypatch) -> None:
    """A provider close that waits on transport I/O must not delay cancellation."""
    blocked = asyncio.Event()
    release = asyncio.Event()
    stream = _SlowCloseStream([_chat_chunk("He")], blocked, release)
    model = _chat_model_with_stream(monkeypatch, stream)
    stream_agen = cast(Any, _stream_events(model))

    async def consume() -> None:
        async for _event in stream_agen:
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert stream.aclose_calls == 1
        assert stream.aclose_completed == 0

        release.set()
        for _ in range(200):
            if stream.aclose_completed == 1:
                break
            await asyncio.sleep(0.01)
        assert stream.aclose_completed == 1
    finally:
        release.set()
        task.cancel()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_stream_propagates_close_failure_before_terminal_event(
    monkeypatch,
) -> None:
    """Cleanup failures before completion stay observable by the caller."""
    stream = _FailingCloseStream([_chat_chunk("Hello")])
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=stream)
    module, _create_calls = _import_any_llm_module(monkeypatch, provider)

    async def fake_handle_stream(response, chunk_stream, model=None):
        yield ResponseCreatedEvent(
            type="response.created",
            response=_response("partial"),
            sequence_number=0,
        )
        async for _chunk in chunk_stream:
            pass
        yield _completed_event()

    monkeypatch.setattr(module.ChatCmplStreamHandler, "handle_stream", fake_handle_stream)
    model = module.AnyLLMModel(model="openrouter/openai/gpt-5.4-mini")
    stream_agen = cast(Any, _stream_events(model))

    first_event = await anext(stream_agen)
    assert first_event.type == "response.created"

    with pytest.raises(RuntimeError, match="close-failure"):
        await stream_agen.aclose()

    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_ignores_close_failure_after_terminal_event(
    monkeypatch,
) -> None:
    """A completed Responses stream stays successful when provider cleanup fails."""
    stream = _FailingCloseStream([_completed_event()])
    model = _responses_model_with_stream(monkeypatch, stream)

    events = [event async for event in _stream_events(model)]

    assert [event.type for event in events] == ["response.completed"]
    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_ignores_close_failure_after_terminal_failure(
    monkeypatch,
) -> None:
    """A cleanup failure must not replace the terminal failure the caller should see."""
    stream = _FailingCloseStream(
        [
            ResponseFailedEvent(
                type="response.failed",
                response=_response("partial", response_id="resp-terminal"),
                sequence_number=1,
            )
        ]
    )
    model = _responses_model_with_stream(monkeypatch, stream)

    with pytest.raises(ModelBehaviorError, match="response.failed"):
        async for _event in _stream_events(model):
            pass

    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_propagates_close_failure_before_terminal_event(
    monkeypatch,
) -> None:
    """Cleanup failures before completion stay observable by the caller."""
    stream = _FailingCloseStream(
        [
            ResponseCreatedEvent(
                type="response.created",
                response=_response("partial"),
                sequence_number=0,
            ),
            _completed_event(),
        ]
    )
    model = _responses_model_with_stream(monkeypatch, stream)
    stream_agen = cast(Any, _stream_events(model))

    first_event = await anext(stream_agen)
    assert first_event.type == "response.created"

    with pytest.raises(RuntimeError, match="close-failure"):
        await stream_agen.aclose()

    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_closes_provider_stream_after_cancellation(
    monkeypatch,
) -> None:
    """Cancelling the consumer must still release the provider Responses stream."""
    blocked = asyncio.Event()
    stream = _BlockingStream(
        [
            ResponseCreatedEvent(
                type="response.created",
                response=_response("partial"),
                sequence_number=0,
            )
        ],
        blocked,
    )
    model = _responses_model_with_stream(monkeypatch, stream)
    stream_agen = cast(Any, _stream_events(model))

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

    assert stream.aclose_calls == 1


class _CloseSignalingStream(_ClosableStream):
    """Signals when `aclose` starts and blocks until released, so a test can cancel mid-close."""

    def __init__(
        self,
        chunks: list[Any],
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


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_stream_populates_span_before_yielding_completed(monkeypatch) -> None:
    """The generation span must carry output and usage when the completed event is delivered."""
    stream = _ClosableStream([_chat_chunk("Hello")])
    module, model = _chat_module_and_model_with_stream(monkeypatch, stream)
    spans = _capture_spans(monkeypatch, module, "generation_span")

    with trace(workflow_name="any-llm-chat-span"):
        stream_agen = cast(Any, _stream_events(model, ModelTracing.ENABLED))
        async for event in stream_agen:
            if event.type == "response.completed":
                # Assert while the generator is suspended at the terminal yield.
                [span] = spans
                assert span.span_data.output == [_response("Hello").model_dump()]
                assert span.span_data.usage == {
                    "requests": 1,
                    "input_tokens": 11,
                    "output_tokens": 13,
                    "total_tokens": 24,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                }
                break
        await stream_agen.aclose()

    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_populates_span_before_yielding_completed(
    monkeypatch,
) -> None:
    """The response span must carry the response and input when the completed event arrives."""
    stream = _ClosableStream([_completed_event()])
    module, model = _responses_module_and_model_with_stream(monkeypatch, stream)
    spans = _capture_spans(monkeypatch, module, "response_span")

    with trace(workflow_name="any-llm-responses-span"):
        stream_agen = cast(Any, _stream_events(model, ModelTracing.ENABLED))
        async for event in stream_agen:
            if event.type == "response.completed":
                [span] = spans
                assert span.span_data.response is not None
                assert span.span_data.response.id == "resp_123"
                assert span.span_data.input == "hi"
                break
        await stream_agen.aclose()

    assert stream.aclose_calls == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_chat_stream_lets_in_flight_close_finish_after_cancellation(
    monkeypatch,
) -> None:
    """Cancelling during `aclose` continues that close instead of starting a second one."""
    close_started = asyncio.Event()
    release = asyncio.Event()
    stream = _CloseSignalingStream([_chat_chunk("Hello")], close_started, release)
    model = _chat_model_with_stream(monkeypatch, stream)

    async def consume() -> None:
        async for _event in _stream_events(model):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(close_started.wait(), timeout=5)
        assert stream.aclose_calls == 1
        assert stream.aclose_completed == 0

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        # The cancelled consumer must not have started a second close.
        assert stream.aclose_calls == 1
        assert stream.aclose_completed == 0

        release.set()
        for _ in range(200):
            if stream.aclose_completed == 1:
                break
            await asyncio.sleep(0.01)

        assert stream.aclose_calls == 1
        assert stream.aclose_completed == 1
    finally:
        release.set()
        task.cancel()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_lets_in_flight_close_finish_after_cancellation(
    monkeypatch,
) -> None:
    """Cancelling during `aclose` continues that close instead of starting a second one."""
    close_started = asyncio.Event()
    release = asyncio.Event()
    stream = _CloseSignalingStream([_completed_event()], close_started, release)
    model = _responses_model_with_stream(monkeypatch, stream)

    async def consume() -> None:
        async for _event in _stream_events(model):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(close_started.wait(), timeout=5)
        assert stream.aclose_calls == 1
        assert stream.aclose_completed == 0

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        assert stream.aclose_calls == 1
        assert stream.aclose_completed == 0

        release.set()
        for _ in range(200):
            if stream.aclose_completed == 1:
                break
            await asyncio.sleep(0.01)

        assert stream.aclose_calls == 1
        assert stream.aclose_completed == 1
    finally:
        release.set()
        task.cancel()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_counts_request_when_usage_is_absent(monkeypatch) -> None:
    """The AnyLLM Responses stream must count its request like the non-streaming path.

    `get_response` already reports one request when the provider omits usage. The streaming
    path went through the run loop's usage-less fallback and reported zero, so the same call
    was counted differently depending only on whether it was streamed.
    """
    completed = _response("Hello")
    completed.usage = None

    async def response_stream() -> AsyncIterator[ResponseCompletedEvent]:
        yield ResponseCompletedEvent(
            type="response.completed", response=completed, sequence_number=1
        )

    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=response_stream())
    module, _ = _import_any_llm_module(monkeypatch, provider)

    events = [
        event
        async for event in module.AnyLLMModel(model="openai/gpt-5.4-mini").stream_response(
            system_instructions=None,
            input="hi",
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

    from agents.usage import _requests_for_response_without_usage

    terminal = events[-1]
    assert isinstance(terminal, ResponseCompletedEvent)
    # No usage payload is synthesized, so token counts are not reported as real zeros.
    assert terminal.response.usage is None
    assert _requests_for_response_without_usage(terminal.response) == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_any_llm_responses_stream_with_usage_is_not_marked(monkeypatch) -> None:
    """A response that did report usage must not also be counted by the usage-less path."""

    async def response_stream() -> AsyncIterator[ResponseCompletedEvent]:
        yield ResponseCompletedEvent(
            type="response.completed", response=_response("Hello"), sequence_number=1
        )

    provider = FakeAnyLLMProvider(supports_responses=True, responses_response=response_stream())
    module, _ = _import_any_llm_module(monkeypatch, provider)

    events = [
        event
        async for event in module.AnyLLMModel(model="openai/gpt-5.4-mini").stream_response(
            system_instructions=None,
            input="hi",
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

    from agents.usage import _requests_for_response_without_usage

    terminal = events[-1]
    assert isinstance(terminal, ResponseCompletedEvent)
    assert terminal.response.usage is not None
    assert _requests_for_response_without_usage(terminal.response) == 0

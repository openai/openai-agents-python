from __future__ import annotations

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
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails
from openai.types.responses import Response, ResponseCompletedEvent, ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)
from pydantic import BaseModel

from agents import (
    Agent,
    Handoff,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
    __version__,
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
            prompt_tokens_details=PromptTokensDetails(cached_tokens=2),
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
            input_tokens_details=InputTokensDetails(cached_tokens=0),
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
async def test_any_llm_chat_path_is_used_when_responses_are_unsupported(monkeypatch) -> None:
    provider = FakeAnyLLMProvider(supports_responses=False, chat_response=_chat_completion("Hello"))
    module, create_calls = _import_any_llm_module(monkeypatch, provider)
    AnyLLMModel = module.AnyLLMModel

    model = AnyLLMModel(model="openrouter/openai/gpt-5.4-mini", api_key="router-key")
    response = await model.get_response(
        system_instructions="You are terse.",
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


def test_any_llm_reasoning_objects_prefer_content_attributes_over_iterable_pairs() -> None:
    pytest.importorskip(
        "any_llm",
        reason="`any-llm-sdk` is only available when the optional dependency is installed.",
    )
    from any_llm.types.completion import Reasoning

    from agents.extensions.models.any_llm_model import _extract_any_llm_reasoning_text

    delta = pytypes.SimpleNamespace(reasoning=Reasoning(content="用户"))

    assert _extract_any_llm_reasoning_text(delta) == "用户"

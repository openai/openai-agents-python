from __future__ import annotations

from typing import Any, cast

import httpx
import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage

from agents.extensions.models.litellm_model import LitellmModel
from agents.items import TResponseInputItem
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.reasoning_content_replay import ReasoningContentReplayContext

REASONING_CONTENT_MODEL_A = "reasoning-content-model-a"
REASONING_CONTENT_MODEL_B = "reasoning-content-model-b"
# The converter currently keys Anthropic thinking-block reconstruction off the model name,
# so this test model keeps the "anthropic" substring while staying otherwise generic.
REASONING_CONTENT_MODEL_C = "reasoning-content-model-c-anthropic"


def _second_turn_input_items(model_name: str) -> list[TResponseInputItem]:
    return cast(
        list[TResponseInputItem],
        [
            {"role": "user", "content": "What's the weather in Tokyo?"},
            {
                "id": "__fake_id__",
                "summary": [
                    {"text": "I should call the weather tool first.", "type": "summary_text"}
                ],
                "type": "reasoning",
                "content": None,
                "encrypted_content": None,
                "status": None,
                "provider_data": {"model": model_name, "response_id": "chatcmpl-test"},
            },
            {
                "arguments": '{"city": "Tokyo"}',
                "call_id": "call_weather_123",
                "name": "get_weather",
                "type": "function_call",
                "id": "__fake_id__",
                "status": None,
                "provider_data": {"model": model_name},
            },
            {
                "type": "function_call_output",
                "call_id": "call_weather_123",
                "output": "The weather in Tokyo is sunny and 22°C.",
            },
        ],
    )


def _second_turn_input_items_with_message(model_name: str) -> list[TResponseInputItem]:
    return cast(
        list[TResponseInputItem],
        [
            {"role": "user", "content": "What's the weather in Tokyo?"},
            {
                "id": "__fake_id__",
                "summary": [
                    {"text": "I should call the weather tool first.", "type": "summary_text"}
                ],
                "type": "reasoning",
                "content": None,
                "encrypted_content": None,
                "status": None,
                "provider_data": {"model": model_name, "response_id": "chatcmpl-test"},
            },
            {
                "id": "__fake_id__",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "I'll call the weather tool now.",
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
                "provider_data": {"model": model_name, "response_id": "chatcmpl-test"},
            },
            {
                "arguments": '{"city": "Tokyo"}',
                "call_id": "call_weather_123",
                "name": "get_weather",
                "type": "function_call",
                "id": "__fake_id__",
                "status": None,
                "provider_data": {"model": model_name},
            },
            {
                "type": "function_call_output",
                "call_id": "call_weather_123",
                "output": "The weather in Tokyo is sunny and 22°C.",
            },
        ],
    )


def _second_turn_input_items_with_file_search(model_name: str) -> list[TResponseInputItem]:
    return cast(
        list[TResponseInputItem],
        [
            {"role": "user", "content": "Find notes about Tokyo weather."},
            {
                "id": "__fake_id__",
                "summary": [
                    {"text": "I should search the knowledge base first.", "type": "summary_text"}
                ],
                "type": "reasoning",
                "content": None,
                "encrypted_content": None,
                "status": None,
                "provider_data": {"model": model_name, "response_id": "chatcmpl-test"},
            },
            {
                "id": "__fake_file_search_id__",
                "queries": ["Tokyo weather"],
                "status": "completed",
                "type": "file_search_call",
            },
        ],
    )


def _second_turn_input_items_with_message_then_reasoning(
    model_name: str,
) -> list[TResponseInputItem]:
    return cast(
        list[TResponseInputItem],
        [
            {"role": "user", "content": "What's the weather in Tokyo?"},
            {
                "id": "__fake_id__",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "I'll call the weather tool now.",
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
                "provider_data": {"model": model_name, "response_id": "chatcmpl-test"},
            },
            {
                "id": "__fake_id__",
                "summary": [
                    {"text": "I should call the weather tool first.", "type": "summary_text"}
                ],
                "type": "reasoning",
                "content": None,
                "encrypted_content": None,
                "status": None,
                "provider_data": {"model": model_name, "response_id": "chatcmpl-test"},
            },
            {
                "arguments": '{"city": "Tokyo"}',
                "call_id": "call_weather_123",
                "name": "get_weather",
                "type": "function_call",
                "id": "__fake_id__",
                "status": None,
                "provider_data": {"model": model_name},
            },
            {
                "type": "function_call_output",
                "call_id": "call_weather_123",
                "output": "The weather in Tokyo is sunny and 22°C.",
            },
        ],
    )


def _second_turn_input_items_with_thinking_blocks(model_name: str) -> list[TResponseInputItem]:
    return cast(
        list[TResponseInputItem],
        [
            {"role": "user", "content": "What's the weather in Tokyo?"},
            {
                "id": "__fake_id__",
                "summary": [
                    {"text": "I should call the weather tool first.", "type": "summary_text"}
                ],
                "type": "reasoning",
                "content": [
                    {
                        "type": "reasoning_text",
                        "text": "First, I need to inspect the request.",
                    }
                ],
                "encrypted_content": "test-signature",
                "status": None,
                "provider_data": {"model": model_name, "response_id": "chatcmpl-test"},
            },
            {
                "arguments": '{"city": "Tokyo"}',
                "call_id": "call_weather_123",
                "name": "get_weather",
                "type": "function_call",
                "id": "__fake_id__",
                "status": None,
                "provider_data": {"model": model_name},
            },
            {
                "type": "function_call_output",
                "call_id": "call_weather_123",
                "output": "The weather in Tokyo is sunny and 22°C.",
            },
        ],
    )


def _assistant_with_tool_calls(messages: list[Any]) -> dict[str, Any]:
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            return msg
    raise AssertionError("Expected an assistant message with tool_calls.")


def test_converter_keeps_default_reasoning_replay_behavior_for_non_default_model() -> None:
    messages = Converter.items_to_messages(
        _second_turn_input_items(REASONING_CONTENT_MODEL_A),
        model=REASONING_CONTENT_MODEL_A,
    )

    assistant = _assistant_with_tool_calls(messages)
    assert "reasoning_content" not in assistant


def test_converter_preserves_reasoning_content_across_output_message_with_hook() -> None:
    def should_replay_reasoning_content(_context: ReasoningContentReplayContext) -> bool:
        return True

    messages = Converter.items_to_messages(
        _second_turn_input_items_with_message(REASONING_CONTENT_MODEL_A),
        model=REASONING_CONTENT_MODEL_A,
        should_replay_reasoning_content=should_replay_reasoning_content,
    )

    assistant = _assistant_with_tool_calls(messages)
    assert assistant["content"] == "I'll call the weather tool now."
    assert assistant["reasoning_content"] == "I should call the weather tool first."


def test_converter_replays_reasoning_content_when_reasoning_follows_message_with_hook() -> None:
    def should_replay_reasoning_content(_context: ReasoningContentReplayContext) -> bool:
        return True

    messages = Converter.items_to_messages(
        _second_turn_input_items_with_message_then_reasoning(REASONING_CONTENT_MODEL_A),
        model=REASONING_CONTENT_MODEL_A,
        should_replay_reasoning_content=should_replay_reasoning_content,
    )

    assistant = _assistant_with_tool_calls(messages)
    assert assistant["content"] == "I'll call the weather tool now."
    assert assistant["reasoning_content"] == "I should call the weather tool first."


def test_converter_replays_reasoning_content_for_file_search_call_with_hook() -> None:
    def should_replay_reasoning_content(_context: ReasoningContentReplayContext) -> bool:
        return True

    messages = Converter.items_to_messages(
        _second_turn_input_items_with_file_search(REASONING_CONTENT_MODEL_A),
        model=REASONING_CONTENT_MODEL_A,
        should_replay_reasoning_content=should_replay_reasoning_content,
    )

    assistant = _assistant_with_tool_calls(messages)
    assert assistant["reasoning_content"] == "I should search the knowledge base first."
    assert assistant["tool_calls"][0]["function"]["name"] == "file_search_call"


def test_converter_replays_reasoning_content_with_thinking_blocks_and_hook() -> None:
    def should_replay_reasoning_content(_context: ReasoningContentReplayContext) -> bool:
        return True

    messages = Converter.items_to_messages(
        _second_turn_input_items_with_thinking_blocks(REASONING_CONTENT_MODEL_C),
        model=REASONING_CONTENT_MODEL_C,
        preserve_thinking_blocks=True,
        should_replay_reasoning_content=should_replay_reasoning_content,
    )

    assistant = _assistant_with_tool_calls(messages)
    assert assistant["reasoning_content"] == "I should call the weather tool first."
    assert assistant["content"][0]["type"] == "thinking"
    assert assistant["content"][0]["thinking"] == "First, I need to inspect the request."


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_openai_chatcompletions_hook_can_enable_reasoning_content_replay() -> None:
    captured: dict[str, Any] = {}
    contexts: list[ReasoningContentReplayContext] = []

    def should_replay_reasoning_content(context: ReasoningContentReplayContext) -> bool:
        contexts.append(context)
        return context.model == REASONING_CONTENT_MODEL_B

    class MockChatCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            msg = ChatCompletionMessage(role="assistant", content="done")
            choice = Choice(index=0, message=msg, finish_reason="stop")
            return ChatCompletion(
                id="test-id",
                created=0,
                model=REASONING_CONTENT_MODEL_B,
                object="chat.completion",
                choices=[choice],
                usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
            )

    class MockChat:
        def __init__(self):
            self.completions = MockChatCompletions()

    class MockClient:
        def __init__(self):
            self.chat = MockChat()
            self.base_url = httpx.URL("https://example.com/v1/")

    model = OpenAIChatCompletionsModel(
        model=REASONING_CONTENT_MODEL_B,
        openai_client=cast(Any, MockClient()),
        should_replay_reasoning_content=should_replay_reasoning_content,
    )

    await model.get_response(
        system_instructions=None,
        input=_second_turn_input_items(REASONING_CONTENT_MODEL_B),
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    assistant = _assistant_with_tool_calls(cast(list[dict[str, Any]], captured["messages"]))
    assert assistant["reasoning_content"] == "I should call the weather tool first."
    assert len(contexts) == 1
    assert contexts[0].model == REASONING_CONTENT_MODEL_B
    assert contexts[0].base_url == "https://example.com/v1"
    assert contexts[0].reasoning.origin_model == REASONING_CONTENT_MODEL_B


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_litellm_hook_can_enable_reasoning_content_replay(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    contexts: list[ReasoningContentReplayContext] = []

    def should_replay_reasoning_content(context: ReasoningContentReplayContext) -> bool:
        contexts.append(context)
        return context.model == REASONING_CONTENT_MODEL_B

    async def fake_acompletion(model, messages=None, **kwargs):
        captured["messages"] = messages
        msg = Message(role="assistant", content="done")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(
        model=REASONING_CONTENT_MODEL_B,
        should_replay_reasoning_content=should_replay_reasoning_content,
    )

    await model.get_response(
        system_instructions=None,
        input=_second_turn_input_items(REASONING_CONTENT_MODEL_B),
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    assistant = _assistant_with_tool_calls(cast(list[dict[str, Any]], captured["messages"]))
    assert assistant["reasoning_content"] == "I should call the weather tool first."
    assert len(contexts) == 1
    assert contexts[0].model == REASONING_CONTENT_MODEL_B
    assert contexts[0].base_url is None
    assert contexts[0].reasoning.origin_model == REASONING_CONTENT_MODEL_B

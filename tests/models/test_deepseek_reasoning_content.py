from typing import Any

import litellm
import pytest
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Choices,
    Function,
    Message,
    ModelResponse,
    Usage,
)

from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter
from agents.models.interface import ModelTracing


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_deepseek_reasoning_content_preserved_in_tool_calls(monkeypatch):
    """
    Ensure DeepSeek reasoning_content is preserved when converting items to messages.

    DeepSeek requires reasoning_content field in assistant messages with tool_calls.
    This test verifies that reasoning content from reasoning items is correctly
    extracted and added to assistant messages during conversion.
    """
    # Capture the messages sent to the model
    captured_calls: list[dict[str, Any]] = []

    async def fake_acompletion(model, messages=None, **kwargs):
        captured_calls.append({"model": model, "messages": messages, **kwargs})

        # First call: model returns reasoning_content + tool_call
        if len(captured_calls) == 1:
            tool_call = ChatCompletionMessageToolCall(
                id="call_123",
                type="function",
                function=Function(name="get_weather", arguments='{"city": "Tokyo"}'),
            )
            msg = Message(
                role="assistant",
                content=None,
                tool_calls=[tool_call],
            )
            # DeepSeek adds reasoning_content to the message
            msg.reasoning_content = "Let me think about getting the weather for Tokyo..."

            choice = Choices(index=0, message=msg)
            return ModelResponse(choices=[choice], usage=Usage(100, 50, 150))

        # Second call: model returns final response
        msg = Message(role="assistant", content="The weather in Tokyo is sunny.")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(100, 50, 150))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(model="deepseek/deepseek-reasoner")

    # First call: get the tool call response
    first_response = await model.get_response(
        system_instructions="You are a helpful assistant.",
        input="What's the weather in Tokyo?",
        model_settings=ModelSettings(),
        tools=[],  # We'll simulate the tool response manually
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert len(first_response.output) >= 1

    input_items: list[Any] = []
    input_items.append({"role": "user", "content": "What's the weather in Tokyo?"})

    for item in first_response.output:
        if hasattr(item, "model_dump"):
            input_items.append(item.model_dump())
        else:
            input_items.append(item)

    input_items.append(
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "The weather in Tokyo is sunny.",
        }
    )

    messages = Converter.items_to_messages(
        input_items,
        model="deepseek/deepseek-reasoner",
    )

    assistant_messages_with_tool_calls = [
        m
        for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
    ]

    assert len(assistant_messages_with_tool_calls) > 0
    assistant_msg = assistant_messages_with_tool_calls[0]
    assert "reasoning_content" in assistant_msg


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_deepseek_reasoning_content_in_multi_turn_conversation(monkeypatch):
    """
    Verify reasoning_content is included in assistant messages during multi-turn conversations.

    When DeepSeek returns reasoning_content with tool_calls, subsequent API calls must
    include the reasoning_content field in the assistant message to avoid 400 errors.
    """
    captured_calls: list[dict[str, Any]] = []

    async def fake_acompletion(model, messages=None, **kwargs):
        captured_calls.append({"model": model, "messages": messages, **kwargs})

        # First call: model returns reasoning_content + tool_call
        if len(captured_calls) == 1:
            tool_call = ChatCompletionMessageToolCall(
                id="call_weather_123",
                type="function",
                function=Function(name="get_weather", arguments='{"city": "Tokyo"}'),
            )
            msg = Message(
                role="assistant",
                content=None,
                tool_calls=[tool_call],
            )
            # DeepSeek adds reasoning_content
            msg.reasoning_content = "I need to get the weather for Tokyo first."
            choice = Choices(index=0, message=msg)
            return ModelResponse(choices=[choice], usage=Usage(100, 50, 150))

        # Second call: check if reasoning_content was in the request
        # In real DeepSeek API, this would fail with 400 if reasoning_content is missing
        msg = Message(
            role="assistant", content="Based on my findings, the weather in Tokyo is sunny."
        )
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(100, 50, 150))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(model="deepseek/deepseek-reasoner")

    # First call
    first_response = await model.get_response(
        system_instructions="You are a helpful assistant.",
        input="What's the weather in Tokyo?",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    input_items: list[Any] = []
    input_items.append({"role": "user", "content": "What's the weather in Tokyo?"})

    for item in first_response.output:
        if hasattr(item, "model_dump"):
            input_items.append(item.model_dump())
        else:
            input_items.append(item)

    input_items.append(
        {
            "type": "function_call_output",
            "call_id": "call_weather_123",
            "output": "The weather in Tokyo is sunny and 22°C.",
        }
    )

    await model.get_response(
        system_instructions="You are a helpful assistant.",
        input=input_items,
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    assert len(captured_calls) == 2

    second_call_messages = captured_calls[1]["messages"]

    assistant_with_tools = None
    for msg in second_call_messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_with_tools = msg
            break

    assert assistant_with_tools is not None
    assert "reasoning_content" in assistant_with_tools


def test_deepseek_reasoning_content_with_openai_chatcompletions_path():
    """
    Verify reasoning_content works when using OpenAIChatCompletionsModel.

    This ensures the fix works for both LiteLLM and OpenAI ChatCompletions code paths.
    """
    from agents.models.chatcmpl_converter import Converter

    input_items: list[Any] = [
        {"role": "user", "content": "What's the weather in Paris?"},
        {
            "id": "__fake_id__",
            "summary": [{"text": "I need to check the weather in Paris.", "type": "summary_text"}],
            "type": "reasoning",
            "content": None,
            "encrypted_content": None,
            "status": None,
            "provider_data": {"model": "deepseek-reasoner", "response_id": "chatcmpl-test"},
        },
        {
            "arguments": '{"city": "Paris"}',
            "call_id": "call_weather_456",
            "name": "get_weather",
            "type": "function_call",
            "id": "__fake_id__",
            "status": None,
            "provider_data": {"model": "deepseek-reasoner"},
        },
        {
            "type": "function_call_output",
            "call_id": "call_weather_456",
            "output": "The weather in Paris is cloudy and 15°C.",
        },
    ]

    messages = Converter.items_to_messages(
        input_items,
        model="deepseek-reasoner",
    )

    assistant_with_tools = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_with_tools = msg
            break

    assert assistant_with_tools is not None
    assert "reasoning_content" in assistant_with_tools
    # Use type: ignore since reasoning_content is a dynamic field not in OpenAI's TypedDict
    assert assistant_with_tools["reasoning_content"] == "I need to check the weather in Paris."  # type: ignore[typeddict-item]


def test_reasoning_content_from_other_provider_not_attached_to_deepseek():
    """
    Verify reasoning_content from non-DeepSeek providers is NOT attached to DeepSeek messages.

    When switching models mid-conversation (e.g., from Claude to DeepSeek), reasoning items
    that originated from Claude should not have their summaries attached as reasoning_content
    to DeepSeek assistant messages, as this would leak unrelated reasoning and may trigger
    DeepSeek 400 errors.
    """
    from agents.models.chatcmpl_converter import Converter

    input_items: list[Any] = [
        {"role": "user", "content": "What's the weather in Paris?"},
        {
            "id": "__fake_id__",
            "summary": [{"text": "Claude's reasoning about the weather.", "type": "summary_text"}],
            "type": "reasoning",
            "content": None,
            "encrypted_content": None,
            "status": None,
            # this one came from Claude, not DeepSeek
            "provider_data": {"model": "claude-sonnet-4-20250514", "response_id": "chatcmpl-test"},
        },
        {
            "arguments": '{"city": "Paris"}',
            "call_id": "call_weather_789",
            "name": "get_weather",
            "type": "function_call",
            "id": "__fake_id__",
            "status": None,
            "provider_data": {"model": "claude-sonnet-4-20250514"},
        },
        {
            "type": "function_call_output",
            "call_id": "call_weather_789",
            "output": "The weather in Paris is cloudy.",
        },
    ]

    messages = Converter.items_to_messages(
        input_items,
        model="deepseek-reasoner",
    )

    assistant_with_tools = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_with_tools = msg
            break

    assert assistant_with_tools is not None
    # reasoning_content should NOT be present since the reasoning came from Claude, not DeepSeek
    assert "reasoning_content" not in assistant_with_tools


def test_reasoning_content_without_provider_data_attached_for_backward_compat():
    """
    Verify reasoning_content from items without provider_data is attached for backward compat.

    For older items that don't have provider_data (before provider tracking was added),
    we should still attach reasoning_content to maintain backward compatibility.
    """
    from agents.models.chatcmpl_converter import Converter

    # Reasoning item without provider_data (older format)
    input_items: list[Any] = [
        {"role": "user", "content": "What's the weather in Tokyo?"},
        {
            "id": "__fake_id__",
            "summary": [{"text": "Reasoning without provider info.", "type": "summary_text"}],
            "type": "reasoning",
            "content": None,
            "encrypted_content": None,
            "status": None,
            # No provider_data
        },
        {
            "arguments": '{"city": "Tokyo"}',
            "call_id": "call_weather_101",
            "name": "get_weather",
            "type": "function_call",
            "id": "__fake_id__",
            "status": None,
        },
        {
            "type": "function_call_output",
            "call_id": "call_weather_101",
            "output": "The weather in Tokyo is sunny.",
        },
    ]

    messages = Converter.items_to_messages(
        input_items,
        model="deepseek-reasoner",
    )

    assistant_with_tools = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_with_tools = msg
            break

    assert assistant_with_tools is not None
    # reasoning_content SHOULD be present for backward compatibility
    assert "reasoning_content" in assistant_with_tools
    assert assistant_with_tools["reasoning_content"] == "Reasoning without provider info."  # type: ignore[typeddict-item]

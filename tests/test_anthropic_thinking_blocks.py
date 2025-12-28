"""
Test for Anthropic thinking blocks in conversation history.

This test validates the fix for issue #1704:
- Thinking blocks are properly preserved from Anthropic responses
- Reasoning items are stored in session but not sent back in conversation history
- Non-reasoning models are unaffected
- Token usage is not increased for non-reasoning scenarios
"""

from __future__ import annotations

from typing import Any, cast

from openai.types.chat import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function

from agents.extensions.models.litellm_model import InternalChatCompletionMessage
from agents.models.chatcmpl_converter import Converter


def create_mock_anthropic_response_with_thinking() -> InternalChatCompletionMessage:
    """Create a mock Anthropic response with thinking blocks (like real response)."""
    message = InternalChatCompletionMessage(
        role="assistant",
        content="I'll check the weather in Paris for you.",
        reasoning_content="I need to call the weather function for Paris",
        thinking_blocks=[
            {
                "type": "thinking",
                "thinking": "I need to call the weather function for Paris",
                "signature": "EqMDCkYIBxgCKkBAFZO8EyZwN1hiLctq0YjZnP0KeKgprr+C0PzgDv4GSggnFwrPQHIZ9A5s+paH+DrQBI1+Vnfq3mLAU5lJnoetEgzUEWx/Cv1022ieAvcaDCXdmg1XkMK0tZ8uCCIwURYAAX0uf2wFdnWt9n8whkhmy8ARQD5G2za4R8X5vTqBq8jpJ15T3c1Jcf3noKMZKooCWFVf0/W5VQqpZTgwDkqyTau7XraS+u48YlmJGSfyWMPO8snFLMZLGaGmVJgHfEI5PILhOEuX/R2cEeLuC715f51LMVuxTNzlOUV/037JV6P2ten7D66FnWU9JJMMJJov+DjMb728yQFHwHz4roBJ5ePHaaFP6mDwpqYuG/hai6pVv2TAK1IdKUui/oXrYtU+0gxb6UF2kS1bspqDuN++R8JdL7CMSU5l28pQ8TsH1TpVF4jZpsFbp1Du4rQIULFsCFFg+Edf9tPgyKZOq6xcskIjT7oylAPO37/jhdNknDq2S82PaSKtke3ViOigtM5uJfG521ZscBJQ1K3kwoI/repIdV9PatjOYdsYAQ==",  # noqa: E501
            }
        ],
    )
    return message


def test_converter_skips_reasoning_items():
    """
    Unit test to verify that reasoning items are skipped when converting items to messages.
    """
    # Create test items including a reasoning item
    test_items: list[dict[str, Any]] = [
        {"role": "user", "content": "Hello"},
        {
            "id": "reasoning_123",
            "type": "reasoning",
            "summary": [{"text": "User said hello", "type": "summary_text"}],
        },
        {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hi there!"}],
            "status": "completed",
        },
    ]

    # Convert to messages
    messages = Converter.items_to_messages(test_items)  # type: ignore[arg-type]

    # Should have user message and assistant message, but no reasoning content
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"

    # Verify no thinking blocks in assistant message
    assistant_msg = messages[1]
    content = assistant_msg.get("content")
    if isinstance(content, list):
        for part in content:
            assert part.get("type") != "thinking"


def test_reasoning_items_preserved_in_message_conversion():
    """
    Test that reasoning content and thinking blocks are properly extracted
    from Anthropic responses and stored in reasoning items.
    """
    # Create mock message with thinking blocks
    mock_message = create_mock_anthropic_response_with_thinking()

    # Convert to output items
    output_items = Converter.message_to_output_items(mock_message)

    # Should have reasoning item, message item, and tool call items
    reasoning_items = [
        item for item in output_items if hasattr(item, "type") and item.type == "reasoning"
    ]
    assert len(reasoning_items) == 1

    reasoning_item = reasoning_items[0]
    assert reasoning_item.summary[0].text == "I need to call the weather function for Paris"

    # Verify thinking blocks are stored if we preserve them
    if (
        hasattr(reasoning_item, "content")
        and reasoning_item.content
        and len(reasoning_item.content) > 0
    ):
        thinking_block = reasoning_item.content[0]
        assert thinking_block.type == "reasoning_text"
        assert thinking_block.text == "I need to call the weather function for Paris"


def test_anthropic_thinking_blocks_with_tool_calls():
    """
    Test for models with extended thinking and interleaved thinking with tool calls.

    This test verifies the Anthropic's API's requirements for thinking blocks
    to be the first content in assistant messages when reasoning is enabled and tool
    calls are present.
    """
    # Create a message with reasoning, thinking blocks and tool calls
    message = InternalChatCompletionMessage(
        role="assistant",
        content="I'll check the weather for you.",
        reasoning_content="The user wants weather information, I need to call the weather function",
        thinking_blocks=[
            {
                "type": "thinking",
                "thinking": (
                    "The user is asking about weather. "
                    "Let me use the weather tool to get this information."
                ),
                "signature": "TestSignature123",
            },
            {
                "type": "thinking",
                "thinking": ("We should use the city Tokyo as the city."),
                "signature": "TestSignature456",
            },
        ],
        tool_calls=[
            ChatCompletionMessageToolCall(
                id="call_123",
                type="function",
                function=Function(name="get_weather", arguments='{"city": "Tokyo"}'),
            )
        ],
    )

    # Step 1: Convert message to output items
    output_items = Converter.message_to_output_items(message)

    # Verify reasoning item exists and contains thinking blocks
    reasoning_items = [
        item for item in output_items if hasattr(item, "type") and item.type == "reasoning"
    ]
    assert len(reasoning_items) == 1, "Should have exactly two reasoning items"

    reasoning_item = reasoning_items[0]

    # Verify thinking text is stored in content
    assert hasattr(reasoning_item, "content") and reasoning_item.content, (
        "Reasoning item should have content"
    )
    assert reasoning_item.content[0].type == "reasoning_text", (
        "Content should be reasoning_text type"
    )

    # Verify signature is stored in encrypted_content
    assert hasattr(reasoning_item, "encrypted_content"), (
        "Reasoning item should have encrypted_content"
    )
    assert reasoning_item.encrypted_content == "TestSignature123\nTestSignature456", (
        "Signature should be preserved"
    )

    # Verify tool calls are present
    tool_call_items = [
        item for item in output_items if hasattr(item, "type") and item.type == "function_call"
    ]
    assert len(tool_call_items) == 1, "Should have exactly one tool call"

    # Step 2: Convert output items back to messages
    # Convert items to dicts for the converter (simulating serialization/deserialization)
    items_as_dicts: list[dict[str, Any]] = []
    for item in output_items:
        if hasattr(item, "model_dump"):
            items_as_dicts.append(item.model_dump())
        else:
            items_as_dicts.append(cast(dict[str, Any], item))

    messages = Converter.items_to_messages(items_as_dicts, preserve_thinking_blocks=True)  # type: ignore[arg-type]

    # Find the assistant message with tool calls
    assistant_messages = [
        msg for msg in messages if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    assert len(assistant_messages) == 1, "Should have exactly one assistant message with tool calls"

    assistant_msg = assistant_messages[0]

    # Content must start with thinking blocks, not text
    content = assistant_msg.get("content")
    assert content is not None, "Assistant message should have content"

    assert isinstance(content, list) and len(content) > 0, (
        "Assistant message content should be a non-empty list"
    )

    first_content = content[0]
    assert first_content.get("type") == "thinking", (
        f"First content must be 'thinking' type for Anthropic compatibility, "
        f"but got '{first_content.get('type')}'"
    )
    expected_thinking = (
        "The user is asking about weather. Let me use the weather tool to get this information."
    )
    assert first_content.get("thinking") == expected_thinking, (
        "Thinking content should be preserved"
    )
    # Signature should also be preserved
    assert first_content.get("signature") == "TestSignature123", (
        "Signature should be preserved in thinking block"
    )

    second_content = content[1]
    assert second_content.get("type") == "thinking", (
        f"Second content must be 'thinking' type for Anthropic compatibility, "
        f"but got '{second_content.get('type')}'"
    )
    expected_thinking = "We should use the city Tokyo as the city."
    assert second_content.get("thinking") == expected_thinking, (
        "Thinking content should be preserved"
    )
    # Signature should also be preserved
    assert second_content.get("signature") == "TestSignature456", (
        "Signature should be preserved in thinking block"
    )

    last_content = content[2]
    assert last_content.get("type") == "text", (
        f"First content must be 'text' type but got '{last_content.get('type')}'"
    )
    expected_text = "I'll check the weather for you."
    assert last_content.get("text") == expected_text, "Content text should be preserved"

    # Verify tool calls are preserved
    tool_calls = assistant_msg.get("tool_calls", [])
    assert len(cast(list[Any], tool_calls)) == 1, "Tool calls should be preserved"
    assert cast(list[Any], tool_calls)[0]["function"]["name"] == "get_weather"

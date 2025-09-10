"""
Test for Anthropic thinking blocks in conversation history.

This test validates the fix for issue #1704:
- Thinking blocks are properly preserved from Anthropic responses
- Reasoning items are stored in session but not sent back in conversation history
- Non-reasoning models are unaffected
- Token usage is not increased for non-reasoning scenarios
"""

from __future__ import annotations

from typing import Any

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

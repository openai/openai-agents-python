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

import httpx
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.types.utils import ModelResponse as LiteLLMModelResponse
from openai.types.chat import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function

from agents.extensions.models.litellm_model import (
    InternalChatCompletionMessage,
    LitellmConverter,
)
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


def _assistant_thinking_blocks(message: Any) -> list[dict[str, Any]]:
    thinking_blocks = message.get("thinking_blocks")
    assert isinstance(thinking_blocks, list)
    assert all(isinstance(block, dict) for block in thinking_blocks)
    return cast(list[dict[str, Any]], thinking_blocks)


def _litellm_anthropic_message(
    thinking_blocks: list[dict[str, Any]],
    *,
    tool_call: bool = False,
) -> Any:
    response_tail = (
        {
            "type": "tool_use",
            "id": "toolu-weather",
            "name": "get_weather",
            "input": {"city": "Tokyo"},
        }
        if tool_call
        else {"type": "text", "text": "answer"}
    )
    completion_response = {
        "id": "msg-thinking",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [*thinking_blocks, response_tail],
        "stop_reason": "tool_use" if tool_call else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    raw_response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    response = AnthropicConfig().transform_parsed_response(
        completion_response=completion_response,
        raw_response=raw_response,
        model_response=LiteLLMModelResponse(model="claude-sonnet-4-5"),
    )
    return response.choices[0].message


def _round_trip_litellm_anthropic_blocks(
    thinking_blocks: list[dict[str, Any]],
    *,
    tool_call: bool = False,
) -> tuple[Any, list[dict[str, Any]]]:
    litellm_message = _litellm_anthropic_message(thinking_blocks, tool_call=tool_call)
    internal_message = LitellmConverter.convert_message_to_openai(
        litellm_message,
        model="anthropic/claude-sonnet-4-5",
    )
    output_items = Converter.message_to_output_items(
        internal_message,
        provider_data={"model": "anthropic/claude-sonnet-4-5"},
    )
    serialized_items: list[Any] = [item.model_dump() for item in output_items]
    messages = Converter.items_to_messages(
        serialized_items,
        model="anthropic/claude-sonnet-4-5",
        preserve_thinking_blocks=True,
    )
    outbound_request = AnthropicConfig().transform_request(
        model="claude-sonnet-4-5",
        messages=cast(Any, [*messages, {"role": "user", "content": "continue"}]),
        optional_params={
            "max_tokens": 1024,
            **(
                {
                    "tools": [
                        {
                            "name": "get_weather",
                            "description": "Get the weather.",
                            "input_schema": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        }
                    ]
                }
                if tool_call
                else {}
            ),
        },
        litellm_params={},
        headers={},
    )
    return output_items, cast(list[dict[str, Any]], outbound_request["messages"])


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
    assert reasoning_item.model_dump()["provider_data"]["thinking_blocks"] == (
        mock_message.thinking_blocks
    )

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

    messages = Converter.items_to_messages(
        items_as_dicts,  # type: ignore[arg-type]
        model="anthropic/claude-4-opus",
        preserve_thinking_blocks=True,
    )

    # Find the assistant message with tool calls
    assistant_messages = [
        msg for msg in messages if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    assert len(assistant_messages) == 1, "Should have exactly one assistant message with tool calls"

    assistant_msg = assistant_messages[0]

    # Thinking blocks must remain ahead of text in Anthropic's native block sequence.
    thinking_blocks = _assistant_thinking_blocks(assistant_msg)
    assert thinking_blocks, "Assistant message should have thinking blocks"

    first_content = thinking_blocks[0]
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
    assert first_content.get("signature") == "TestSignature123", (
        "Signature should be preserved in thinking block"
    )

    second_content = thinking_blocks[1]
    assert second_content.get("type") == "thinking", (
        f"Second content must be 'thinking' type for Anthropic compatibility, "
        f"but got '{second_content.get('type')}'"
    )
    expected_thinking = "We should use the city Tokyo as the city."
    assert second_content.get("thinking") == expected_thinking, (
        "Thinking content should be preserved"
    )
    assert second_content.get("signature") == "TestSignature456", (
        "Signature should be preserved in thinking block"
    )

    expected_text = "I'll check the weather for you."
    assert assistant_msg.get("content") == expected_text, "Content text should be preserved"

    # Verify tool calls are preserved
    tool_calls = assistant_msg.get("tool_calls", [])
    assert len(cast(list[Any], tool_calls)) == 1, "Tool calls should be preserved"
    assert cast(list[Any], tool_calls)[0]["function"]["name"] == "get_weather"


def test_items_to_messages_preserves_positional_bool_arguments():
    """
    Preserve positional compatibility for the released items_to_messages signature.
    """
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
            }
        ],
        tool_calls=[
            ChatCompletionMessageToolCall(
                id="call_123",
                type="function",
                function=Function(name="get_weather", arguments='{"city": "Tokyo"}'),
            )
        ],
    )

    output_items = Converter.message_to_output_items(message)
    items_as_dicts: list[dict[str, Any]] = []
    for item in output_items:
        if hasattr(item, "model_dump"):
            items_as_dicts.append(item.model_dump())
        else:
            items_as_dicts.append(cast(dict[str, Any], item))

    messages = Converter.items_to_messages(
        items_as_dicts,  # type: ignore[arg-type]
        "anthropic/claude-4-opus",
        True,
        True,
    )

    assistant_messages = [
        msg for msg in messages if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    assert len(assistant_messages) == 1, "Should have exactly one assistant message with tool calls"

    assistant_msg = assistant_messages[0]
    thinking_blocks = _assistant_thinking_blocks(assistant_msg)
    assert thinking_blocks[0].get("type") == "thinking", (
        "The third positional argument must continue to map to preserve_thinking_blocks"
    )


def test_anthropic_thinking_blocks_without_tool_calls():
    """
    Test for models with extended thinking WITHOUT tool calls.

    This test verifies that thinking blocks are properly attached to assistant
    messages even when there are no tool calls (fixes issue #2195).
    """
    # Create a message with reasoning and thinking blocks but NO tool calls
    message = InternalChatCompletionMessage(
        role="assistant",
        content="The weather in Paris is sunny with a temperature of 22°C.",
        reasoning_content="The user wants to know about the weather in Paris.",
        thinking_blocks=[
            {
                "type": "thinking",
                "thinking": "Let me think about the weather in Paris.",
                "signature": "TestSignatureNoTools123",
            }
        ],
        tool_calls=None,  # No tool calls
    )

    # Step 1: Convert message to output items
    output_items = Converter.message_to_output_items(message)

    # Verify reasoning item exists and contains thinking blocks
    reasoning_items = [
        item for item in output_items if hasattr(item, "type") and item.type == "reasoning"
    ]
    assert len(reasoning_items) == 1, "Should have exactly one reasoning item"

    reasoning_item = reasoning_items[0]

    # Verify thinking text is stored in content
    assert hasattr(reasoning_item, "content") and reasoning_item.content, (
        "Reasoning item should have content"
    )
    assert reasoning_item.content[0].type == "reasoning_text", (
        "Content should be reasoning_text type"
    )
    assert reasoning_item.content[0].text == "Let me think about the weather in Paris.", (
        "Thinking text should be preserved"
    )

    # Verify signature is stored in encrypted_content
    assert hasattr(reasoning_item, "encrypted_content"), (
        "Reasoning item should have encrypted_content"
    )
    assert reasoning_item.encrypted_content == "TestSignatureNoTools123", (
        "Signature should be preserved"
    )

    # Verify message item exists
    message_items = [
        item for item in output_items if hasattr(item, "type") and item.type == "message"
    ]
    assert len(message_items) == 1, "Should have exactly one message item"

    # Step 2: Convert output items back to messages with preserve_thinking_blocks=True
    items_as_dicts: list[dict[str, Any]] = []
    for item in output_items:
        if hasattr(item, "model_dump"):
            items_as_dicts.append(item.model_dump())
        else:
            items_as_dicts.append(cast(dict[str, Any], item))

    messages = Converter.items_to_messages(
        items_as_dicts,  # type: ignore[arg-type]
        model="anthropic/claude-4-opus",
        preserve_thinking_blocks=True,
    )

    # Should have one assistant message
    assistant_messages = [msg for msg in messages if msg.get("role") == "assistant"]
    assert len(assistant_messages) == 1, "Should have exactly one assistant message"

    assistant_msg = assistant_messages[0]

    # Thinking blocks stay in LiteLLM's native field even without tool calls.
    thinking_blocks = _assistant_thinking_blocks(assistant_msg)
    assert len(thinking_blocks) == 1, (
        f"Assistant message should have exactly one thinking block, got {len(thinking_blocks)}"
    )

    first_content = thinking_blocks[0]
    assert first_content.get("type") == "thinking", (
        f"First content must be 'thinking' type for Anthropic compatibility, "
        f"but got '{first_content.get('type')}'"
    )
    assert first_content.get("thinking") == "Let me think about the weather in Paris.", (
        "Thinking content should be preserved"
    )
    assert first_content.get("signature") == "TestSignatureNoTools123", (
        "Signature should be preserved in thinking block"
    )

    assert assistant_msg.get("content") == (
        "The weather in Paris is sunny with a temperature of 22°C."
    ), "Text content should be preserved"


def test_litellm_round_trip_preserves_omitted_thinking_block() -> None:
    thinking_blocks = [{"type": "thinking", "thinking": "", "signature": "OmittedSignature"}]

    output_items, outbound_messages = _round_trip_litellm_anthropic_blocks(thinking_blocks)

    reasoning_items = [item for item in output_items if item.type == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0].summary == []
    assert reasoning_items[0].model_dump()["provider_data"]["thinking_blocks"] == thinking_blocks
    assert outbound_messages[0]["content"][:1] == thinking_blocks


def test_litellm_round_trip_preserves_complete_thinking_block_sequence() -> None:
    thinking_blocks = [
        {"type": "thinking", "thinking": "", "signature": "OmittedSignature"},
        {"type": "redacted_thinking", "data": "EncryptedRedactedThinking"},
        {"type": "thinking", "thinking": "visible", "signature": "VisibleSignature"},
    ]

    _, outbound_messages = _round_trip_litellm_anthropic_blocks(
        thinking_blocks,
        tool_call=True,
    )

    assert outbound_messages[0]["content"][:3] == thinking_blocks
    assert outbound_messages[0]["content"][3] == {
        "type": "tool_use",
        "id": "toolu-weather",
        "name": "get_weather",
        "input": {"city": "Tokyo"},
    }


def test_complete_thinking_blocks_respect_replay_guards() -> None:
    message = InternalChatCompletionMessage(
        role="assistant",
        content="answer",
        reasoning_content="visible",
        thinking_blocks=[{"type": "thinking", "thinking": "visible", "signature": "Signature"}],
    )
    items: list[Any] = [
        item.model_dump()
        for item in Converter.message_to_output_items(
            message,
            provider_data={"model": "anthropic/claude-sonnet-4-5"},
        )
    ]

    disabled_messages = Converter.items_to_messages(
        items,
        model="anthropic/claude-sonnet-4-5",
        preserve_thinking_blocks=False,
    )
    mismatched_messages = Converter.items_to_messages(
        items,
        model="anthropic/claude-opus-4-5",
        preserve_thinking_blocks=True,
    )
    unknown_origin_items: list[Any] = [
        item.model_dump()
        for item in Converter.message_to_output_items(
            message,
            provider_data={"response_id": "response-from-unknown-provider"},
        )
    ]
    unknown_origin_messages = Converter.items_to_messages(
        unknown_origin_items,
        model="anthropic/claude-sonnet-4-5",
        preserve_thinking_blocks=True,
    )

    assert all("thinking_blocks" not in message for message in disabled_messages)
    assert all("thinking_blocks" not in message for message in mismatched_messages)
    assert all("thinking_blocks" not in message for message in unknown_origin_messages)


def test_originless_thinking_blocks_preserve_legacy_deepseek_reasoning_replay() -> None:
    message = InternalChatCompletionMessage(
        role="assistant",
        content="answer",
        reasoning_content="legacy reasoning",
        thinking_blocks=[{"type": "thinking", "thinking": "visible", "signature": "Signature"}],
    )
    items: list[Any] = [item.model_dump() for item in Converter.message_to_output_items(message)]

    reasoning_item = next(item for item in items if item["type"] == "reasoning")
    assert reasoning_item["provider_data"] == {
        "thinking_blocks": [{"type": "thinking", "thinking": "visible", "signature": "Signature"}]
    }

    messages = Converter.items_to_messages(items, model="deepseek/deepseek-reasoner")

    assistant_message = next(message for message in messages if message["role"] == "assistant")
    assert assistant_message["reasoning_content"] == "legacy reasoning"  # type: ignore[typeddict-item]
    assert "thinking_blocks" not in assistant_message


def test_legacy_reasoning_item_reconstructs_inline_thinking_blocks() -> None:
    for provider_data in (None, {"thinking_blocks": ["invalid-block"]}):
        reasoning_item: dict[str, Any] = {
            "id": "reasoning_legacy",
            "type": "reasoning",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "legacy thinking"}],
            "encrypted_content": "LegacySignature",
        }
        if provider_data is not None:
            reasoning_item["provider_data"] = provider_data

        history: list[dict[str, Any]] = [
            reasoning_item,
            {
                "id": "message_legacy",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            },
        ]

        messages = Converter.items_to_messages(
            history,  # type: ignore[arg-type]
            model="anthropic/claude-sonnet-4-5",
            preserve_thinking_blocks=True,
        )

        assert messages[0]["content"] == [
            {
                "type": "thinking",
                "thinking": "legacy thinking",
                "signature": "LegacySignature",
            },
            {"type": "text", "text": "answer"},
        ]
        assert "thinking_blocks" not in messages[0]


def test_thinking_blocks_do_not_leak_across_an_intervening_user_turn():
    """A reasoning item not followed by its own assistant message must not leak.

    When a turn produces extended thinking but no visible output, the only stored
    output item is the reasoning item, so the next item in the history is the user's
    next message. The signed thinking blocks belong to that earlier turn and must not
    be prepended to a later assistant message.
    """
    silent_turn = InternalChatCompletionMessage(
        role="assistant",
        content="",
        reasoning_content="Nothing to say yet.",
        thinking_blocks=[
            {
                "type": "thinking",
                "thinking": "Earlier private thinking.",
                "signature": "EarlierTurnSignature",
            }
        ],
        tool_calls=None,
    )
    output_items = Converter.message_to_output_items(silent_turn)
    assert [item.type for item in output_items] == ["reasoning"]

    history: list[dict[str, Any]] = [{"role": "user", "content": "first question"}]
    history += [item.model_dump() for item in output_items]
    history += [
        {"role": "user", "content": "second question"},
        {
            "id": "msg_2",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "an answer", "annotations": []}],
        },
    ]

    messages = Converter.items_to_messages(
        history,  # type: ignore[arg-type]
        model="anthropic/claude-4-opus",
        preserve_thinking_blocks=True,
    )

    assistant_messages = [msg for msg in messages if msg.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].get("content") == "an answer"

"""
Test for Gemini thought signatures in function calling.

Validates that thought signatures are preserved through the bidirectional roundtrip:
- Gemini chatcmpl message → response item → back to message
"""

from __future__ import annotations

from typing import Any

from openai.types.chat.chat_completion_message_tool_call import Function

from agents.extensions.models.litellm_model import InternalChatCompletionMessage, InternalToolCall
from agents.models.chatcmpl_converter import Converter


def test_gemini_thought_signature_roundtrip():
    """Test that thought signatures are preserved from Gemini responses to messages."""

    # Create mock Gemini response with thought signature in new extra_content structure
    class MockToolCall(InternalToolCall):
        def __init__(self):
            super().__init__(
                id="call_123",
                type="function",
                function=Function(name="get_weather", arguments='{"city": "Paris"}'),
                extra_content={"google": {"thought_signature": "test_signature_abc"}},
            )

    message = InternalChatCompletionMessage(
        role="assistant",
        content="I'll check the weather.",
        reasoning_content="",
        tool_calls=[MockToolCall()],
    )

    # Step 1: Convert to items
    provider_data = {"model": "gemini/gemini-3-pro", "response_id": "gemini-response-id-123"}

    items = Converter.message_to_output_items(message, provider_data=provider_data)

    func_calls = [item for item in items if hasattr(item, "type") and item.type == "function_call"]
    assert len(func_calls) == 1

    # Verify thought_signature is stored in items with our provider_data structure
    func_call_dict = func_calls[0].model_dump()

    assert func_call_dict["provider_data"]["model"] == "gemini/gemini-3-pro"
    assert func_call_dict["provider_data"]["response_id"] == "gemini-response-id-123"
    assert func_call_dict["provider_data"]["thought_signature"] == "test_signature_abc"

    # Step 2: Convert back to messages
    items_as_dicts = [item.model_dump() for item in items]
    messages = Converter.items_to_messages(
        [{"role": "user", "content": "test"}] + items_as_dicts,
        model="gemini/gemini-3-pro",
    )

    # Verify thought_signature is restored in extra_content format
    assistant_msg = [msg for msg in messages if msg.get("role") == "assistant"][0]
    tool_call = assistant_msg["tool_calls"][0]  # type: ignore[index, typeddict-item]
    assert tool_call["extra_content"]["google"]["thought_signature"] == "test_signature_abc"


def test_gemini_multiple_tool_calls_with_thought_signatures():
    """Test multiple tool calls each preserve their own thought signatures."""
    tool_call_1 = InternalToolCall(
        id="call_1",
        type="function",
        function=Function(name="func_a", arguments='{"x": 1}'),
        extra_content={"google": {"thought_signature": "sig_aaa"}},
    )
    tool_call_2 = InternalToolCall(
        id="call_2",
        type="function",
        function=Function(name="func_b", arguments='{"y": 2}'),
        extra_content={"google": {"thought_signature": "sig_bbb"}},
    )

    message = InternalChatCompletionMessage(
        role="assistant",
        content="Calling two functions.",
        reasoning_content="",
        tool_calls=[tool_call_1, tool_call_2],
    )

    provider_data = {"model": "gemini/gemini-3-pro"}
    items = Converter.message_to_output_items(message, provider_data=provider_data)

    func_calls = [i for i in items if hasattr(i, "type") and i.type == "function_call"]
    assert len(func_calls) == 2

    assert func_calls[0].model_dump()["provider_data"]["thought_signature"] == "sig_aaa"
    assert func_calls[1].model_dump()["provider_data"]["thought_signature"] == "sig_bbb"


def test_gemini_thought_signature_items_to_messages():
    """Test that items_to_messages restores extra_content from provider_data for Gemini."""

    # Create a function call item with provider_data containing thought_signature
    func_call_item = {
        "id": "fake-id",
        "call_id": "call_restore",
        "name": "restore_func",
        "arguments": '{"test": true}',
        "type": "function_call",
        "provider_data": {
            "model": "gemini/gemini-3-pro",
            "response_id": "gemini-response-id-123",
            "thought_signature": "restored_sig_xyz",
        },
    }

    items = [{"role": "user", "content": "test"}, func_call_item]
    messages = Converter.items_to_messages(items, model="gemini/gemini-3-pro")  # type: ignore[arg-type]

    # Find the assistant message with tool_calls
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1

    tool_calls: list[dict[str, Any]] = assistant_msgs[0].get("tool_calls", [])  # type: ignore[assignment]
    assert len(tool_calls) == 1

    # Verify extra_content is restored in Google format
    assert tool_calls[0]["extra_content"]["google"]["thought_signature"] == "restored_sig_xyz"

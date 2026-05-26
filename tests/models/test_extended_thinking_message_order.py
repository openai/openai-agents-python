"""Tests for the extended thinking message order bug fix in LitellmModel."""

from __future__ import annotations

from typing import Any, cast

from openai.types.chat import ChatCompletionMessageParam

from agents.extensions.models.litellm_model import LitellmModel


class TestExtendedThinkingMessageOrder:
    """Test the _fix_tool_message_ordering method."""

    def test_basic_reordering_tool_result_before_call(self):
        """Test that a tool result appearing before its tool call gets reordered correctly."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Hello"},
            {"role": "tool", "tool_call_id": "call_123", "content": "Result for call_123"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "test", "arguments": "{}"},
                    }
                ],
            },
            {"role": "user", "content": "Thanks"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        # Should reorder to: user, assistant+tool_call, tool_result, user
        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["tool_calls"][0]["id"] == "call_123"  # type: ignore
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "call_123"
        assert result[3]["role"] == "user"

    def test_consecutive_tool_calls_get_separated(self):
        """Test that consecutive assistant messages with tool calls get properly paired with results."""  # noqa: E501
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "test1", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "test2", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Result 1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "Result 2"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        # Should pair each tool call with its result immediately
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["tool_calls"][0]["id"] == "call_1"  # type: ignore
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "call_1"
        assert result[3]["role"] == "assistant"
        assert result[3]["tool_calls"][0]["id"] == "call_2"  # type: ignore
        assert result[4]["role"] == "tool"
        assert result[4]["tool_call_id"] == "call_2"

    def test_unmatched_tool_results_preserved(self):
        """Test that tool results without matching tool calls are preserved."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "test", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Matched result"},
            {"role": "tool", "tool_call_id": "call_orphan", "content": "Orphaned result"},
            {"role": "user", "content": "End"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        # Should preserve the orphaned tool result
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "call_1"
        assert result[3]["role"] == "tool"  # Orphaned result preserved
        assert result[3]["tool_call_id"] == "call_orphan"
        assert result[4]["role"] == "user"

    def test_tool_calls_without_results_preserved(self):
        """Test that tool calls without results are still included."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "test", "arguments": "{}"},
                    }
                ],
            },
            {"role": "user", "content": "End"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        # Should preserve the tool call even without a result
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["tool_calls"][0]["id"] == "call_1"  # type: ignore
        assert result[2]["role"] == "user"

    def test_correctly_ordered_messages_unchanged(self):
        """Test that correctly ordered messages remain in the same order."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "test", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Result"},
            {"role": "assistant", "content": "Done"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        # Should remain exactly the same
        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["tool_calls"][0]["id"] == "call_1"  # type: ignore
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "call_1"
        assert result[3]["role"] == "assistant"

    def test_multiple_tool_calls_single_message(self):
        """Test assistant message with multiple tool calls gets split properly."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "test1", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "test2", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Result 1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "Result 2"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        # Should split the multi-tool message and pair each properly
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert len(result[1]["tool_calls"]) == 1  # type: ignore
        assert result[1]["tool_calls"][0]["id"] == "call_1"  # type: ignore
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "call_1"
        assert result[3]["role"] == "assistant"
        assert len(result[3]["tool_calls"]) == 1  # type: ignore
        assert result[3]["tool_calls"][0]["id"] == "call_2"  # type: ignore
        assert result[4]["role"] == "tool"
        assert result[4]["tool_call_id"] == "call_2"

    def test_split_does_not_duplicate_content_or_thinking(self):
        """Splitting multi-tool assistant messages must not duplicate text/thinking blocks.

        Anthropic's extended thinking API rejects requests that include the same signed
        thinking block more than once, and duplicated assistant text corrupts conversation
        history. Only the first split should retain content, thinking_blocks, and
        reasoning_content; subsequent splits should carry the tool_call alone.
        """
        # Build the assistant message via cast so mypy doesn't reject the
        # extra keys (`thinking_blocks`, `reasoning_content`) which are not
        # part of the upstream ChatCompletionAssistantMessageParam TypedDict
        # but are surfaced by litellm for Anthropic extended thinking.
        assistant_msg = cast(
            ChatCompletionMessageParam,
            {
                "role": "assistant",
                "content": "Looking up both queries.",
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "plan", "signature": "sig_abc"}
                ],
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
        )
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Search both"},
            assistant_msg,
            {"role": "tool", "tool_call_id": "call_1", "content": "ok1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "ok2"},
        ]

        model = LitellmModel("claude-3-5-sonnet")
        result = model._fix_tool_message_ordering(messages)

        assistants = [cast(dict[str, Any], m) for m in result if m.get("role") == "assistant"]
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

    def test_empty_messages_list(self):
        """Test that empty message list is handled correctly."""
        messages: list[ChatCompletionMessageParam] = []

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        assert result == []

    def test_no_tool_messages(self):
        """Test that messages without tool calls are left unchanged."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        assert result == messages

    def test_complex_mixed_scenario(self):
        """Test a complex scenario with various message types and orderings."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Start"},
            {
                "role": "tool",
                "tool_call_id": "call_out_of_order",
                "content": "Out of order result",
            },  # This comes before its call
            {"role": "assistant", "content": "Regular response"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_out_of_order",
                        "type": "function",
                        "function": {"name": "test", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_normal",
                        "type": "function",
                        "function": {"name": "test2", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_normal", "content": "Normal result"},
            {
                "role": "tool",
                "tool_call_id": "call_orphan",
                "content": "Orphaned result",
            },  # No matching call
            {"role": "user", "content": "End"},
        ]

        model = LitellmModel("test-model")
        result = model._fix_tool_message_ordering(messages)

        # Should reorder properly while preserving all messages
        assert len(result) == 8
        assert result[0]["role"] == "user"  # Start
        assert result[1]["role"] == "assistant"  # Regular response
        assert result[2]["role"] == "assistant"  # call_out_of_order
        assert result[2]["tool_calls"][0]["id"] == "call_out_of_order"  # type: ignore
        assert result[3]["role"] == "tool"  # Out of order result (now properly paired)
        assert result[3]["tool_call_id"] == "call_out_of_order"
        assert result[4]["role"] == "assistant"  # call_normal
        assert result[4]["tool_calls"][0]["id"] == "call_normal"  # type: ignore
        assert result[5]["role"] == "tool"  # Normal result
        assert result[5]["tool_call_id"] == "call_normal"
        assert result[6]["role"] == "tool"  # Orphaned result (preserved)
        assert result[6]["tool_call_id"] == "call_orphan"
        assert result[7]["role"] == "user"  # End

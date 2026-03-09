"""Tests for issue #2258: to_input_list() should exclude handoff-internal messages."""

import pytest

from agents.run_internal.items import (
    _is_handoff_internal_message,
    run_item_to_input_item,
)
from agents.items import MessageOutputItem, RunItem
from unittest.mock import MagicMock


class TestIsHandoffInternalMessage:
    """Tests for _is_handoff_internal_message function."""

    def test_returns_false_for_non_dict(self):
        """Non-dict items should return False."""
        assert _is_handoff_internal_message("string") is False
        assert _is_handoff_internal_message(123) is False
        assert _is_handoff_internal_message(None) is False

    def test_returns_false_for_user_message(self):
        """User messages should not be considered handoff-internal."""
        item = {"role": "user", "content": "Hello"}
        assert _is_handoff_internal_message(item) is False

    def test_returns_false_for_assistant_message_without_markers(self):
        """Assistant messages without conversation history markers should not be filtered."""
        item = {"role": "assistant", "content": "Hello, how can I help?"}
        assert _is_handoff_internal_message(item) is False

    def test_returns_true_for_handoff_summary_message(self):
        """Assistant messages with conversation history markers should be filtered."""
        item = {
            "role": "assistant",
            "content": "For context, here is the conversation so far between the user and the previous agent:\n<CONVERSATION HISTORY>\n1. user: Hello\n</CONVERSATION HISTORY>"
        }
        assert _is_handoff_internal_message(item) is True

    def test_returns_false_for_dict_without_role(self):
        """Dict items without a role should return False."""
        item = {"type": "function_call", "call_id": "123"}
        assert _is_handoff_internal_message(item) is False

    def test_returns_false_for_non_string_content(self):
        """Items with non-string content should return False."""
        item = {"role": "assistant", "content": {"key": "value"}}
        assert _is_handoff_internal_message(item) is False


class TestRunItemToInputItemFiltersHandoffMessages:
    """Tests that run_item_to_input_item filters handoff-internal messages."""

    def test_filters_handoff_internal_message(self):
        """Handoff-internal messages should be filtered out."""
        # Create a mock RunItem that returns a handoff-internal message
        mock_agent = MagicMock()
        handoff_content = (
            "For context, here is the conversation so far between the user and the previous agent:\n"
            "<CONVERSATION HISTORY>\n"
            "1. user: Hello\n"
            "2. assistant: Hi there\n"
            "</CONVERSATION HISTORY>"
        )
        raw_item = {"role": "assistant", "content": handoff_content}

        # Create a mock RunItem with the required attributes
        mock_run_item = MagicMock()
        mock_run_item.type = "message_output_item"
        mock_run_item.raw_item = raw_item
        mock_run_item.to_input_item.return_value = raw_item

        result = run_item_to_input_item(mock_run_item)
        assert result is None

    def test_preserves_regular_assistant_message(self):
        """Regular assistant messages should be preserved."""
        mock_agent = MagicMock()
        raw_item = {"role": "assistant", "content": "Hello, how can I help?"}

        mock_run_item = MagicMock()
        mock_run_item.type = "message_output_item"
        mock_run_item.raw_item = raw_item
        mock_run_item.to_input_item.return_value = raw_item

        result = run_item_to_input_item(mock_run_item)
        assert result == raw_item

    def test_preserves_user_message(self):
        """User messages should be preserved."""
        mock_agent = MagicMock()
        raw_item = {"role": "user", "content": "Hello"}

        mock_run_item = MagicMock()
        mock_run_item.type = "message_output_item"
        mock_run_item.raw_item = raw_item
        mock_run_item.to_input_item.return_value = raw_item

        result = run_item_to_input_item(mock_run_item)
        assert result == raw_item

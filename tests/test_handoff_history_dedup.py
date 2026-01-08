"""Tests for to_input_list with handoff history deduplication (Issue #2258)."""

from __future__ import annotations

from agents.result import RunResultBase


class TestNestedHistorySummaryDetection:
    """Tests for _is_nested_history_summary static method."""

    def test_detects_summary_with_markers(self) -> None:
        """Verify detection of nested history summary messages."""
        summary_item = {
            "role": "assistant",
            "content": "<CONVERSATION HISTORY>\ntest\n</CONVERSATION HISTORY>",
        }
        assert RunResultBase._is_nested_history_summary(summary_item) is True

    def test_ignores_regular_assistant_message(self) -> None:
        """Regular assistant messages should not be detected as summaries."""
        regular_item = {
            "role": "assistant",
            "content": "Hello, how can I help?",
        }
        assert RunResultBase._is_nested_history_summary(regular_item) is False

    def test_ignores_user_message(self) -> None:
        """User messages should not be detected as summaries."""
        user_item = {"role": "user", "content": "Test"}
        assert RunResultBase._is_nested_history_summary(user_item) is False

    def test_ignores_function_output(self) -> None:
        """Function outputs should not be detected as summaries."""
        function_item = {
            "type": "function_call_output",
            "call_id": "123",
            "output": "result",
        }
        assert RunResultBase._is_nested_history_summary(function_item) is False

    def test_requires_both_markers(self) -> None:
        """Both start and end markers are required for detection."""
        partial_start = {
            "role": "assistant",
            "content": "<CONVERSATION HISTORY> only start marker",
        }
        assert RunResultBase._is_nested_history_summary(partial_start) is False

        partial_end = {
            "role": "assistant",
            "content": "only end marker </CONVERSATION HISTORY>",
        }
        assert RunResultBase._is_nested_history_summary(partial_end) is False

    def test_handles_non_string_content(self) -> None:
        """Non-string content should return False."""
        structured_content = {
            "role": "assistant",
            "content": [{"type": "text", "text": "structured content"}],
        }
        assert RunResultBase._is_nested_history_summary(structured_content) is False

    def test_handles_non_dict_input(self) -> None:
        """Non-dict inputs should return False."""
        assert RunResultBase._is_nested_history_summary("not a dict") is False  # type: ignore
        assert RunResultBase._is_nested_history_summary(None) is False  # type: ignore
        assert RunResultBase._is_nested_history_summary(123) is False  # type: ignore

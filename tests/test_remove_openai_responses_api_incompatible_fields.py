from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.models.openai_responses import OpenAIResponsesModel


@pytest.fixture
def model() -> OpenAIResponsesModel:
    """Create a model instance for testing."""
    mock_client = MagicMock()
    return OpenAIResponsesModel(model="gpt-5", openai_client=mock_client)


class TestRemoveOpenAIResponsesAPIIncompatibleFields:
    """Tests for _remove_openai_responses_api_incompatible_fields method."""

    def test_returns_unchanged_when_no_provider_data(self, model: OpenAIResponsesModel):
        """When no items have provider_data, the input should be returned unchanged."""
        list_input = [
            {"type": "message", "content": "hello"},
            {"type": "function_call", "call_id": "call_123", "name": "test"},
        ]

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        assert result is list_input  # Same object reference.

    def test_removes_reasoning_items_with_provider_data(self, model: OpenAIResponsesModel):
        """Reasoning items with provider_data should be completely removed."""
        list_input = [
            {"type": "message", "content": "hello"},
            {"type": "reasoning", "provider_data": {"model": "gemini/gemini-3"}},
            {"type": "function_call", "call_id": "call_123"},
        ]

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        assert len(result) == 2
        assert result[0] == {"type": "message", "content": "hello"}
        assert result[1] == {"type": "function_call", "call_id": "call_123"}

    def test_keeps_reasoning_items_without_provider_data(self, model: OpenAIResponsesModel):
        """Reasoning items without provider_data should be kept."""
        list_input = [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": "hello", "provider_data": {"foo": "bar"}},
        ]

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        assert len(result) == 2
        assert result[0] == {"type": "reasoning", "summary": []}
        assert result[1] == {"type": "message", "content": "hello"}

    def test_removes_provider_data_from_all_items(self, model: OpenAIResponsesModel):
        """provider_data field should be removed from all dict items."""
        list_input = [
            {"type": "message", "content": "hello", "provider_data": {"model": "gemini/gemini-3"}},
            {
                "type": "function_call",
                "call_id": "call_123",
                "provider_data": {"model": "gemini/gemini-3"},
            },
        ]

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        assert len(result) == 2
        assert "provider_data" not in result[0]
        assert "provider_data" not in result[1]

    def test_removes_fake_responses_id(self, model: OpenAIResponsesModel):
        """Items with id equal to FAKE_RESPONSES_ID should have their id removed."""
        list_input = [
            {
                "type": "message",
                "id": FAKE_RESPONSES_ID,
                "content": "hello",
                "provider_data": {"model": "gemini/gemini-3"},
            },
        ]

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        assert len(result) == 1
        assert "id" not in result[0]
        assert result[0]["content"] == "hello"

    def test_preserves_real_ids(self, model: OpenAIResponsesModel):
        """Real IDs (not FAKE_RESPONSES_ID) should be preserved."""
        list_input = [
            {
                "type": "message",
                "id": "msg_real123",
                "content": "hello",
                "provider_data": {},
            },
        ]

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        assert result[0]["id"] == "msg_real123"

    def test_handles_empty_list(self, model: OpenAIResponsesModel):
        """Empty list should be returned unchanged."""
        list_input: list[dict[str, Any]] = []

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        assert result == []

    def test_combined_scenario(self, model: OpenAIResponsesModel):
        """Test a realistic scenario with multiple items needing different processing."""
        list_input = [
            {"type": "message", "content": "user input"},
            {"type": "reasoning", "summary": [], "provider_data": {"model": "gemini/gemini-3"}},
            {
                "type": "function_call",
                "call_id": "call_abc_123",
                "name": "get_weather",
                "provider_data": {"model": "gemini/gemini-3"},
            },
            {
                "type": "function_call_output",
                "call_id": "call_abc_123",
                "output": '{"temp": 72}',
            },
            {
                "type": "message",
                "id": FAKE_RESPONSES_ID,
                "content": "The weather is 72F",
                "provider_data": {"model": "gemini/gemini-3"},
            },
        ]

        result = model._remove_openai_responses_api_incompatible_fields(list_input)

        # Should have 4 items (reasoning with provider_data removed).
        assert len(result) == 4

        # First item unchanged (no provider_data).
        assert result[0] == {"type": "message", "content": "user input"}

        # Function call: __thought__ suffix removed, provider_data removed.
        assert result[1]["type"] == "function_call"
        assert result[1]["call_id"] == "call_abc_123"
        assert "provider_data" not in result[1]

        # Function call output: __thought__ suffix removed, provider_data removed.
        assert result[2]["type"] == "function_call_output"
        assert result[2]["call_id"] == "call_abc_123"

        # Last message: fake id removed, provider_data removed.
        assert result[3]["type"] == "message"
        assert result[3]["content"] == "The weather is 72F"
        assert "id" not in result[3]
        assert "provider_data" not in result[3]

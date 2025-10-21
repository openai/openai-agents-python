"""
Unit tests for LitellmModel._convert_tool_choice_for_response

Tests the static method that converts various tool_choice formats
to the format expected by the Response type.

Related to Issue #1846: Support tool_choice with specific tool names in LiteLLM streaming
"""

import pytest
from openai import NotGiven, omit
from openai.types.responses.tool_choice_function import ToolChoiceFunction

from agents.extensions.models.litellm_model import LitellmModel


class TestConvertToolChoiceForResponse:
    """Test the _convert_tool_choice_for_response static method."""

    def test_convert_omit_returns_auto(self):
        """Test that omit is converted to 'auto'"""
        result = LitellmModel._convert_tool_choice_for_response(omit)
        assert result == "auto"

    def test_convert_not_given_returns_auto(self):
        """Test that NotGiven is converted to 'auto'"""
        result = LitellmModel._convert_tool_choice_for_response(NotGiven())
        assert result == "auto"

    def test_convert_literal_auto(self):
        """Test that literal 'auto' is preserved"""
        result = LitellmModel._convert_tool_choice_for_response("auto")
        assert result == "auto"

    def test_convert_literal_required(self):
        """Test that literal 'required' is preserved"""
        result = LitellmModel._convert_tool_choice_for_response("required")
        assert result == "required"

    def test_convert_literal_none(self):
        """Test that literal 'none' is preserved"""
        result = LitellmModel._convert_tool_choice_for_response("none")
        assert result == "none"

    def test_convert_tool_choice_function_preserved(self):
        """Test that ToolChoiceFunction is preserved as-is"""
        tool_choice = ToolChoiceFunction(type="function", name="my_tool")
        result = LitellmModel._convert_tool_choice_for_response(tool_choice)
        assert result == tool_choice
        assert isinstance(result, ToolChoiceFunction)
        assert result.name == "my_tool"

    def test_convert_dict_from_chatcompletions_converter(self):
        """
        Test conversion from ChatCompletions Converter dict format.
        Format: {"type": "function", "function": {"name": "tool_name"}}
        """
        tool_choice_dict = {
            "type": "function",
            "function": {"name": "my_custom_tool"},
        }
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_dict)
        assert isinstance(result, ToolChoiceFunction)
        assert result.type == "function"
        assert result.name == "my_custom_tool"

    def test_convert_dict_missing_function_name_returns_auto(self):
        """Test that dict without function name falls back to 'auto'"""
        tool_choice_dict = {
            "type": "function",
            "function": {},  # Missing 'name'
        }
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_dict)
        assert result == "auto"

    def test_convert_dict_empty_function_name_returns_auto(self):
        """Test that dict with empty function name falls back to 'auto'"""
        tool_choice_dict = {
            "type": "function",
            "function": {"name": ""},  # Empty name
        }
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_dict)
        assert result == "auto"

    def test_convert_dict_missing_function_key_returns_auto(self):
        """Test that dict without 'function' key falls back to 'auto'"""
        tool_choice_dict = {"type": "function"}  # Missing 'function' key
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_dict)
        assert result == "auto"

    def test_convert_dict_wrong_type_returns_auto(self):
        """Test that dict with wrong type falls back to 'auto'"""
        tool_choice_dict = {
            "type": "wrong_type",
            "function": {"name": "my_tool"},
        }
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_dict)
        assert result == "auto"

    def test_convert_dict_function_not_dict_returns_auto(self):
        """Test that dict with non-dict function value falls back to 'auto'"""
        tool_choice_dict = {
            "type": "function",
            "function": "not_a_dict",
        }
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_dict)
        assert result == "auto"

    def test_convert_unexpected_type_returns_auto(self):
        """Test that unexpected types fall back to 'auto'"""
        result = LitellmModel._convert_tool_choice_for_response(123)
        assert result == "auto"

        result = LitellmModel._convert_tool_choice_for_response([])
        assert result == "auto"

        result = LitellmModel._convert_tool_choice_for_response(None)
        assert result == "auto"


class TestToolChoiceConversionEdgeCases:
    """Test edge cases and real-world scenarios."""

    def test_real_world_scenario_chatcompletions_format(self):
        """
        Test a real-world scenario from ChatCompletions Converter.
        This is the actual format returned when tool_choice specifies a tool name.
        """
        # This is what ChatCompletions Converter returns
        tool_choice_from_converter = {
            "type": "function",
            "function": {"name": "get_weather"},
        }
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_from_converter)
        assert isinstance(result, ToolChoiceFunction)
        assert result.name == "get_weather"
        assert result.type == "function"

    def test_none_string_vs_none_literal(self):
        """Test that string 'none' works but None (NoneType) defaults to auto"""
        # String "none" should be preserved
        result = LitellmModel._convert_tool_choice_for_response("none")
        assert result == "none"

        # NoneType should fallback to auto
        result = LitellmModel._convert_tool_choice_for_response(None)
        assert result == "auto"

    def test_complex_tool_name(self):
        """Test that complex tool names are handled correctly"""
        tool_choice_dict = {
            "type": "function",
            "function": {"name": "get_user_profile_with_special_chars_123"},
        }
        result = LitellmModel._convert_tool_choice_for_response(tool_choice_dict)
        assert isinstance(result, ToolChoiceFunction)
        assert result.name == "get_user_profile_with_special_chars_123"

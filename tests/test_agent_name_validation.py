"""Tests for enhanced agent name validation."""

import pytest

from agents import Agent
from agents.util._transforms import validate_agent_name


class TestAgentNameValidation:
    """Test suite for enhanced agent name validation."""

    def test_valid_names_pass(self):
        """Test that valid agent names are accepted."""
        valid_names = [
            "Assistant",
            "Customer Service Agent",
            "data_analyst",
            "Research-Bot",
            "Agent_1",
            "Multi Word Agent",
            "Simple123",
            "a",  # Single character
            "Agent-Helper-Bot",
            "user_support_agent",
        ]

        for name in valid_names:
            # Should not raise any exception
            validate_agent_name(name)
            # Should be able to create agent successfully
            Agent(name=name)

    def test_empty_name_fails(self):
        """Test that empty names are rejected."""
        with pytest.raises(ValueError, match="Agent name cannot be empty"):
            validate_agent_name("")

        with pytest.raises(ValueError, match="Invalid agent name: Agent name cannot be empty"):
            Agent(name="")

    def test_whitespace_only_name_fails(self):
        """Test that whitespace-only names are rejected."""
        whitespace_names = ["   ", "\t", "\n", " \t \n "]

        for name in whitespace_names:
            with pytest.raises(ValueError, match="Agent name cannot be only whitespace"):
                validate_agent_name(name)

            with pytest.raises(
                ValueError, match="Invalid agent name: Agent name cannot be only whitespace"
            ):
                Agent(name=name)

    def test_leading_trailing_whitespace_fails(self):
        """Test that names with leading/trailing whitespace are rejected."""
        whitespace_names = [
            " Agent",
            "Agent ",
            " Agent ",
            "\tAgent",
            "Agent\n",
        ]

        for name in whitespace_names:
            with pytest.raises(ValueError, match="has leading/trailing whitespace"):
                validate_agent_name(name)

            with pytest.raises(
                ValueError, match="Invalid agent name.*has leading/trailing whitespace"
            ):
                Agent(name=name)

    def test_problematic_characters_fail(self):
        """Test that names with problematic characters are rejected."""
        problematic_names = [
            "Agent@Home",  # @ symbol
            "Agent#1",  # # symbol
            "Agent$",  # $ symbol
            "Agent%Bot",  # % symbol
            "Agent&Co",  # & symbol
            "Agent*Star",  # * symbol
            "Agent+Plus",  # + symbol
            "Agent=Equal",  # = symbol
            "Agent|Pipe",  # | symbol
            "Agent\\Back",  # \ symbol
            "Agent/Slash",  # / symbol
            "Agent<Less",  # < symbol
            "Agent>More",  # > symbol
            "Agent?Quest",  # ? symbol
            "Agent!Bang",  # ! symbol
            "Agent(Paren",  # ( symbol
            "Agent)Close",  # ) symbol
            "Agent[Brack",  # [ symbol
            "Agent]End",  # ] symbol
            "Agent{Brace",  # { symbol
            "Agent}Close",  # } symbol
            "Agent:Colon",  # : symbol
            "Agent;Semi",  # ; symbol
            "Agent'Quote",  # ' symbol
            'Agent"Doub',  # " symbol
            "Agent,Comma",  # , symbol
            "Agent.Dot",  # . symbol
        ]

        for name in problematic_names:
            with pytest.raises(ValueError, match="contains characters .* that may cause issues"):
                validate_agent_name(name)

            with pytest.raises(
                ValueError, match="Invalid agent name.*contains characters .* that may cause issues"
            ):
                Agent(name=name)

    def test_names_starting_with_numbers_fail(self):
        """Test that names starting with numbers are rejected."""
        number_names = [
            "1Agent",
            "2nd_Agent",
            "99problems",
            "0zero",
        ]

        for name in number_names:
            with pytest.raises(ValueError, match="starts with a number"):
                validate_agent_name(name)

            with pytest.raises(ValueError, match="Invalid agent name.*starts with a number"):
                Agent(name=name)

    def test_very_long_names_fail(self):
        """Test that very long names are rejected."""
        long_name = "A" * 101  # 101 characters

        with pytest.raises(
            ValueError, match="is 101 characters long.*shorter.*under 100 characters"
        ):
            validate_agent_name(long_name)

        with pytest.raises(ValueError, match="Invalid agent name.*is 101 characters long"):
            Agent(name=long_name)

    def test_exactly_100_characters_passes(self):
        """Test that names with exactly 100 characters are accepted."""
        name_100_chars = "A" * 100  # Exactly 100 characters

        # Should not raise
        validate_agent_name(name_100_chars)
        Agent(name=name_100_chars)

    def test_type_validation_still_works(self):
        """Test that type validation still works as before."""
        with pytest.raises(TypeError, match="Agent name must be a string, got int"):
            Agent(name=123)  # type: ignore

        with pytest.raises(TypeError, match="Agent name must be a string, got NoneType"):
            Agent(name=None)  # type: ignore

    def test_error_messages_are_helpful(self):
        """Test that error messages provide helpful guidance."""
        # Test whitespace suggestion
        with pytest.raises(ValueError) as exc_info:
            validate_agent_name(" Agent ")
        assert "Consider using 'Agent' instead" in str(exc_info.value)

        # Test character list in error
        with pytest.raises(ValueError) as exc_info:
            validate_agent_name("Agent@#$")
        error_msg = str(exc_info.value)
        assert "['#', '$', '@']" in error_msg
        assert "Consider using only letters, numbers, spaces, hyphens, and underscores" in error_msg

        # Test length guidance
        with pytest.raises(ValueError) as exc_info:
            validate_agent_name("A" * 150)
        assert "150 characters long" in str(exc_info.value)
        assert "under 100 characters" in str(exc_info.value)

    def test_existing_agent_creation_still_works(self):
        """Test that existing valid agent creation patterns still work."""
        # These should all work as before
        Agent(name="Assistant")
        Agent(name="Customer Service")
        Agent(name="data_processor")
        Agent(name="Multi-Agent-System")

        # Test with other parameters
        Agent(name="Test Agent", instructions="Test instructions")
        Agent(name="Another Agent", handoff_description="Test description")

    def test_validation_function_direct_usage(self):
        """Test that the validation function can be used directly."""
        # Should not raise
        validate_agent_name("Valid Agent")

        # Should raise with helpful message
        with pytest.raises(ValueError, match="Agent name cannot be empty"):
            validate_agent_name("")

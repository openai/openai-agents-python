from unittest.mock import Mock

import pytest

from agents import Agent, RunContextWrapper


class TestInstructionsSignatureValidation:
    """Test suite for instructions function signature validation"""

    @pytest.fixture
    def mock_run_context(self):
        """Create a mock RunContextWrapper for testing"""
        return Mock(spec=RunContextWrapper)

    @pytest.mark.asyncio
    async def test_valid_async_signature_passes(self, mock_run_context):
        """Test that async function with correct signature works"""
        async def valid_instructions(context, agent):
            return "Valid async instructions"

        agent = Agent(name="test_agent", instructions=valid_instructions)
        result = await agent.get_system_prompt(mock_run_context)
        assert result == "Valid async instructions"

    @pytest.mark.asyncio
    async def test_valid_sync_signature_passes(self, mock_run_context):
        """Test that sync function with correct signature works"""
        def valid_instructions(context, agent):
            return "Valid sync instructions"

        agent = Agent(name="test_agent", instructions=valid_instructions)
        result = await agent.get_system_prompt(mock_run_context)
        assert result == "Valid sync instructions"

    @pytest.mark.asyncio
    async def test_one_parameter_raises_error(self, mock_run_context):
        """Test that function with only one parameter raises TypeError"""
        def invalid_instructions(context):
            return "Should fail"

        agent = Agent(name="test_agent", instructions=invalid_instructions)  # type: ignore[arg-type]

        with pytest.raises(TypeError) as exc_info:
            await agent.get_system_prompt(mock_run_context)

        assert "must accept exactly 2 arguments" in str(exc_info.value)
        assert "but got 1" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_three_parameters_raises_error(self, mock_run_context):
        """Test that function with three parameters raises TypeError"""
        def invalid_instructions(context, agent, extra):
            return "Should fail"

        agent = Agent(name="test_agent", instructions=invalid_instructions)  # type: ignore[arg-type]

        with pytest.raises(TypeError) as exc_info:
            await agent.get_system_prompt(mock_run_context)

        assert "must accept exactly 2 arguments" in str(exc_info.value)
        assert "but got 3" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_zero_parameters_raises_error(self, mock_run_context):
        """Test that function with no parameters raises TypeError"""
        def invalid_instructions():
            return "Should fail"

        agent = Agent(name="test_agent", instructions=invalid_instructions)  # type: ignore[arg-type]

        with pytest.raises(TypeError) as exc_info:
            await agent.get_system_prompt(mock_run_context)

        assert "must accept exactly 2 arguments" in str(exc_info.value)
        assert "but got 0" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_function_with_args_kwargs_fails(self, mock_run_context):
        """Test that function with *args/**kwargs fails validation"""
        def flexible_instructions(context, agent, *args, **kwargs):
            return "Flexible instructions"

        agent = Agent(name="test_agent", instructions=flexible_instructions)

        with pytest.raises(TypeError) as exc_info:
            await agent.get_system_prompt(mock_run_context)

        assert "must accept exactly 2 arguments" in str(exc_info.value)
        assert "but got" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_string_instructions_still_work(self, mock_run_context):
        """Test that string instructions continue to work"""
        agent = Agent(name="test_agent", instructions="Static string instructions")
        result = await agent.get_system_prompt(mock_run_context)
        assert result == "Static string instructions"

    @pytest.mark.asyncio
    async def test_none_instructions_return_none(self, mock_run_context):
        """Test that None instructions return None"""
        agent = Agent(name="test_agent", instructions=None)
        result = await agent.get_system_prompt(mock_run_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_callable_instructions_raises_error(self, mock_run_context):
        """Test that non-callable instructions raise a TypeError during initialization"""
        with pytest.raises(TypeError) as exc_info:
            Agent(name="test_agent", instructions=123)  # type: ignore[arg-type]

        assert "Agent instructions must be a string, callable, or None" in str(exc_info.value)
        assert "got int" in str(exc_info.value)

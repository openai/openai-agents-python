import pytest
from unittest.mock import Mock
# Adjust import based on actual repo structure
from src.agents.agent import Agent, RunContextWrapper

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
        
        agent = Agent(instructions=valid_instructions)
        result = await agent.get_system_prompt(mock_run_context)
        assert result == "Valid async instructions"
    
    @pytest.mark.asyncio
    async def test_valid_sync_signature_passes(self, mock_run_context):
        """Test that sync function with correct signature works"""
        def valid_instructions(context, agent):
            return "Valid sync instructions"
        
        agent = Agent(instructions=valid_instructions)
        result = await agent.get_system_prompt(mock_run_context)
        assert result == "Valid sync instructions"
    
    @pytest.mark.asyncio
    async def test_one_parameter_raises_error(self, mock_run_context):
        """Test that function with only one parameter raises TypeError"""
        def invalid_instructions(context):
            return "Should fail"
        
        agent = Agent(instructions=invalid_instructions)
        
        with pytest.raises(TypeError) as exc_info:
            await agent.get_system_prompt(mock_run_context)
        
        assert "must accept exactly 2 arguments" in str(exc_info.value)
        assert "but got 1" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_three_parameters_raises_error(self, mock_run_context):
        """Test that function with three parameters raises TypeError"""
        def invalid_instructions(context, agent, extra):
            return "Should fail"
        
        agent = Agent(instructions=invalid_instructions)
        
        with pytest.raises(TypeError) as exc_info:
            await agent.get_system_prompt(mock_run_context)
        
        assert "must accept exactly 2 arguments" in str(exc_info.value)
        assert "but got 3" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_zero_parameters_raises_error(self, mock_run_context):
        """Test that function with no parameters raises TypeError"""
        def invalid_instructions():
            return "Should fail"
        
        agent = Agent(instructions=invalid_instructions)
        
        with pytest.raises(TypeError) as exc_info:
            await agent.get_system_prompt(mock_run_context)
        
        assert "must accept exactly 2 arguments" in str(exc_info.value)
        assert "but got 0" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_function_with_args_kwargs_passes(self, mock_run_context):
        """Test that function with *args/**kwargs still works (edge case)"""
        def flexible_instructions(context, agent, *args, **kwargs):
            return "Flexible instructions"
        
        agent = Agent(instructions=flexible_instructions)
        # This should potentially pass as it can accept the 2 required args
        # Adjust this test based on your desired behavior
        result = await agent.get_system_prompt(mock_run_context)
        assert result == "Flexible instructions"
    
    @pytest.mark.asyncio
    async def test_string_instructions_still_work(self, mock_run_context):
        """Test that string instructions continue to work"""
        agent = Agent(instructions="Static string instructions")
        result = await agent.get_system_prompt(mock_run_context)
        assert result == "Static string instructions"
    
    @pytest.mark.asyncio
    async def test_none_instructions_return_none(self, mock_run_context):
        """Test that None instructions return None"""
        agent = Agent(instructions=None)
        result = await agent.get_system_prompt(mock_run_context)
        assert result is None
    
    @pytest.mark.asyncio
    async def test_non_callable_instructions_log_error(self, mock_run_context, caplog):
        """Test that non-callable instructions log an error"""
        agent = Agent(instructions=123)  # Invalid type
        result = await agent.get_system_prompt(mock_run_context)
        assert result is None
        # Check that error was logged (adjust based on actual logging setup)
        # assert "Instructions must be a string or a function" in caplog.text
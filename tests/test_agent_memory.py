import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Assuming TResponseInputItem is accessible for type hinting and test data construction.
# Adjust the import path if necessary based on project structure.
# from src.agents.items import TResponseInputItem # Might be needed for direct use
# For testing, constructing dicts that match TResponseInputItem structure is often sufficient.

from src.agents.agent import Agent
from src.agents.memory import SessionMemory, SQLiteSessionMemory
from src.agents.run import Runner, RunConfig
from src.agents.models.interface import Model # For mock model
from src.agents.items import ModelResponse, TResponseInputItem, TResponseOutputItem # For constructing mock responses


# Test data - sample input items
user_msg_1: TResponseInputItem = {"role": "user", "content": "Hello there!"}
asst_msg_1: TResponseInputItem = {"role": "assistant", "content": "Hi! How can I help?"}
user_msg_2: TResponseInputItem = {"role": "user", "content": "What's the weather?"}
asst_msg_2: TResponseInputItem = {"role": "assistant", "content": "It's sunny!"}


@pytest.fixture
def in_memory_sqlite_memory() -> SQLiteSessionMemory:
    """Provides an in-memory SQLiteSessionMemory instance for testing."""
    memory = SQLiteSessionMemory(db_path=":memory:")
    # Ensure fresh table for each test using this fixture
    asyncio.run(memory.clear()) # Clear in case a previous test in the same session didn't clean up
    memory._init_db() # Re-initialize schema
    return memory

class TestSQLiteSessionMemory:
    @pytest.mark.asyncio
    async def test_add_and_get_history(self, in_memory_sqlite_memory: SQLiteSessionMemory):
        memory = in_memory_sqlite_memory
        await memory.add_message(user_msg_1)
        await memory.add_items([asst_msg_1, user_msg_2])

        history = await memory.get_history()
        assert len(history) == 3
        assert history[0] == user_msg_1
        assert history[1] == asst_msg_1
        assert history[2] == user_msg_2

    @pytest.mark.asyncio
    async def test_clear_history(self, in_memory_sqlite_memory: SQLiteSessionMemory):
        memory = in_memory_sqlite_memory
        await memory.add_message(user_msg_1)
        await memory.clear()
        history = await memory.get_history()
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_add_items_maintains_order(self, in_memory_sqlite_memory: SQLiteSessionMemory):
        memory = in_memory_sqlite_memory
        items = [{"role": "user", "content": f"Message {i}"} for i in range(5)]
        await memory.add_items(items)
        history = await memory.get_history()
        assert history == items

    def test_persistent_db(self, tmp_path):
        db_file = tmp_path / "test_persistent.db"
        memory1 = SQLiteSessionMemory(db_path=str(db_file))
        asyncio.run(memory1.add_message(user_msg_1))
        
        # Create a new instance with the same file
        memory2 = SQLiteSessionMemory(db_path=str(db_file))
        history = asyncio.run(memory2.get_history())
        assert len(history) == 1
        assert history[0] == user_msg_1
        asyncio.run(memory2.clear()) # Clean up


class TestAgentMemoryInitialization:
    def test_agent_memory_true_initializes_sqlite(self):
        agent = Agent(name="TestAgent", memory=True)
        assert isinstance(agent.memory, SQLiteSessionMemory)

    def test_agent_memory_false_sets_none(self):
        agent = Agent(name="TestAgent", memory=False)
        assert agent.memory is None

    def test_agent_memory_none_is_default(self):
        agent = Agent(name="TestAgent")
        assert agent.memory is None

    def test_agent_custom_memory_instance(self):
        custom_memory_mock = AsyncMock(spec=SessionMemory)
        agent = Agent(name="TestAgent", memory=custom_memory_mock)
        assert agent.memory is custom_memory_mock


@pytest.mark.asyncio
async def test_runner_with_memory_integration():
    """Test Runner.run with an agent that has memory over multiple turns."""
    
    # Mock the model provider and model
    mock_model = AsyncMock(spec=Model)
    
    # Define model responses for multiple turns
    # Turn 1: User says "Hello", Assistant says "Hi"
    model_response_1_output: list[TResponseOutputItem] = [{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Hi"}]}]
    model_response_1 = ModelResponse(output=model_response_1_output, usage=MagicMock(), response_id="res1")

    # Turn 2: User says "State?", Assistant says "California"
    model_response_2_output: list[TResponseOutputItem] = [{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "California"}]}]
    model_response_2 = ModelResponse(output=model_response_2_output, usage=MagicMock(), response_id="res2")

    mock_model.get_response.side_effect = [model_response_1, model_response_2]
    # stream_response would also need mocking if testing streaming runs here

    mock_provider = MagicMock()
    mock_provider.get_model.return_value = mock_model

    run_config = RunConfig(model_provider=mock_provider)
    
    # Use a real SQLiteSessionMemory (in-memory)
    agent_memory = SQLiteSessionMemory(db_path=":memory:")
    agent = Agent(name="TestAgentWithMemory", model="test-model", memory=agent_memory)

    # Turn 1
    input_turn_1 = "Hello"
    result_turn_1 = await Runner.run(agent, input_turn_1, run_config=run_config)
    
    assert result_turn_1.final_output == "Hi" # Assuming default output_type is str
    
    # Check memory after turn 1
    history_after_turn_1 = await agent_memory.get_history()
    assert len(history_after_turn_1) == 2 # User: Hello, Assistant: Hi
    assert history_after_turn_1[0]["role"] == "user"
    assert history_after_turn_1[0]["content"] == "Hello"
    assert history_after_turn_1[1]["role"] == "assistant"
    assert history_after_turn_1[1]["content"] == [{"type": "text", "text": "Hi"}] # ModelResponse output structure

    # Verify model was called with correct history (i.e., empty for first turn)
    args_call_1, kwargs_call_1 = mock_model.get_response.call_args_list[0]
    input_to_model_turn_1 = kwargs_call_1.get('input')
    assert len(input_to_model_turn_1) == 1
    assert input_to_model_turn_1[0]["content"] == "Hello"


    # Turn 2 - using the *same agent instance* which now has memory
    input_turn_2 = "What state is it in?" # Example from issue
    result_turn_2 = await Runner.run(agent, input_turn_2, run_config=run_config)

    assert result_turn_2.final_output == "California"

    # Check memory after turn 2
    history_after_turn_2 = await agent_memory.get_history()
    assert len(history_after_turn_2) == 4 # User: Hello, Asst: Hi, User: State?, Asst: California
    assert history_after_turn_2[2]["role"] == "user"
    assert history_after_turn_2[2]["content"] == "What state is it in?"
    assert history_after_turn_2[3]["role"] == "assistant"
    assert history_after_turn_2[3]["content"] == [{"type": "text", "text": "California"}]


    # Verify model was called with history for second turn
    args_call_2, kwargs_call_2 = mock_model.get_response.call_args_list[1]
    input_to_model_turn_2 = kwargs_call_2.get('input')

    assert len(input_to_model_turn_2) == 3 # History (User:Hello, Asst:Hi) + New (User:State?)
    assert input_to_model_turn_2[0]["role"] == "user"
    assert input_to_model_turn_2[0]["content"] == "Hello"
    assert input_to_model_turn_2[1]["role"] == "assistant"
    # The content saved from ModelResponse is the list of dicts, not just plain text
    assert input_to_model_turn_2[1]["content"] == [{"type": "text", "text": "Hi"}] 
    assert input_to_model_turn_2[2]["role"] == "user"
    assert input_to_model_turn_2[2]["content"] == "What state is it in?"
    
    # Test clearing memory via agent reference (if Agent had a clear_memory method, or directly)
    await agent.memory.clear()
    history_after_clear = await agent_memory.get_history()
    assert len(history_after_clear) == 0


@pytest.mark.asyncio
async def test_issue_example_with_memory():
    """Test the specific example from the issue description."""
    mock_model = AsyncMock(spec=Model)
    
    # Mock responses
    # 1. "What city is the Golden Gate Bridge in?" -> "San Francisco"
    res1_output: list[TResponseOutputItem] = [{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "San Francisco"}]}]
    res1 = ModelResponse(output=res1_output, usage=MagicMock(), response_id="res_city")

    # 2. "What state is it in?" -> "California"
    res2_output: list[TResponseOutputItem] = [{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "California"}]}]
    res2 = ModelResponse(output=res2_output, usage=MagicMock(), response_id="res_state")
    
    mock_model.get_response.side_effect = [res1, res2]

    mock_provider = MagicMock()
    mock_provider.get_model.return_value = mock_model
    run_config = RunConfig(model_provider=mock_provider)

    # Agent with memory=True uses default SQLiteSessionMemory
    agent = Agent(name="Assistant", instructions="Reply very concisely.", model="test-model", memory=True)

    # First turn
    result1 = await Runner.run(agent, "What city is the Golden Gate Bridge in?", run_config=run_config)
    # print(f"Result 1: {result1.final_output}") # For debugging if needed
    assert result1.final_output == "San Francisco"

    # Check memory content (optional, but good for deep check)
    if isinstance(agent.memory, SQLiteSessionMemory): # Should be
        history1 = await agent.memory.get_history()
        assert len(history1) == 2
        assert history1[0]["content"] == "What city is the Golden Gate Bridge in?"
        assert history1[1]["content"] == [{"type": "text", "text": "San Francisco"}]


    # Second turn - memory should be used automatically
    result2 = await Runner.run(agent, "What state is it in?", run_config=run_config)
    # print(f"Result 2: {result2.final_output}") # For debugging
    assert result2.final_output == "California"
    
    # Check that the model received the history in the second call
    assert mock_model.get_response.call_count == 2
    args_call_2, kwargs_call_2 = mock_model.get_response.call_args_list[1]
    input_to_model_turn_2 = kwargs_call_2.get('input')
    
    assert len(input_to_model_turn_2) == 3 # History (User, Asst) + New User Question
    assert input_to_model_turn_2[0]["content"] == "What city is the Golden Gate Bridge in?"
    assert input_to_model_turn_2[1]["content"] == [{"type": "text", "text": "San Francisco"}]
    assert input_to_model_turn_2[2]["content"] == "What state is it in?"

# TODO: Add tests for streaming runs with memory if time permits.
# The logic in _run_single_turn_streamed is very similar to _run_single_turn,
# so these tests provide good coverage of the core memory handling.

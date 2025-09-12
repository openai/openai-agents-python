"""Tests for AdvancedSQLiteSession functionality."""

from typing import Any, Optional, cast

import pytest

pytest.importorskip("sqlalchemy")  # Skip tests if SQLAlchemy is not installed
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents import Agent, Runner, TResponseInputItem, function_tool
from agents.extensions.memory import AdvancedSQLiteSession
from agents.result import RunResult
from agents.run_context import RunContextWrapper
from agents.usage import Usage
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio


@function_tool
async def test_tool(query: str) -> str:
    """A test tool for testing tool call tracking."""
    return f"Tool result for: {query}"


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel(), tools=[test_tool])


@pytest.fixture
def usage_data() -> Usage:
    """Fixture for test usage data."""
    return Usage(
        requests=1,
        input_tokens=50,
        output_tokens=30,
        total_tokens=80,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
    )


def create_mock_run_result(
    usage: Optional[Usage] = None, agent: Optional[Agent] = None
) -> RunResult:
    """Helper function to create a mock RunResult for testing."""
    if agent is None:
        agent = Agent(name="test", model=FakeModel())

    if usage is None:
        usage = Usage(
            requests=1,
            input_tokens=50,
            output_tokens=30,
            total_tokens=80,
            input_tokens_details=InputTokensDetails(cached_tokens=10),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
        )

    context_wrapper = RunContextWrapper(context=None, usage=usage)

    return RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="test output",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=context_wrapper,
        _last_agent=agent,
    )


async def test_advanced_session_basic_functionality(agent: Agent):
    """Test basic AdvancedSQLiteSession functionality."""
    session_id = "advanced_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test basic session operations work
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    # Get items and verify
    retrieved = await session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"

    session.close()


async def test_message_structure_tracking(agent: Agent):
    """Test that message structure is properly tracked."""
    session_id = "structure_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add various types of messages
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "What's 2+2?"},
        {"type": "function_call", "name": "calculator", "arguments": '{"expression": "2+2"}'},  # type: ignore
        {"type": "function_call_output", "output": "4"},  # type: ignore
        {"role": "assistant", "content": "The answer is 4"},
        {"type": "reasoning", "summary": [{"text": "Simple math", "type": "summary_text"}]},  # type: ignore
    ]
    await session.add_items(items)

    # Get conversation structure
    conversation_turns = await session.get_conversation_by_turns()
    assert len(conversation_turns) == 1  # Should be one user turn

    turn_1_items = conversation_turns[1]
    assert len(turn_1_items) == 5

    # Verify item types are classified correctly
    item_types = [item["type"] for item in turn_1_items]
    assert "user" in item_types
    assert "function_call" in item_types
    assert "function_call_output" in item_types
    assert "assistant" in item_types
    assert "reasoning" in item_types

    session.close()


async def test_tool_usage_tracking(agent: Agent):
    """Test tool usage tracking functionality."""
    session_id = "tools_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items with tool calls
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Search for cats"},
        {"type": "function_call", "name": "web_search", "arguments": '{"query": "cats"}'},  # type: ignore
        {"type": "function_call_output", "output": "Found cat information"},  # type: ignore
        {"type": "function_call", "name": "calculator", "arguments": '{"expression": "1+1"}'},  # type: ignore
        {"type": "function_call_output", "output": "2"},  # type: ignore
        {"role": "assistant", "content": "I found information about cats and calculated 1+1=2"},
    ]
    await session.add_items(items)

    # Get tool usage
    tool_usage = await session.get_tool_usage()
    assert len(tool_usage) == 2  # Two different tools used

    tool_names = {usage[0] for usage in tool_usage}
    assert "web_search" in tool_names
    assert "calculator" in tool_names

    session.close()


async def test_soft_deletion_functionality(agent: Agent):
    """Test soft deletion and reactivation functionality."""
    session_id = "deletion_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add multiple turns
    turn_1_items: list[TResponseInputItem] = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
    ]
    await session.add_items(turn_1_items)

    turn_2_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Second question"},
        {"role": "assistant", "content": "Second answer"},
    ]
    await session.add_items(turn_2_items)

    turn_3_items: list[TResponseInputItem] = [
        {"role": "user", "content": "Third question"},
        {"role": "assistant", "content": "Third answer"},
    ]
    await session.add_items(turn_3_items)

    # Verify all items are active
    all_items = await session.get_items()
    assert len(all_items) == 6

    # Soft delete from turn 2 onwards
    deleted = await session.soft_delete_from_turn(2)
    assert deleted is True

    # Verify only turn 1 items are active
    active_items = await session.get_items()
    assert len(active_items) == 2
    assert active_items[0].get("content") == "First question"
    assert active_items[1].get("content") == "First answer"

    # Verify we can still get inactive items
    all_items_including_inactive = await session.get_items(include_inactive=True)
    assert len(all_items_including_inactive) == 6

    # Test reactivation
    reactivated = await session.reactivate_from_turn(2)
    assert reactivated is True

    # Verify all items are active again
    all_active_again = await session.get_items()
    assert len(all_active_again) == 6

    session.close()


async def test_usage_tracking_storage(agent: Agent, usage_data: Usage):
    """Test usage data storage and retrieval."""
    session_id = "usage_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Simulate adding items for turn 1 to increment turn counter
    await session.add_items([{"role": "user", "content": "First turn"}])
    run_result_1 = create_mock_run_result(usage_data)
    await session.store_run_usage(run_result_1)

    # Create different usage data for turn 2
    usage_data_2 = Usage(
        requests=2,
        input_tokens=75,
        output_tokens=45,
        total_tokens=120,
        input_tokens_details=InputTokensDetails(cached_tokens=20),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=15),
    )

    # Simulate adding items for turn 2 to increment turn counter
    await session.add_items([{"role": "user", "content": "Second turn"}])
    run_result_2 = create_mock_run_result(usage_data_2)
    await session.store_run_usage(run_result_2)

    # Test session-level usage aggregation
    session_usage = await session.get_session_usage()
    assert session_usage is not None
    assert session_usage["requests"] == 3  # 1 + 2
    assert session_usage["total_tokens"] == 200  # 80 + 120
    assert session_usage["input_tokens"] == 125  # 50 + 75
    assert session_usage["output_tokens"] == 75  # 30 + 45
    assert session_usage["total_turns"] == 2

    # Test turn-level usage retrieval
    turn_1_usage = await session.get_turn_usage(1)
    assert isinstance(turn_1_usage, dict)
    assert turn_1_usage["requests"] == 1
    assert turn_1_usage["total_tokens"] == 80
    assert turn_1_usage["input_tokens_details"]["cached_tokens"] == 10
    assert turn_1_usage["output_tokens_details"]["reasoning_tokens"] == 5

    turn_2_usage = await session.get_turn_usage(2)
    assert isinstance(turn_2_usage, dict)
    assert turn_2_usage["requests"] == 2
    assert turn_2_usage["total_tokens"] == 120
    assert turn_2_usage["input_tokens_details"]["cached_tokens"] == 20
    assert turn_2_usage["output_tokens_details"]["reasoning_tokens"] == 15

    # Test getting all turn usage
    all_turn_usage = await session.get_turn_usage()
    assert isinstance(all_turn_usage, list)
    assert len(all_turn_usage) == 2
    assert all_turn_usage[0]["user_turn_number"] == 1
    assert all_turn_usage[1]["user_turn_number"] == 2

    session.close()


async def test_runner_integration_with_usage_tracking(agent: Agent):
    """Test integration with Runner and automatic usage tracking pattern."""
    session_id = "integration_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    async def store_session_usage(result: Any, session: AdvancedSQLiteSession):
        """Helper function to store usage after runner completes."""
        try:
            await session.store_run_usage(result)
        except Exception:
            # Ignore errors in test helper
            pass

    # Set up fake model responses
    assert isinstance(agent.model, FakeModel)
    fake_model = agent.model
    fake_model.set_next_output([get_text_message("San Francisco")])

    # First turn
    result1 = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    assert result1.final_output == "San Francisco"
    await store_session_usage(result1, session)

    # Second turn
    fake_model.set_next_output([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"
    await store_session_usage(result2, session)

    # Verify conversation structure
    conversation_turns = await session.get_conversation_by_turns()
    assert len(conversation_turns) == 2

    # Verify usage was tracked
    session_usage = await session.get_session_usage()
    assert session_usage is not None
    assert session_usage["total_turns"] == 2
    # FakeModel doesn't generate realistic usage data, so we just check structure exists
    assert "requests" in session_usage
    assert "total_tokens" in session_usage

    session.close()


async def test_sequence_ordering():
    """Test that sequence ordering works correctly even with same timestamps."""
    session_id = "sequence_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add multiple items quickly to test sequence ordering
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Response 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Response 2"},
    ]
    await session.add_items(items)

    # Get items and verify order is preserved
    retrieved = await session.get_items()
    assert len(retrieved) == 4
    assert retrieved[0].get("content") == "Message 1"
    assert retrieved[1].get("content") == "Response 1"
    assert retrieved[2].get("content") == "Message 2"
    assert retrieved[3].get("content") == "Response 2"

    session.close()


async def test_conversation_structure_with_multiple_turns():
    """Test conversation structure tracking with multiple user turns."""
    session_id = "multi_turn_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Turn 1
    turn_1: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi!"},
    ]
    await session.add_items(turn_1)

    # Turn 2
    turn_2: list[TResponseInputItem] = [
        {"role": "user", "content": "How are you?"},
        {"type": "function_call", "name": "mood_check", "arguments": "{}"},  # type: ignore
        {"type": "function_call_output", "output": "I'm good"},  # type: ignore
        {"role": "assistant", "content": "I'm doing well!"},
    ]
    await session.add_items(turn_2)

    # Turn 3
    turn_3: list[TResponseInputItem] = [
        {"role": "user", "content": "Goodbye"},
        {"role": "assistant", "content": "See you later!"},
    ]
    await session.add_items(turn_3)

    # Verify conversation structure
    conversation_turns = await session.get_conversation_by_turns()
    assert len(conversation_turns) == 3

    # Turn 1 should have 2 items
    assert len(conversation_turns[1]) == 2
    assert conversation_turns[1][0]["type"] == "user"
    assert conversation_turns[1][1]["type"] == "assistant"

    # Turn 2 should have 4 items including tool calls
    assert len(conversation_turns[2]) == 4
    turn_2_types = [item["type"] for item in conversation_turns[2]]
    assert "user" in turn_2_types
    assert "function_call" in turn_2_types
    assert "function_call_output" in turn_2_types
    assert "assistant" in turn_2_types

    # Turn 3 should have 2 items
    assert len(conversation_turns[3]) == 2

    session.close()


async def test_empty_session_operations():
    """Test operations on empty sessions."""
    session_id = "empty_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test getting items from empty session
    items = await session.get_items()
    assert len(items) == 0

    # Test getting conversation from empty session
    conversation = await session.get_conversation_by_turns()
    assert len(conversation) == 0

    # Test getting tool usage from empty session
    tool_usage = await session.get_tool_usage()
    assert len(tool_usage) == 0

    # Test getting session usage from empty session
    session_usage = await session.get_session_usage()
    assert session_usage is None

    # Test soft deletion on empty session
    deleted = await session.soft_delete_from_turn(1)
    assert deleted is False

    session.close()


async def test_json_serialization_edge_cases(usage_data: Usage):
    """Test edge cases in JSON serialization of usage data."""
    session_id = "json_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test with normal usage data (need to add user message first to create turn)
    await session.add_items([{"role": "user", "content": "First test"}])
    run_result_1 = create_mock_run_result(usage_data)
    await session.store_run_usage(run_result_1)

    # Test with None usage data
    run_result_none = create_mock_run_result(None)
    await session.store_run_usage(run_result_none)

    # Test with usage data missing details
    minimal_usage = Usage(
        requests=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    await session.add_items([{"role": "user", "content": "Second test"}])
    run_result_2 = create_mock_run_result(minimal_usage)
    await session.store_run_usage(run_result_2)

    # Verify we can retrieve the data
    turn_1_usage = await session.get_turn_usage(1)
    assert isinstance(turn_1_usage, dict)
    assert turn_1_usage["requests"] == 1
    assert turn_1_usage["input_tokens_details"]["cached_tokens"] == 10

    turn_2_usage = await session.get_turn_usage(2)
    assert isinstance(turn_2_usage, dict)
    assert turn_2_usage["requests"] == 1
    # Should have default values for minimal data (Usage class provides defaults)
    assert turn_2_usage["input_tokens_details"]["cached_tokens"] == 0
    assert turn_2_usage["output_tokens_details"]["reasoning_tokens"] == 0

    session.close()


async def test_session_isolation():
    """Test that different session IDs maintain separate data."""
    session1 = AdvancedSQLiteSession(session_id="session_1", create_tables=True)
    session2 = AdvancedSQLiteSession(session_id="session_2", create_tables=True)

    # Add data to session 1
    await session1.add_items([{"role": "user", "content": "Session 1 message"}])

    # Add data to session 2
    await session2.add_items([{"role": "user", "content": "Session 2 message"}])

    # Verify isolation
    session1_items = await session1.get_items()
    session2_items = await session2.get_items()

    assert len(session1_items) == 1
    assert len(session2_items) == 1
    assert session1_items[0].get("content") == "Session 1 message"
    assert session2_items[0].get("content") == "Session 2 message"

    # Test conversation structure isolation
    session1_turns = await session1.get_conversation_by_turns()
    session2_turns = await session2.get_conversation_by_turns()

    assert len(session1_turns) == 1
    assert len(session2_turns) == 1

    session1.close()
    session2.close()


async def test_error_handling_in_usage_tracking(usage_data: Usage):
    """Test that usage tracking errors don't break the main flow."""
    session_id = "error_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Test normal operation
    run_result = create_mock_run_result(usage_data)
    await session.store_run_usage(run_result)

    # Close the session to simulate database errors
    session.close()

    # This should not raise an exception (error should be caught)
    await session.store_run_usage(run_result)


async def test_tool_name_extraction():
    """Test that tool names are correctly extracted from different item types."""
    session_id = "tool_names_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Add items with different ways of specifying tool names
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Use tools please"},  # Need user message to create turn
        {"type": "function_call", "name": "search_web", "arguments": "{}"},  # type: ignore
        {"type": "function_call_output", "tool_name": "search_web", "output": "result"},  # type: ignore
        {"type": "function_call", "name": "calculator", "arguments": "{}"},  # type: ignore
    ]
    await session.add_items(items)

    # Get conversation structure and verify tool names
    conversation_turns = await session.get_conversation_by_turns()
    turn_items = conversation_turns[1]

    tool_items = [item for item in turn_items if item["tool_name"]]
    tool_names = [item["tool_name"] for item in tool_items]

    assert "search_web" in tool_names
    assert "calculator" in tool_names

    session.close()


async def test_tool_execution_integration(agent: Agent):
    """Test integration with actual tool execution."""
    session_id = "tool_integration_test"
    session = AdvancedSQLiteSession(session_id=session_id, create_tables=True)

    # Set up the fake model to trigger a tool call
    fake_model = cast(FakeModel, agent.model)
    fake_model.set_next_output(
        [
            {  # type: ignore
                "type": "function_call",
                "name": "test_tool",
                "arguments": '{"query": "test query"}',
                "call_id": "call_123",
            }
        ]
    )

    # Then set the final response
    fake_model.set_next_output([get_text_message("Tool executed successfully")])

    # Run the agent
    result = await Runner.run(
        agent,
        "Please use the test tool",
        session=session,
    )

    # Verify the tool was executed
    assert "Tool result for: test query" in str(result.new_items)

    # Verify tool usage was tracked
    tool_usage = await session.get_tool_usage()
    assert len(tool_usage) > 0

    session.close()

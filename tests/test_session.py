"""Tests for session memory functionality."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from agents import Agent, Runner, SQLiteSession, TResponseInputItem
from agents.exceptions import UserError

from .fake_model import FakeModel
from .test_responses import get_text_message


# Helper functions for parametrized testing of different Runner methods
def _run_sync_wrapper(agent, input_data, **kwargs):
    """Wrapper for run_sync that properly sets up an event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return Runner.run_sync(agent, input_data, **kwargs)
    finally:
        loop.close()


async def run_agent_async(runner_method: str, agent, input_data, **kwargs):
    """Helper function to run agent with different methods."""
    if runner_method == "run":
        return await Runner.run(agent, input_data, **kwargs)
    elif runner_method == "run_sync":
        # For run_sync, we need to run it in a thread with its own event loop
        return await asyncio.to_thread(_run_sync_wrapper, agent, input_data, **kwargs)
    elif runner_method == "run_streamed":
        result = Runner.run_streamed(agent, input_data, **kwargs)
        # For streaming, we first try to get at least one event to trigger any early exceptions
        # If there's an exception in setup (like memory validation), it will be raised here
        try:
            first_event = None
            async for event in result.stream_events():
                if first_event is None:
                    first_event = event
                # Continue consuming all events
                pass
        except Exception:
            # If an exception occurs during streaming, we let it propagate up
            raise
        return result
    else:
        raise ValueError(f"Unknown runner method: {runner_method}")


# Parametrized tests for different runner methods
@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_memory_basic_functionality_parametrized(runner_method):
    """Test basic session memory functionality with SQLite backend across all runner methods."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"
        session_id = "test_session_123"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # First turn
        model.set_next_output([get_text_message("San Francisco")])
        result1 = await run_agent_async(
            runner_method,
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        assert result1.final_output == "San Francisco"

        # Second turn - should have conversation history
        model.set_next_output([get_text_message("California")])
        result2 = await run_agent_async(
            runner_method,
            agent,
            "What state is it in?",
            session=session,
        )
        assert result2.final_output == "California"

        # Verify that the input to the second turn includes the previous conversation
        # The model should have received the full conversation history
        last_input = model.last_turn_args["input"]
        assert len(last_input) > 1  # Should have more than just the current message

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_memory_with_explicit_instance_parametrized(runner_method):
    """Test session memory with an explicit SQLiteSession instance across all runner methods."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"
        session_id = "test_session_456"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # First turn
        model.set_next_output([get_text_message("Hello")])
        result1 = await run_agent_async(runner_method, agent, "Hi there", session=session)
        assert result1.final_output == "Hello"

        # Second turn
        model.set_next_output([get_text_message("I remember you said hi")])
        result2 = await run_agent_async(
            runner_method,
            agent,
            "Do you remember what I said?",
            session=session,
        )
        assert result2.final_output == "I remember you said hi"

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_memory_disabled_parametrized(runner_method):
    """Test that session memory is disabled when session=None across all runner methods."""
    model = FakeModel()
    agent = Agent(name="test", model=model)

    # First turn (no session parameters = disabled)
    model.set_next_output([get_text_message("Hello")])
    result1 = await run_agent_async(runner_method, agent, "Hi there")
    assert result1.final_output == "Hello"

    # Second turn - should NOT have conversation history
    model.set_next_output([get_text_message("I don't remember")])
    result2 = await run_agent_async(runner_method, agent, "Do you remember what I said?")
    assert result2.final_output == "I don't remember"

    # Verify that the input to the second turn is just the current message
    last_input = model.last_turn_args["input"]
    assert len(last_input) == 1  # Should only have the current message


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_memory_different_sessions_parametrized(runner_method):
    """Test that different session IDs maintain separate conversation histories across all runner
    methods."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Session 1
        session_id_1 = "session_1"
        session_1 = SQLiteSession(session_id_1, db_path)

        model.set_next_output([get_text_message("I like cats")])
        result1 = await run_agent_async(runner_method, agent, "I like cats", session=session_1)
        assert result1.final_output == "I like cats"

        # Session 2 - different session
        session_id_2 = "session_2"
        session_2 = SQLiteSession(session_id_2, db_path)

        model.set_next_output([get_text_message("I like dogs")])
        result2 = await run_agent_async(runner_method, agent, "I like dogs", session=session_2)
        assert result2.final_output == "I like dogs"

        # Back to Session 1 - should remember cats, not dogs
        model.set_next_output([get_text_message("Yes, you mentioned cats")])
        result3 = await run_agent_async(
            runner_method,
            agent,
            "What did I say I like?",
            session=session_1,
        )
        assert result3.final_output == "Yes, you mentioned cats"

        session_1.close()
        session_2.close()


@pytest.mark.asyncio
async def test_sqlite_session_memory_direct():
    """Test SQLiteSession class directly."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_direct.db"
        session_id = "direct_test"
        session = SQLiteSession(session_id, db_path)

        # Test adding and retrieving items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        await session.add_items(items)
        retrieved = await session.get_items()

        assert len(retrieved) == 2
        assert retrieved[0].get("role") == "user"
        assert retrieved[0].get("content") == "Hello"
        assert retrieved[1].get("role") == "assistant"
        assert retrieved[1].get("content") == "Hi there!"

        # Test clearing session
        await session.clear_session()
        retrieved_after_clear = await session.get_items()
        assert len(retrieved_after_clear) == 0

        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_memory_pop_item():
    """Test SQLiteSession pop_item functionality."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_pop.db"
        session_id = "pop_test"
        session = SQLiteSession(session_id, db_path)

        # Test popping from empty session
        popped = await session.pop_item()
        assert popped is None

        # Add items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]

        await session.add_items(items)

        # Verify all items are there
        retrieved = await session.get_items()
        assert len(retrieved) == 3

        # Pop the most recent item
        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("role") == "user"
        assert popped.get("content") == "How are you?"

        # Verify item was removed
        retrieved_after_pop = await session.get_items()
        assert len(retrieved_after_pop) == 2
        assert retrieved_after_pop[-1].get("content") == "Hi there!"

        # Pop another item
        popped2 = await session.pop_item()
        assert popped2 is not None
        assert popped2.get("role") == "assistant"
        assert popped2.get("content") == "Hi there!"

        # Pop the last item
        popped3 = await session.pop_item()
        assert popped3 is not None
        assert popped3.get("role") == "user"
        assert popped3.get("content") == "Hello"

        # Try to pop from empty session again
        popped4 = await session.pop_item()
        assert popped4 is None

        # Verify session is empty
        final_items = await session.get_items()
        assert len(final_items) == 0

        session.close()


@pytest.mark.asyncio
async def test_session_memory_pop_different_sessions():
    """Test that pop_item only affects the specified session."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_pop_sessions.db"

        session_1_id = "session_1"
        session_2_id = "session_2"
        session_1 = SQLiteSession(session_1_id, db_path)
        session_2 = SQLiteSession(session_2_id, db_path)

        # Add items to both sessions
        items_1: list[TResponseInputItem] = [
            {"role": "user", "content": "Session 1 message"},
        ]
        items_2: list[TResponseInputItem] = [
            {"role": "user", "content": "Session 2 message 1"},
            {"role": "user", "content": "Session 2 message 2"},
        ]

        await session_1.add_items(items_1)
        await session_2.add_items(items_2)

        # Pop from session 2
        popped = await session_2.pop_item()
        assert popped is not None
        assert popped.get("content") == "Session 2 message 2"

        # Verify session 1 is unaffected
        session_1_items = await session_1.get_items()
        assert len(session_1_items) == 1
        assert session_1_items[0].get("content") == "Session 1 message"

        # Verify session 2 has one item left
        session_2_items = await session_2.get_items()
        assert len(session_2_items) == 1
        assert session_2_items[0].get("content") == "Session 2 message 1"

        session_1.close()
        session_2.close()


@pytest.mark.asyncio
async def test_sqlite_session_get_items_with_limit():
    """Test SQLiteSession get_items with limit parameter."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_count.db"
        session_id = "count_test"
        session = SQLiteSession(session_id, db_path)

        # Add multiple items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Message 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Message 3"},
            {"role": "assistant", "content": "Response 3"},
        ]

        await session.add_items(items)

        # Test getting all items (default behavior)
        all_items = await session.get_items()
        assert len(all_items) == 6
        assert all_items[0].get("content") == "Message 1"
        assert all_items[-1].get("content") == "Response 3"

        # Test getting latest 2 items
        latest_2 = await session.get_items(limit=2)
        assert len(latest_2) == 2
        assert latest_2[0].get("content") == "Message 3"
        assert latest_2[1].get("content") == "Response 3"

        # Test getting latest 4 items
        latest_4 = await session.get_items(limit=4)
        assert len(latest_4) == 4
        assert latest_4[0].get("content") == "Message 2"
        assert latest_4[1].get("content") == "Response 2"
        assert latest_4[2].get("content") == "Message 3"
        assert latest_4[3].get("content") == "Response 3"

        # Test getting more items than available
        latest_10 = await session.get_items(limit=10)
        assert len(latest_10) == 6  # Should return all available items
        assert latest_10[0].get("content") == "Message 1"
        assert latest_10[-1].get("content") == "Response 3"

        # Test getting 0 items
        latest_0 = await session.get_items(limit=0)
        assert len(latest_0) == 0

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_memory_rejects_both_session_and_list_input(runner_method):
    """Test that passing both a session and list input raises a UserError across all runner
    methods.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_validation.db"
        session_id = "test_validation_parametrized"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Test that providing both a session and a list input raises a UserError
        model.set_next_output([get_text_message("This shouldn't run")])

        list_input = [
            {"role": "user", "content": "Test message"},
        ]

        with pytest.raises(UserError) as exc_info:
            await run_agent_async(runner_method, agent, list_input, session=session)

        # Verify the error message explains the issue
        assert "Cannot provide both a session and a list of input items" in str(exc_info.value)
        assert "manually manage conversation history" in str(exc_info.value)

        session.close()

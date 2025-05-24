"""Tests for session memory functionality."""

import pytest
import tempfile
from pathlib import Path
import asyncio

from agents import Agent, Runner, SQLiteSession

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
        result1 = await run_agent_async(
            runner_method, agent, "Hi there", session=session
        )
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
    result2 = await run_agent_async(
        runner_method, agent, "Do you remember what I said?"
    )
    assert result2.final_output == "I don't remember"

    # Verify that the input to the second turn is just the current message
    last_input = model.last_turn_args["input"]
    assert len(last_input) == 1  # Should only have the current message


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_memory_different_sessions_parametrized(runner_method):
    """Test that different session IDs maintain separate conversation histories across all runner methods."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Session 1
        session_id_1 = "session_1"
        session_1 = SQLiteSession(session_id_1, db_path)

        model.set_next_output([get_text_message("I like cats")])
        result1 = await run_agent_async(
            runner_method, agent, "I like cats", session=session_1
        )
        assert result1.final_output == "I like cats"

        # Session 2 - different session
        session_id_2 = "session_2"
        session_2 = SQLiteSession(session_id_2, db_path)

        model.set_next_output([get_text_message("I like dogs")])
        result2 = await run_agent_async(
            runner_method, agent, "I like dogs", session=session_2
        )
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

        # Test adding and retrieving messages
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        await session.add_messages(messages)
        retrieved = await session.get_messages()

        assert len(retrieved) == 2
        assert retrieved[0]["role"] == "user"
        assert retrieved[0]["content"] == "Hello"
        assert retrieved[1]["role"] == "assistant"
        assert retrieved[1]["content"] == "Hi there!"

        # Test clearing session
        await session.clear_session()
        retrieved_after_clear = await session.get_messages()
        assert len(retrieved_after_clear) == 0

        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_memory_pop_message():
    """Test SQLiteSession pop_message functionality."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_pop.db"
        session_id = "pop_test"
        session = SQLiteSession(session_id, db_path)

        # Test popping from empty session
        popped = await session.pop_message()
        assert popped is None

        # Add messages
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]

        await session.add_messages(messages)

        # Verify all messages are there
        retrieved = await session.get_messages()
        assert len(retrieved) == 3

        # Pop the most recent message
        popped = await session.pop_message()
        assert popped is not None
        assert popped["role"] == "user"
        assert popped["content"] == "How are you?"

        # Verify message was removed
        retrieved_after_pop = await session.get_messages()
        assert len(retrieved_after_pop) == 2
        assert retrieved_after_pop[-1]["content"] == "Hi there!"

        # Pop another message
        popped2 = await session.pop_message()
        assert popped2 is not None
        assert popped2["role"] == "assistant"
        assert popped2["content"] == "Hi there!"

        # Pop the last message
        popped3 = await session.pop_message()
        assert popped3 is not None
        assert popped3["role"] == "user"
        assert popped3["content"] == "Hello"

        # Try to pop from empty session again
        popped4 = await session.pop_message()
        assert popped4 is None

        # Verify session is empty
        final_messages = await session.get_messages()
        assert len(final_messages) == 0

        session.close()


@pytest.mark.asyncio
async def test_session_memory_pop_different_sessions():
    """Test that pop_message only affects the specified session."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_pop_sessions.db"

        session_1_id = "session_1"
        session_2_id = "session_2"
        session_1 = SQLiteSession(session_1_id, db_path)
        session_2 = SQLiteSession(session_2_id, db_path)

        # Add messages to both sessions
        messages_1 = [
            {"role": "user", "content": "Session 1 message"},
        ]
        messages_2 = [
            {"role": "user", "content": "Session 2 message 1"},
            {"role": "user", "content": "Session 2 message 2"},
        ]

        await session_1.add_messages(messages_1)
        await session_2.add_messages(messages_2)

        # Pop from session 2
        popped = await session_2.pop_message()
        assert popped is not None
        assert popped["content"] == "Session 2 message 2"

        # Verify session 1 is unaffected
        session_1_messages = await session_1.get_messages()
        assert len(session_1_messages) == 1
        assert session_1_messages[0]["content"] == "Session 1 message"

        # Verify session 2 has one message left
        session_2_messages = await session_2.get_messages()
        assert len(session_2_messages) == 1
        assert session_2_messages[0]["content"] == "Session 2 message 1"

        session_1.close()
        session_2.close()


# Original non-parametrized tests for backwards compatibility
@pytest.mark.asyncio
async def test_session_memory_basic_functionality():
    """Test basic session memory functionality with SQLite backend."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"
        session_id = "test_session_123"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # First turn
        model.set_next_output([get_text_message("San Francisco")])
        result1 = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        assert result1.final_output == "San Francisco"

        # Second turn - should have conversation history
        model.set_next_output([get_text_message("California")])
        result2 = await Runner.run(agent, "What state is it in?", session=session)
        assert result2.final_output == "California"

        # Verify that the input to the second turn includes the previous conversation
        # The model should have received the full conversation history
        last_input = model.last_turn_args["input"]
        assert len(last_input) > 1  # Should have more than just the current message

        session.close()


@pytest.mark.asyncio
async def test_session_memory_with_explicit_instance():
    """Test session memory with an explicit SQLiteSession instance."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"
        session_id = "test_session_456"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # First turn
        model.set_next_output([get_text_message("Hello")])
        result1 = await Runner.run(agent, "Hi there", session=session)
        assert result1.final_output == "Hello"

        # Second turn
        model.set_next_output([get_text_message("I remember you said hi")])
        result2 = await Runner.run(
            agent, "Do you remember what I said?", session=session
        )
        assert result2.final_output == "I remember you said hi"

        session.close()


@pytest.mark.asyncio
async def test_session_memory_disabled():
    """Test that session memory is disabled when session=None."""
    model = FakeModel()
    agent = Agent(name="test", model=model)

    # First turn (no session parameters = disabled)
    model.set_next_output([get_text_message("Hello")])
    result1 = await Runner.run(agent, "Hi there")
    assert result1.final_output == "Hello"

    # Second turn - should NOT have conversation history
    model.set_next_output([get_text_message("I don't remember")])
    result2 = await Runner.run(agent, "Do you remember what I said?")
    assert result2.final_output == "I don't remember"

    # Verify that the input to the second turn is just the current message
    last_input = model.last_turn_args["input"]
    assert len(last_input) == 1  # Should only have the current message


@pytest.mark.asyncio
async def test_session_memory_different_sessions():
    """Test that different session IDs maintain separate conversation histories."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Session 1
        session_id_1 = "session_1"
        session_1 = SQLiteSession(session_id_1, db_path)

        model.set_next_output([get_text_message("I like cats")])
        result1 = await Runner.run(agent, "I like cats", session=session_1)
        assert result1.final_output == "I like cats"

        # Session 2 - different session
        session_id_2 = "session_2"
        session_2 = SQLiteSession(session_id_2, db_path)

        model.set_next_output([get_text_message("I like dogs")])
        result2 = await Runner.run(agent, "I like dogs", session=session_2)
        assert result2.final_output == "I like dogs"

        # Back to Session 1 - should remember cats, not dogs
        model.set_next_output([get_text_message("Yes, you mentioned cats")])
        result3 = await Runner.run(agent, "What did I say I like?", session=session_1)
        assert result3.final_output == "Yes, you mentioned cats"

        session_1.close()
        session_2.close()

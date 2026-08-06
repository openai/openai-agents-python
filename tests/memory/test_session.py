"""Tests for session memory functionality."""

import asyncio
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from agents import Agent, RunConfig, Runner, SessionSettings, SQLiteSession, TResponseInputItem
from agents.memory.sqlite_session import _await_mutation
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message


@pytest.mark.asyncio
async def test_await_mutation_cancellation_hides_later_failure_without_loop_error() -> None:
    """A failed mutation must not leak a false loop error after caller cancellation."""
    mutation_started = asyncio.Event()
    allow_failure = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, Any]] = []

    async def mutation() -> None:
        mutation_started.set()
        await allow_failure.wait()
        raise RuntimeError("mutation failed")

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    task = asyncio.create_task(_await_mutation(mutation()))
    try:
        await mutation_started.wait()
        task.cancel("caller-cancelled")
        allow_failure.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_exception_handler)
        allow_failure.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
async def test_sqlite_session_close_closes_worker_thread_connections():
    """Test that close cleans up connections opened by async worker threads."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_worker_thread_close.db"
        session = SQLiteSession("worker_thread_close", db_path)

        await session.add_items([{"role": "user", "content": "Hello"}])
        connections = list(session._connections)

        assert connections

        session.close()

        assert session._connections == set()
        with pytest.raises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")


@pytest.mark.asyncio
async def test_sqlite_session_closed_rejects_empty_add_items():
    """add_items([]) must not bypass the closed check through the empty-list fast path."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "closed_empty_add.db"
        session = SQLiteSession("closed_empty_add_test", db_path)
        session.close()

        with pytest.raises(RuntimeError, match="SQLiteSession is closed"):
            await session.add_items([])


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
async def test_sqlite_session_pop_item_skips_corrupt_most_recent():
    """pop_item skips corrupt newest rows and returns the next valid item."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_pop_corrupt.db"
        session = SQLiteSession("pop_corrupt", db_path)

        valid_item: TResponseInputItem = {"role": "user", "content": "valid"}
        await session.add_items([valid_item])

        with session._locked_connection() as conn:
            conn.execute(
                f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
                (session.session_id, "not valid json {{{"),
            )
            conn.commit()

        assert await session.pop_item() == valid_item
        assert await session.get_items() == []

        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_pop_item_returns_none_after_dropping_only_corrupt_rows():
    """pop_item removes corrupt rows and returns None when no valid items remain."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_pop_only_corrupt.db"
        session = SQLiteSession("pop_only_corrupt", db_path)

        with session._locked_connection() as conn:
            conn.execute(
                f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
                (session.session_id, "not valid json {{{"),
            )
            conn.commit()

        assert await session.pop_item() is None
        assert await session.get_items() == []

        session.close()


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


@pytest.mark.asyncio
async def test_sqlite_session_get_items_limit_skips_corrupt_newest_rows():
    """limit counts valid items, expanding past corrupt newest rows."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_limit_corrupt.db"
        session = SQLiteSession("limit_corrupt", db_path)

        await session.add_items(
            [
                {"role": "user", "content": "valid 0"},
                {"role": "assistant", "content": "valid 1"},
                {"role": "user", "content": "valid 2"},
            ]
        )

        with session._locked_connection() as conn:
            conn.execute(
                f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
                (session.session_id, "not valid json {{{"),
            )
            conn.commit()

        # Newest row is corrupt; limit=2 should still return the two latest valid items.
        limited = await session.get_items(limit=2)
        assert [item.get("content") for item in limited] == ["valid 1", "valid 2"]

        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_get_items_session_settings_limit_skips_corrupt_rows():
    """session_settings.limit also counts valid items when newest rows are corrupt."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_settings_limit_corrupt.db"
        session = SQLiteSession(
            "settings_limit_corrupt",
            db_path,
            session_settings=SessionSettings(limit=2),
        )

        await session.add_items(
            [
                {"role": "user", "content": "valid 0"},
                {"role": "assistant", "content": "valid 1"},
                {"role": "user", "content": "valid 2"},
            ]
        )

        with session._locked_connection() as conn:
            conn.execute(
                f"INSERT INTO {session.messages_table} (session_id, message_data) VALUES (?, ?)",
                (session.session_id, "not valid json {{{"),
            )
            conn.commit()

        limited = await session.get_items()
        assert [item.get("content") for item in limited] == ["valid 1", "valid 2"]

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_memory_appends_list_input_by_default(runner_method):
    """Test that list inputs are appended to session history when no callback is provided."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_validation.db"
        session_id = "test_validation_parametrized"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        initial_history: list[TResponseInputItem] = [
            {"role": "user", "content": "Earlier message"},
            {"role": "assistant", "content": "Saved reply"},
        ]
        await session.add_items(initial_history)

        list_input = [{"role": "user", "content": "Test message"}]

        model.set_next_output([get_text_message("This should run")])
        await run_agent_async(runner_method, agent, list_input, session=session)

        assert model.last_turn_args["input"] == initial_history + list_input

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_callback_prepared_input(runner_method):
    """Test if the user passes a list of items and want to append them."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Session
        session_id = "session_1"
        session = SQLiteSession(session_id, db_path)

        # Add first messages manually
        initial_history: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello there."},
            {"role": "assistant", "content": "Hi, I'm here to assist you."},
        ]
        try:
            await session.add_items(initial_history)

            def filter_assistant_messages(history, new_input):
                # Only include user messages from history
                return [item for item in history if item["role"] == "user"] + new_input

            new_turn_input = [{"role": "user", "content": "What your name?"}]
            model.set_next_output([get_text_message("I'm gpt-4o")])

            # Run the agent with the callable
            await run_agent_async(
                runner_method,
                agent,
                new_turn_input,
                session=session,
                run_config=RunConfig(session_input_callback=filter_assistant_messages),
            )

            expected_model_input = [
                initial_history[0],  # From history
                new_turn_input[0],  # New input
            ]

            assert len(model.last_turn_args["input"]) == 2
            assert model.last_turn_args["input"] == expected_model_input
        finally:
            session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_callback_repeating_history_does_not_grow_session(runner_method):
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_memory.db"
        model = FakeModel()
        agent = Agent(name="test", model=model)
        session = SQLiteSession("session_repeat", db_path)

        def repeat_first(history, new_input):
            if not history:
                return new_input
            return history + [history[0]] + new_input

        try:
            for turn in range(3):
                model.set_next_output([get_text_message(f"assistant {turn}")])
                await run_agent_async(
                    runner_method,
                    agent,
                    f"user {turn}",
                    session=session,
                    run_config=RunConfig(session_input_callback=repeat_first),
                )

            stored = await session.get_items()
            user_messages = [item for item in stored if item.get("role") == "user"]
            assert [item.get("content") for item in user_messages] == [
                "user 0",
                "user 1",
                "user 2",
            ]
            assert len(stored) == 6
        finally:
            session.close()


@pytest.mark.asyncio
async def test_sqlite_session_unicode_content():
    """Test that session correctly stores and retrieves unicode/non-ASCII content."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_unicode.db"
        session_id = "unicode_test"
        session = SQLiteSession(session_id, db_path)

        # Add unicode content to the session
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "😊👍"},
            {"role": "user", "content": "Привет"},
        ]
        await session.add_items(items)

        # Retrieve items and verify unicode content
        retrieved = await session.get_items()
        assert retrieved[0].get("content") == "こんにちは"
        assert retrieved[1].get("content") == "😊👍"
        assert retrieved[2].get("content") == "Привет"
        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_special_characters_and_sql_injection():
    """
    Test that session safely stores and retrieves items with special characters and SQL keywords.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_special_chars.db"
        session_id = "special_chars_test"
        session = SQLiteSession(session_id, db_path)

        # Add items with special characters and SQL keywords
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "O'Reilly"},
            {"role": "assistant", "content": "DROP TABLE sessions;"},
            {"role": "user", "content": ('"SELECT * FROM users WHERE name = "admin";"')},
            {"role": "assistant", "content": "Robert'); DROP TABLE students;--"},
            {"role": "user", "content": "Normal message"},
        ]
        await session.add_items(items)

        # Retrieve all items and verify they are stored correctly
        retrieved = await session.get_items()
        assert len(retrieved) == len(items)
        assert retrieved[0].get("content") == "O'Reilly"
        assert retrieved[1].get("content") == "DROP TABLE sessions;"
        assert retrieved[2].get("content") == '"SELECT * FROM users WHERE name = "admin";"'
        assert retrieved[3].get("content") == "Robert'); DROP TABLE students;--"
        assert retrieved[4].get("content") == "Normal message"
        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_concurrent_access():
    """
    Test concurrent access to the same session to verify data integrity.
    """
    import concurrent.futures

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_concurrent.db"
        session_id = "concurrent_test"
        session = SQLiteSession(session_id, db_path)

        # Add initial item
        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(10)
        ]

        # Use ThreadPoolExecutor to simulate concurrent writes
        def add_item(item):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(session.add_items([item]))
            loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            executor.map(add_item, items)

        # Retrieve all items and verify all are present
        retrieved = await session.get_items()
        contents = {
            content
            for item in retrieved
            for content in [item.get("content")]
            if isinstance(content, str)
        }
        expected = {f"Message {i}" for i in range(10)}
        assert contents == expected
        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_file_lock_is_shared_across_instances():
    """File-backed sessions pointing at the same DB path should reuse one process-local lock."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_shared_lock.db"
        lock_path = db_path.resolve()

        session_1 = SQLiteSession("session_1", db_path)
        session_2 = SQLiteSession("session_2", db_path)

        assert session_1._lock is session_2._lock
        assert SQLiteSession._file_lock_counts[lock_path] == 2

        await asyncio.gather(
            session_1.add_items([{"role": "user", "content": "session_1"}]),
            session_2.add_items([{"role": "user", "content": "session_2"}]),
        )

        assert [item.get("content") for item in await session_1.get_items()] == ["session_1"]
        assert [item.get("content") for item in await session_2.get_items()] == ["session_2"]

        session_1.close()
        assert SQLiteSession._file_lock_counts[lock_path] == 1
        assert lock_path in SQLiteSession._file_locks

        session_2.close()
        assert lock_path not in SQLiteSession._file_lock_counts
        assert lock_path not in SQLiteSession._file_locks


@pytest.mark.asyncio
async def test_sqlite_session_failed_add_items_releases_write_lock():
    """A failed add_items must not leave an open write transaction on the cached connection."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_rollback.db"
        session = SQLiteSession("rollback_test", db_path)

        # json.dumps() fails only after _insert_items() has already opened a write
        # transaction with the sessions-table upsert.
        unserializable = cast(TResponseInputItem, {"role": "user", "content": object()})
        with pytest.raises(TypeError):
            await session.add_items([unserializable])

        # timeout=0 disables the busy handler, so this raises immediately if the failed
        # write is still holding the SQLite write lock.
        probe = sqlite3.connect(str(db_path), timeout=0)
        try:
            probe.execute("INSERT INTO agent_sessions (session_id) VALUES ('probe')")
            probe.commit()
            rolled_back = probe.execute(
                "SELECT COUNT(*) FROM agent_sessions WHERE session_id = 'rollback_test'"
            ).fetchone()[0]
        finally:
            probe.close()

        assert rolled_back == 0

        # The session must remain usable after the failure.
        await session.add_items([{"role": "user", "content": "after failure"}])
        assert [item.get("content") for item in await session.get_items()] == ["after failure"]

        session.close()


@pytest.mark.asyncio
async def test_session_add_items_exception_propagates_in_streamed():
    """Test that exceptions from session.add_items are properly propagated
    in run_streamed instead of causing the stream to hang forever.
    Regression test for https://github.com/openai/openai-agents-python/issues/2130
    """
    session = SQLiteSession("test_exception_session")

    async def _failing_add_items(_items):
        raise RuntimeError("Simulated session.add_items failure")

    session.add_items = _failing_add_items  # type: ignore[method-assign]

    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.set_next_output([get_text_message("This should not be reached")])

    result = Runner.run_streamed(agent, "Hello", session=session)

    async def consume_stream():
        async for _event in result.stream_events():
            pass

    with pytest.raises(RuntimeError, match="Simulated session.add_items failure"):
        # Timeout ensures test fails fast instead of hanging forever if bug regresses
        await asyncio.wait_for(consume_stream(), timeout=5.0)

    session.close()


# ============================================================================
# SessionSettings Tests
# ============================================================================


@pytest.mark.asyncio
async def test_session_settings_default():
    """Test that session_settings defaults to empty SessionSettings."""
    from agents.memory import SessionSettings

    session = SQLiteSession("default_settings_test")

    # Should have default SessionSettings
    assert isinstance(session.session_settings, SessionSettings)
    assert session.session_settings.limit is None

    session.close()


@pytest.mark.asyncio
async def test_session_settings_constructor():
    """Test passing session_settings via constructor."""
    from agents.memory import SessionSettings

    session = SQLiteSession("constructor_settings_test", session_settings=SessionSettings(limit=5))

    assert session.session_settings is not None
    assert session.session_settings.limit == 5

    session.close()


@pytest.mark.asyncio
async def test_session_settings_constructor_normalizes_dictionary() -> None:
    session = SQLiteSession("dictionary_settings_test", session_settings={"limit": 0})

    assert isinstance(session.session_settings, SessionSettings)
    assert session.session_settings.limit == 0
    assert session.session_settings.resolve({"limit": 4}).limit == 4

    session.close()


def test_session_settings_rejects_unknown_dictionary_fields() -> None:
    with pytest.raises(TypeError, match="Unknown session settings: limitt"):
        SQLiteSession("invalid_settings_test", session_settings={"limitt": 1})


@pytest.mark.asyncio
async def test_get_items_uses_session_settings_limit():
    """Test that get_items uses session_settings.limit as default."""
    from agents.memory import SessionSettings

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_settings_limit.db"
        session = SQLiteSession(
            "uses_settings_limit_test", db_path, session_settings=SessionSettings(limit=3)
        )

        # Add 5 items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(5)
        ]
        await session.add_items(items)

        # get_items() with no limit should use session_settings.limit=3
        retrieved = await session.get_items()
        assert len(retrieved) == 3
        # Should get the last 3 items
        assert retrieved[0].get("content") == "Message 2"
        assert retrieved[1].get("content") == "Message 3"
        assert retrieved[2].get("content") == "Message 4"

        session.close()


@pytest.mark.asyncio
async def test_get_items_explicit_limit_overrides_session_settings():
    """Test that explicit limit parameter overrides session_settings."""
    from agents.memory import SessionSettings

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_override.db"
        session = SQLiteSession(
            "explicit_override_test", db_path, session_settings=SessionSettings(limit=5)
        )

        # Add 10 items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(10)
        ]
        await session.add_items(items)

        # Explicit limit=2 should override session_settings.limit=5
        retrieved = await session.get_items(limit=2)
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Message 8"
        assert retrieved[1].get("content") == "Message 9"

        session.close()


@pytest.mark.asyncio
async def test_session_settings_resolve():
    """Test SessionSettings.resolve() method."""
    from agents.memory import SessionSettings

    base = SessionSettings(limit=100)
    override = SessionSettings(limit=50)

    final = base.resolve(override)

    assert final.limit == 50  # Override wins
    assert base.limit == 100  # Original unchanged

    # Resolving with None returns self
    final_none = base.resolve(None)
    assert final_none.limit == 100


@pytest.mark.asyncio
async def test_runner_with_session_settings_override():
    """Test that RunConfig can override session's default settings."""
    from agents.memory import SessionSettings

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_runner_override.db"

        # Session with default limit=100
        session = SQLiteSession(
            "runner_override_test", db_path, session_settings=SessionSettings(limit=100)
        )

        # Add some history
        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Turn {i}"} for i in range(10)
        ]
        await session.add_items(items)

        model = FakeModel()
        agent = Agent(name="test", model=model)
        model.set_next_output([get_text_message("Got it")])

        await Runner.run(
            agent,
            "New question",
            session=session,
            run_config=RunConfig(
                session_settings=SessionSettings(limit=2)  # Override to 2
            ),
        )

        # Verify the agent received only the last 2 history items + new question
        last_input = model.last_turn_args["input"]
        # Filter out the new "New question" input
        history_items = [item for item in last_input if item.get("content") != "New question"]
        # Should have 2 history items (last two from the 10 we added)
        assert len(history_items) == 2

        session.close()


def _drop_sqlite_table(db_path: Path, table: str) -> None:
    """Drop a table from an independent connection to make a later statement fail."""
    helper = sqlite3.connect(str(db_path))
    try:
        helper.execute(f"DROP TABLE {table}")
        helper.commit()
    finally:
        helper.close()


def _sqlite_write_lock_is_free(db_path: Path) -> bool:
    """Return whether an independent writer can take the SQLite write lock."""
    probe = sqlite3.connect(str(db_path), timeout=0)
    try:
        probe.execute("CREATE TABLE IF NOT EXISTS probe_lock (x INTEGER)")
        probe.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


@pytest.mark.asyncio
async def test_sqlite_session_failed_clear_session_rolls_back():
    """A failed clear must restore earlier statements and release the cached write lock."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "clear_rollback.db"
        session = SQLiteSession("clear_rollback", db_path)
        await session.add_items([{"role": "user", "content": "kept"}])

        _drop_sqlite_table(db_path, "agent_sessions")

        with pytest.raises(sqlite3.OperationalError):
            await session.clear_session()

        assert all(not conn.in_transaction for conn in session._connections)
        assert _sqlite_write_lock_is_free(db_path)
        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_failed_pop_item_releases_write_lock():
    """A failed pop must not leave a write transaction on the cached connection."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "pop_rollback.db"
        session = SQLiteSession("pop_rollback", db_path)
        await session.add_items([{"role": "user", "content": "kept"}])

        _drop_sqlite_table(db_path, "agent_messages")

        with pytest.raises(sqlite3.OperationalError):
            await session.pop_item()

        assert all(not conn.in_transaction for conn in session._connections)
        assert _sqlite_write_lock_is_free(db_path)
        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_rollback_failure_evicts_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    """A file connection that cannot roll back must be closed and replaced."""

    class FailingRollbackConnection(sqlite3.Connection):
        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "rollback_failure.db"
        session = SQLiteSession("rollback_failure", db_path)
        conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            factory=FailingRollbackConnection,
        )
        with session._connections_lock:
            session._connections.add(conn)
        real_get_connection = session._get_connection
        monkeypatch.setattr(session, "_get_connection", lambda: conn)
        unserializable = cast(TResponseInputItem, {"role": "user", "content": object()})

        with pytest.raises(TypeError):
            await session.add_items([unserializable])

        assert conn not in session._connections
        assert _sqlite_write_lock_is_free(db_path)

        monkeypatch.setattr(session, "_get_connection", real_get_connection)
        await session.add_items([{"role": "user", "content": "after failure"}])
        assert [item.get("content") for item in await session.get_items()] == ["after failure"]
        session.close()


@pytest.mark.asyncio
async def test_sqlite_session_close_retries_quarantined_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    """A failed invalidation close must remain owned until a later close succeeds."""

    class FailingRollbackAndCloseConnection(sqlite3.Connection):
        fail_close = True

        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            if self.fail_close:
                raise RuntimeError("close failed")
            super().close()

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "close_retry.db"
        session = SQLiteSession("close_retry", db_path)
        conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            factory=FailingRollbackAndCloseConnection,
        )
        with session._connections_lock:
            session._connections.add(conn)
        monkeypatch.setattr(session, "_get_connection", lambda: conn)
        unserializable = cast(TResponseInputItem, {"role": "user", "content": object()})

        with pytest.raises(TypeError):
            await session.add_items([unserializable])

        assert session._closed is True
        assert conn in session._quarantined_connections
        assert _sqlite_write_lock_is_free(db_path) is False

        conn.fail_close = False
        session.close()

        assert session._quarantined_connections == set()
        assert _sqlite_write_lock_is_free(db_path)
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "pop", "clear"])
async def test_sqlite_session_post_commit_cancellation_propagates_after_known_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    """Cancellation after a worker commit must propagate without inviting a retry."""

    class PausingCommitConnection(sqlite3.Connection):
        pause_commit = False
        commit_finished = threading.Event()
        allow_return = threading.Event()

        def commit(self) -> None:
            super().commit()
            if self.pause_commit:
                self.pause_commit = False
                self.commit_finished.set()
                assert self.allow_return.wait(timeout=10)

    db_path = tmp_path / f"post_commit_{operation}.db"
    session = SQLiteSession(f"post_commit_{operation}", db_path)
    item: TResponseInputItem = {"role": "user", "content": "once"}
    if operation != "add":
        await session.add_items([item])

    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        factory=PausingCommitConnection,
    )
    with session._connections_lock:
        session._connections.add(conn)
    monkeypatch.setattr(session, "_get_connection", lambda: conn)
    conn.pause_commit = True

    if operation == "add":
        mutation: asyncio.Task[Any] = asyncio.create_task(session.add_items([item]))
    elif operation == "pop":
        mutation = asyncio.create_task(session.pop_item())
    else:
        mutation = asyncio.create_task(session.clear_session())

    try:
        assert await asyncio.to_thread(conn.commit_finished.wait, 10)
        mutation.cancel()
        await asyncio.sleep(0)
        mutation.cancel()
        await asyncio.sleep(0)
        conn.allow_return.set()
        with pytest.raises(asyncio.CancelledError):
            await mutation
    finally:
        conn.allow_return.set()
        if not mutation.done():
            mutation.cancel()
            await asyncio.gather(mutation, return_exceptions=True)

    if operation == "add":
        assert await session.get_items() == [item]
    elif operation == "pop":
        assert await session.get_items() == []
    else:
        assert await session.get_items() == []
    assert mutation.cancelled()
    session.close()

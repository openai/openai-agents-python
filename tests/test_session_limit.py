"""Test session_limit parameter functionality via SessionSettings."""

import tempfile
from pathlib import Path

import pytest

from agents import Agent, RunConfig, SQLiteSession
from agents.memory import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message
from tests.test_session import run_agent_async


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_limit_parameter(runner_method):
    """Test that session_limit parameter correctly limits conversation history
    retrieved from session across all Runner methods."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_limit.db"
        session_id = "limit_test"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Build up a longer conversation history
        model.set_next_output([get_text_message("Reply 1")])
        await run_agent_async(runner_method, agent, "Message 1", session=session)

        model.set_next_output([get_text_message("Reply 2")])
        await run_agent_async(runner_method, agent, "Message 2", session=session)

        model.set_next_output([get_text_message("Reply 3")])
        await run_agent_async(runner_method, agent, "Message 3", session=session)

        # Verify we have 6 items in total (3 user + 3 assistant)
        all_items = await session.get_items()
        assert len(all_items) == 6

        # Test session_limit via RunConfig - should only get last 2 history items + new input
        model.set_next_output([get_text_message("Reply 4")])
        await run_agent_async(
            runner_method,
            agent,
            "Message 4",
            session=session,
            run_config=RunConfig(session_settings=SessionSettings(limit=2)),
        )

        # Verify model received limited history
        last_input = model.last_turn_args["input"]
        # Should have: 2 history items + 1 new message = 3 total
        assert len(last_input) == 3
        # First item should be "Message 3" (not Message 1 or 2)
        assert last_input[0].get("content") == "Message 3"
        # Assistant message has content as a list
        assert last_input[1].get("content")[0]["text"] == "Reply 3"
        assert last_input[2].get("content") == "Message 4"

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_limit_zero(runner_method):
    """Test that session_limit=0 provides no history, only new message."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_limit_zero.db"
        session_id = "limit_zero_test"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Build conversation history
        model.set_next_output([get_text_message("Reply 1")])
        await run_agent_async(runner_method, agent, "Message 1", session=session)

        model.set_next_output([get_text_message("Reply 2")])
        await run_agent_async(runner_method, agent, "Message 2", session=session)

        # Test with limit=0 - should get NO history, just new message
        model.set_next_output([get_text_message("Reply 3")])
        await run_agent_async(
            runner_method,
            agent,
            "Message 3",
            session=session,
            run_config=RunConfig(session_settings=SessionSettings(limit=0)),
        )

        # Verify model received only the new message
        last_input = model.last_turn_args["input"]
        assert len(last_input) == 1
        assert last_input[0].get("content") == "Message 3"

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_limit_none_gets_all_history(runner_method):
    """Test that session_limit=None retrieves entire history (default behavior)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_limit_none.db"
        session_id = "limit_none_test"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Build longer conversation
        for i in range(1, 6):
            model.set_next_output([get_text_message(f"Reply {i}")])
            await run_agent_async(runner_method, agent, f"Message {i}", session=session)

        # Verify 10 items in session (5 user + 5 assistant)
        all_items = await session.get_items()
        assert len(all_items) == 10

        # Test with session_limit=None (default) - should get all history
        model.set_next_output([get_text_message("Reply 6")])
        await run_agent_async(
            runner_method,
            agent,
            "Message 6",
            session=session,
            run_config=RunConfig(session_settings=SessionSettings(limit=None)),
        )

        # Verify model received all history + new message
        last_input = model.last_turn_args["input"]
        assert len(last_input) == 11  # 10 history + 1 new
        assert last_input[0].get("content") == "Message 1"
        assert last_input[-1].get("content") == "Message 6"

        session.close()


@pytest.mark.parametrize("runner_method", ["run", "run_sync", "run_streamed"])
@pytest.mark.asyncio
async def test_session_limit_larger_than_history(runner_method):
    """Test that session_limit larger than history size returns all items."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_limit_large.db"
        session_id = "limit_large_test"
        session = SQLiteSession(session_id, db_path)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # Build small conversation
        model.set_next_output([get_text_message("Reply 1")])
        await run_agent_async(runner_method, agent, "Message 1", session=session)

        # Test with limit=100 (much larger than actual history)
        model.set_next_output([get_text_message("Reply 2")])
        await run_agent_async(
            runner_method,
            agent,
            "Message 2",
            session=session,
            run_config=RunConfig(session_settings=SessionSettings(limit=100)),
        )

        # Verify model received all available history + new message
        last_input = model.last_turn_args["input"]
        assert len(last_input) == 3  # 2 history + 1 new
        assert last_input[0].get("content") == "Message 1"
        # Assistant message has content as a list
        assert last_input[1].get("content")[0]["text"] == "Reply 1"
        assert last_input[2].get("content") == "Message 2"

        session.close()

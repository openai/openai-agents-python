from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")  # Skip tests if SQLAlchemy is not installed

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

# Use in-memory SQLite for tests
DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


async def test_sqlalchemy_session_direct_ops(agent: Agent):
    """Test direct database operations of SQLAlchemySession."""
    session_id = "direct_ops_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    # 1. Add items
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    # 2. Get items and verify
    retrieved = await session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"

    # 3. Pop item
    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "Hi there!"
    retrieved_after_pop = await session.get_items()
    assert len(retrieved_after_pop) == 1
    assert retrieved_after_pop[0].get("content") == "Hello"

    # 4. Clear session
    await session.clear_session()
    retrieved_after_clear = await session.get_items()
    assert len(retrieved_after_clear) == 0


async def test_runner_integration(agent: Agent):
    """Test that SQLAlchemySession works correctly with the agent Runner."""
    session_id = "runner_integration_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    # First turn
    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("San Francisco")])
    result1 = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    assert result1.final_output == "San Francisco"

    # Second turn
    agent.model.set_next_output([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"

    # Verify history was passed to the model on the second turn
    last_input = agent.model.last_turn_args["input"]
    assert len(last_input) > 1
    assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)


async def test_session_isolation(agent: Agent):
    """Test that different session IDs result in isolated conversation histories."""
    session_id_1 = "session_1"
    session1 = SQLAlchemySession.from_url(session_id_1, url=DB_URL, create_tables=True)

    session_id_2 = "session_2"
    session2 = SQLAlchemySession.from_url(session_id_2, url=DB_URL, create_tables=True)

    # Interact with session 1
    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("I like cats.")])
    await Runner.run(agent, "I like cats.", session=session1)

    # Interact with session 2
    agent.model.set_next_output([get_text_message("I like dogs.")])
    await Runner.run(agent, "I like dogs.", session=session2)

    # Go back to session 1 and check its memory
    agent.model.set_next_output([get_text_message("You said you like cats.")])
    result = await Runner.run(agent, "What animal did I say I like?", session=session1)
    assert "cats" in result.final_output.lower()
    assert "dogs" not in result.final_output.lower()


async def test_get_items_with_limit(agent: Agent):
    """Test the limit parameter in get_items."""
    session_id = "limit_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
        {"role": "assistant", "content": "4"},
    ]
    await session.add_items(items)

    # Get last 2 items
    latest_2 = await session.get_items(limit=2)
    assert len(latest_2) == 2
    assert latest_2[0].get("content") == "3"
    assert latest_2[1].get("content") == "4"

    # Get all items
    all_items = await session.get_items()
    assert len(all_items) == 4

    # Get more than available
    more_than_all = await session.get_items(limit=10)
    assert len(more_than_all) == 4


async def test_pop_from_empty_session():
    """Test that pop_item returns None on an empty session."""
    session = SQLAlchemySession.from_url("empty_session", url=DB_URL, create_tables=True)
    popped = await session.pop_item()
    assert popped is None


async def test_add_empty_items_list():
    """Test that adding an empty list of items is a no-op."""
    session_id = "add_empty_test"
    session = SQLAlchemySession.from_url(session_id, url=DB_URL, create_tables=True)

    initial_items = await session.get_items()
    assert len(initial_items) == 0

    await session.add_items([])

    items_after_add = await session.get_items()
    assert len(items_after_add) == 0

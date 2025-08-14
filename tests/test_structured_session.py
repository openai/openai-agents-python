"""Tests for structured session storage functionality."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agents import Agent, Runner, SQLiteSession, function_tool
from agents.items import TResponseInputItem

from .fake_model import FakeModel
from .test_responses import get_text_message


@pytest.mark.asyncio
async def test_structured_session_creation():
    """Test that structured session creates the additional tables."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_structured.db"
        session = SQLiteSession("test_session", db_path, structured=True)

        # Check that the structured tables were created
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        expected_tables = [
            "agent_conversation_messages",
            "agent_messages",
            "agent_sessions",
            "agent_tool_calls",
        ]
        for table in expected_tables:
            assert table in tables

        session.close()


@pytest.mark.asyncio
async def test_structured_session_disabled_by_default():
    """Test that structured tables are not created when structured=False."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_flat.db"
        session = SQLiteSession("test_session", db_path, structured=False)

        # Check that only the basic tables were created
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        expected_tables = ["agent_messages", "agent_sessions"]
        for table in expected_tables:
            assert table in tables

        # Structured tables should not exist
        assert "agent_conversation_messages" not in tables
        assert "agent_tool_calls" not in tables

        session.close()


@pytest.mark.asyncio
async def test_structured_session_conversation_flow():
    """Test a full conversation flow with structured storage."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_conversation.db"
        session = SQLiteSession("test_session", db_path, structured=True)

        # Create a simple tool for testing
        @function_tool
        def get_test_number(max_val: int = 100) -> int:
            """Get a test number."""
            return 42

        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[get_test_number])

        # Simulate a simple message without tool calls for this test
        model.set_next_output([get_text_message("I'll pick a random number: 42")])

        await Runner.run(
            agent,
            "Pick a random number",
            session=session
        )

        # Check that data was stored in structured tables
        conn = sqlite3.connect(str(db_path))

        # Check conversation messages table
        cursor = conn.execute(
            """SELECT role, content FROM agent_conversation_messages
               WHERE session_id = ? ORDER BY created_at""",
            ("test_session",)
        )
        conversation_rows = cursor.fetchall()

        # Should have user message and potentially assistant message
        assert len(conversation_rows) >= 1
        assert conversation_rows[0][0] == "user"  # First should be user role
        assert "Pick a random number" in conversation_rows[0][1]

        # Check tool calls table (should be empty for this simple message test)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_tool_calls WHERE session_id = ?",
            ("test_session",)
        )
        tool_call_count = cursor.fetchone()[0]
        assert tool_call_count == 0  # No tool calls in this simple test

        conn.close()
        session.close()


@pytest.mark.asyncio
async def test_structured_session_backward_compatibility():
    """Test that structured=True doesn't break existing functionality."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_compat.db"
        session = SQLiteSession("test_session", db_path, structured=True)

        model = FakeModel()
        agent = Agent(name="test", model=model)

        # First turn
        model.set_next_output([get_text_message("Hello!")])
        result1 = await Runner.run(agent, "Hi there", session=session)
        assert result1.final_output == "Hello!"

        # Second turn - should have conversation history
        model.set_next_output([get_text_message("I remember you said hi")])
        result2 = await Runner.run(agent, "What did I say?", session=session)
        assert result2.final_output == "I remember you said hi"

        # Verify conversation history is working
        items = await session.get_items()
        assert len(items) >= 2  # Should have multiple items from the conversation

        session.close()


@pytest.mark.asyncio
async def test_structured_session_pop_item():
    """Test that pop_item works correctly with structured storage."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_pop.db"
        session = SQLiteSession("test_session", db_path, structured=True)

        # Add some test items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        await session.add_items(items)

        # Pop the last item
        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("role") == "assistant"
        assert popped.get("content") == "Hi there!"

        # Check that structured tables are also cleaned up
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_conversation_messages WHERE session_id = ?",
            ("test_session",)
        )
        count = cursor.fetchone()[0]
        conn.close()

        # Should only have 1 message left (the user message)
        assert count == 1

        session.close()


@pytest.mark.asyncio
async def test_structured_session_clear():
    """Test that clear_session works correctly with structured storage."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_clear.db"
        session = SQLiteSession("test_session", db_path, structured=True)

        # Add some test items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "test_tool",
                "arguments": '{"param": "value"}',
                "status": "completed"
            }
        ]
        await session.add_items(items)

        # Clear the session
        await session.clear_session()

        # Check that all tables are empty
        conn = sqlite3.connect(str(db_path))

        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE session_id = ?",
            ("test_session",)
        )
        assert cursor.fetchone()[0] == 0

        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_conversation_messages WHERE session_id = ?",
            ("test_session",)
        )
        assert cursor.fetchone()[0] == 0

        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_tool_calls WHERE session_id = ?",
            ("test_session",)
        )
        assert cursor.fetchone()[0] == 0

        conn.close()
        session.close()


@pytest.mark.asyncio
async def test_flat_vs_structured_storage_equivalence():
    """Test that flat and structured storage produce equivalent get_items results."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path_flat = Path(temp_dir) / "test_flat.db"
        db_path_structured = Path(temp_dir) / "test_structured.db"

        session_flat = SQLiteSession("test_session", db_path_flat, structured=False)
        session_structured = SQLiteSession("test_session", db_path_structured, structured=True)

        # Add the same items to both sessions
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "test_tool",
                "arguments": '{"param": "value"}',
                "status": "completed"
            },
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "result"
            }
        ]

        await session_flat.add_items(items)
        await session_structured.add_items(items)

        # Get items from both sessions
        items_flat = await session_flat.get_items()
        items_structured = await session_structured.get_items()

        # Should be identical
        assert len(items_flat) == len(items_structured)
        assert items_flat == items_structured

        session_flat.close()
        session_structured.close()

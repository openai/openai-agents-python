import json
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from agents.extensions.memory import (
    PostgreSQLSession,
)
from agents.extensions.memory.postgres_session import MessageRow
from agents.items import TResponseInputItem


class AsyncContextManagerMock:
    """Helper class to mock async context managers."""

    def __init__(self, return_value):
        self.return_value = return_value

    async def __aenter__(self):
        return self.return_value

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        return None


class TestPostgreSQLSession(unittest.IsolatedAsyncioTestCase):
    """Test suite for PostgreSQLSession class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_pool = AsyncMock()

        # Make connection method return the async context manager directly, not a coroutine
        def mock_connection() -> AsyncContextManagerMock:
            return AsyncContextManagerMock(AsyncMock())

        self.mock_pool.connection = mock_connection
        self.session = PostgreSQLSession(
            session_id="test_session_123",
            pool=self.mock_pool,
            sessions_table="test_sessions",
            messages_table="test_messages",
        )

    def setup_connection_mock(self, mock_conn: AsyncMock, mock_cursor: AsyncMock) -> None:
        """Helper to set up connection and cursor mocks properly."""

        def mock_connection() -> AsyncContextManagerMock:
            return AsyncContextManagerMock(mock_conn)

        self.mock_pool.connection = mock_connection

        def mock_cursor_method(*_args: Any, **_kwargs: Any) -> AsyncContextManagerMock:
            return AsyncContextManagerMock(mock_cursor)

        mock_conn.cursor = mock_cursor_method

        def mock_transaction() -> AsyncContextManagerMock:
            return AsyncContextManagerMock(None)

        mock_conn.transaction = mock_transaction

    def test_init_with_defaults(self):
        """Test initialization with default table names."""
        mock_pool: AsyncConnectionPool[AsyncConnection[TupleRow]] = AsyncMock()
        session = PostgreSQLSession("test", mock_pool)
        self.assertEqual(session.session_id, "test")
        self.assertEqual(session.pool, mock_pool)
        self.assertEqual(session.sessions_table, "agent_sessions")
        self.assertEqual(session.messages_table, "agent_messages")
        self.assertFalse(session._initialized)

    async def test_ensure_initialized_once(self):
        """Test that database initialization happens only once."""

        async def mock_init_db():
            self.session._initialized = True

        with patch.object(self.session, "_init_db", side_effect=mock_init_db) as mock_init:
            await self.session._ensure_initialized()
            await self.session._ensure_initialized()

            # Should only be called once due to the _initialized flag
            mock_init.assert_called_once()

    async def test_init_db_creates_tables(self):
        """Test that database initialization creates necessary tables."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        await self.session._init_db()

        # Check that execute was called for sessions table, messages table, and index
        self.assertEqual(mock_cursor.execute.call_count, 3)

        # Verify sessions table creation
        sessions_call = mock_cursor.execute.call_args_list[0][0][0]
        sessions_call_str = str(sessions_call)
        self.assertIn("CREATE TABLE IF NOT EXISTS", sessions_call_str)
        self.assertIn("session_id TEXT PRIMARY KEY", sessions_call_str)

        # Verify messages table creation
        messages_call = mock_cursor.execute.call_args_list[1][0][0]
        messages_call_str = str(messages_call)
        self.assertIn("CREATE TABLE IF NOT EXISTS", messages_call_str)
        self.assertIn("message_data JSONB NOT NULL", messages_call_str)
        self.assertIn("FOREIGN KEY (session_id) REFERENCES", messages_call_str)

        # Verify index creation
        index_call = mock_cursor.execute.call_args_list[2][0][0]
        index_call_str = str(index_call)
        self.assertIn("CREATE INDEX IF NOT EXISTS", index_call_str)

        self.assertTrue(self.session._initialized)

    async def test_get_items_no_limit(self):
        """Test getting all items without limit."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        # Mock fetchall to return test data
        test_data = [
            MessageRow(message_data={"role": "user", "content": "Hello"}),
            MessageRow(message_data={"role": "assistant", "content": "Hi there"}),
        ]
        mock_cursor.fetchall.return_value = test_data

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            result = await self.session.get_items()

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {"role": "user", "content": "Hello"})
        self.assertEqual(result[1], {"role": "assistant", "content": "Hi there"})

        # Verify query was called correctly
        mock_cursor.execute.assert_called_once()
        query_call = mock_cursor.execute.call_args[0][0]
        query_call_str = str(query_call)
        self.assertIn("SELECT message_data FROM", query_call_str)
        self.assertIn("ORDER BY created_at ASC", query_call_str)
        self.assertNotIn("LIMIT", query_call_str)

    async def test_get_items_with_limit(self):
        """Test getting items with a limit."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        test_data = [MessageRow(message_data={"role": "user", "content": "Hello"})]
        mock_cursor.fetchall.return_value = test_data

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            result = await self.session.get_items(limit=5)

        self.assertEqual(len(result), 1)

        # Verify query includes limit and uses subquery
        query_call = mock_cursor.execute.call_args[0][0]
        query_call_str = str(query_call)
        self.assertIn("LIMIT %s", query_call_str)
        self.assertIn("ORDER BY created_at DESC", query_call_str)
        self.assertEqual(mock_cursor.execute.call_args[0][1], ("test_session_123", 5))

    async def test_get_items_handles_invalid_data(self):
        """Test that get_items handles invalid data gracefully."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        # Mix of valid and invalid data
        test_data = [
            MessageRow(message_data={"role": "user", "content": "Hello"}),
            None,  # This should be skipped due to AttributeError
            MessageRow(message_data={"role": "assistant", "content": "Hi"}),
        ]
        mock_cursor.fetchall.return_value = test_data

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            result = await self.session.get_items()

        # Should only return valid items
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {"role": "user", "content": "Hello"})
        self.assertEqual(result[1], {"role": "assistant", "content": "Hi"})

    async def test_add_items_empty_list(self):
        """Test adding empty list of items."""
        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock) as mock_init:
            await self.session.add_items([])
            mock_init.assert_not_called()

    async def test_add_items_success(self):
        """Test successfully adding items."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        test_items = cast(
            list[TResponseInputItem],
            [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi there"}],
        )

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            await self.session.add_items(test_items)

        # Verify session creation, item insertion, and timestamp update
        self.assertEqual(mock_cursor.execute.call_count, 2)  # session insert + timestamp update
        self.assertEqual(mock_cursor.executemany.call_count, 1)  # items insert

        # Check session insert
        session_call = mock_cursor.execute.call_args_list[0]
        session_call_str = str(session_call[0][0])
        self.assertIn("INSERT INTO", session_call_str)
        self.assertIn("ON CONFLICT (session_id) DO NOTHING", session_call_str)

        # Check items insert
        items_call = mock_cursor.executemany.call_args
        items_call_str = str(items_call[0][0])
        self.assertIn("INSERT INTO", items_call_str)
        expected_data = [
            ("test_session_123", json.dumps(test_items[0])),
            ("test_session_123", json.dumps(test_items[1])),
        ]
        self.assertEqual(items_call[0][1], expected_data)

        # Check timestamp update
        timestamp_call = mock_cursor.execute.call_args_list[1]
        timestamp_call_str = str(timestamp_call[0][0])
        self.assertIn("UPDATE", timestamp_call_str)
        self.assertIn("SET updated_at = CURRENT_TIMESTAMP", timestamp_call_str)

    async def test_pop_item_success(self):
        """Test successfully popping an item."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        test_item = {"role": "user", "content": "Hello", "type": "message"}
        mock_cursor.fetchone.return_value = MessageRow(
            message_data={"role": "user", "content": "Hello", "type": "message"}
        )

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            result = await self.session.pop_item()

        self.assertEqual(result, test_item)

        # Verify single DELETE ... RETURNING query
        self.assertEqual(mock_cursor.execute.call_count, 1)

        # Check delete query with RETURNING
        delete_call = mock_cursor.execute.call_args_list[0]
        delete_call_str = str(delete_call[0][0])
        self.assertIn("DELETE FROM", delete_call_str)
        self.assertIn("RETURNING message_data", delete_call_str)
        self.assertIn("ORDER BY created_at DESC", delete_call_str)
        self.assertIn("LIMIT 1", delete_call_str)
        self.assertEqual(delete_call[0][1], ("test_session_123",))

    async def test_pop_item_empty_session(self):
        """Test popping from an empty session."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        mock_cursor.fetchone.return_value = None

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            result = await self.session.pop_item()

        self.assertIsNone(result)

        # Should only call select, not delete
        self.assertEqual(mock_cursor.execute.call_count, 1)

    async def test_pop_item_handles_invalid_data(self):
        """Test that pop_item handles invalid data gracefully."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        # Invalid data structure - mock object without message_data attribute
        class InvalidRow:
            def __init__(self):
                self.id = 123
                # No message_data attribute

        mock_cursor.fetchone.return_value = InvalidRow()

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            result = await self.session.pop_item()

        self.assertIsNone(result)

        # Should execute the DELETE ... RETURNING query once
        self.assertEqual(mock_cursor.execute.call_count, 1)

    async def test_clear_session(self):
        """Test clearing session."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()

        # Set up context managers
        self.setup_connection_mock(mock_conn, mock_cursor)

        with patch.object(self.session, "_ensure_initialized", new_callable=AsyncMock):
            await self.session.clear_session()

        # Should delete from both messages and sessions tables
        self.assertEqual(mock_cursor.execute.call_count, 2)

        # Check messages deletion
        messages_call = mock_cursor.execute.call_args_list[0]
        messages_call_str = str(messages_call[0][0])
        self.assertIn("DELETE FROM", messages_call_str)
        self.assertIn("WHERE session_id = %s", messages_call_str)
        self.assertEqual(messages_call[0][1], ("test_session_123",))

        # Check sessions deletion
        sessions_call = mock_cursor.execute.call_args_list[1]
        sessions_call_str = str(sessions_call[0][0])
        self.assertIn("DELETE FROM", sessions_call_str)
        self.assertIn("WHERE session_id = %s", sessions_call_str)
        self.assertEqual(sessions_call[0][1], ("test_session_123",))

    async def test_close(self):
        """Test closing the session."""
        self.session._initialized = True

        await self.session.close()

        self.mock_pool.close.assert_called_once()
        self.assertFalse(self.session._initialized)

    @patch("agents.extensions.memory.postgres_session.AsyncConnectionPool")
    async def test_from_connection_string_success(self, mock_pool_class):
        """Test creating a session from connection string."""
        mock_pool = AsyncMock()
        mock_pool_class.return_value = mock_pool

        connection_string = "postgresql://user:pass@host/db"
        session_id = "test_session_123"

        session = await PostgreSQLSession.from_connection_string(session_id, connection_string)

        # Verify pool was created with the connection string
        mock_pool_class.assert_called_once_with(connection_string, open=False)
        mock_pool.open.assert_called_once()

        # Verify session was created with correct parameters
        self.assertEqual(session.session_id, session_id)
        self.assertEqual(session.pool, mock_pool)
        self.assertEqual(session.sessions_table, "agent_sessions")
        self.assertEqual(session.messages_table, "agent_messages")

    @patch("agents.extensions.memory.postgres_session.AsyncConnectionPool")
    async def test_from_connection_string_custom_tables(self, mock_pool_class):
        """Test creating a session from connection string with custom table names."""
        mock_pool = AsyncMock()
        mock_pool_class.return_value = mock_pool

        connection_string = "postgresql://user:pass@host/db"
        session_id = "test_session_123"
        custom_sessions_table = "custom_sessions"
        custom_messages_table = "custom_messages"

        session = await PostgreSQLSession.from_connection_string(
            session_id,
            connection_string,
            sessions_table=custom_sessions_table,
            messages_table=custom_messages_table,
        )

        # Verify pool was created with the connection string
        mock_pool_class.assert_called_once_with(connection_string, open=False)
        mock_pool.open.assert_called_once()

        # Verify session was created with correct parameters
        self.assertEqual(session.session_id, session_id)
        self.assertEqual(session.pool, mock_pool)
        self.assertEqual(session.sessions_table, custom_sessions_table)
        self.assertEqual(session.messages_table, custom_messages_table)


@pytest.mark.skip(reason="Integration tests require a running PostgreSQL instance")
class TestPostgreSQLSessionIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests for PostgreSQL session that require a running database."""

    # Test connection string - modify as needed for your test database
    TEST_CONNECTION_STRING = "postgresql://postgres:password@localhost:5432/test_db"

    async def asyncSetUp(self):
        """Set up test session."""
        self.session_id = "test_integration_session"
        self.session = await PostgreSQLSession.from_connection_string(
            self.session_id,
            self.TEST_CONNECTION_STRING,
            sessions_table="test_sessions",
            messages_table="test_messages",
        )

        # Clean up any existing test data
        await self.session.clear_session()

    async def asyncTearDown(self):
        """Clean up after tests."""
        if hasattr(self, "session"):
            await self.session.clear_session()
            await self.session.close()

    async def test_integration_full_workflow(self):
        """Test complete workflow: add items, get items, pop item, clear session."""
        # Initially empty
        items = await self.session.get_items()
        self.assertEqual(len(items), 0)

        # Add some test items
        test_items = cast(
            list[TResponseInputItem],
            [
                {"role": "user", "content": "Hello", "type": "message"},
                {"role": "assistant", "content": "Hi there!", "type": "message"},
                {"role": "user", "content": "How are you?", "type": "message"},
                {"role": "assistant", "content": "I'm doing well, thank you!", "type": "message"},
            ],
        )

        for item in test_items:
            await self.session.add_items([item])

        # Verify items were added
        stored_items = await self.session.get_items()
        self.assertEqual(len(stored_items), 4)
        self.assertEqual(stored_items[0], test_items[0])
        self.assertEqual(stored_items[-1], test_items[-1])

        # Test with limit
        limited_items = await self.session.get_items(limit=2)
        self.assertEqual(len(limited_items), 2)
        # Should get the last 2 items in chronological order
        self.assertEqual(limited_items[0], test_items[2])
        self.assertEqual(limited_items[1], test_items[3])

        # Test pop_item
        popped_item = await self.session.pop_item()
        self.assertEqual(popped_item, test_items[3])  # Last item

        # Verify item was removed
        remaining_items = await self.session.get_items()
        self.assertEqual(len(remaining_items), 3)
        self.assertEqual(remaining_items[-1], test_items[2])

        # Test clear_session
        await self.session.clear_session()
        final_items = await self.session.get_items()
        self.assertEqual(len(final_items), 0)

    async def test_integration_multiple_sessions(self):
        """Test that different sessions maintain separate data."""
        # Create a second session
        session2 = await PostgreSQLSession.from_connection_string(
            "test_integration_session_2",
            self.TEST_CONNECTION_STRING,
            sessions_table="test_sessions",
            messages_table="test_messages",
        )

        try:
            # Add different items to each session
            items1 = cast(
                list[TResponseInputItem],
                [{"role": "user", "content": "Session 1 message", "type": "message"}],
            )
            items2 = cast(
                list[TResponseInputItem],
                [{"role": "user", "content": "Session 2 message", "type": "message"}],
            )

            await self.session.add_items(items1)
            await session2.add_items(items2)

            # Verify sessions have different data
            session1_items = await self.session.get_items()
            session2_items = await session2.get_items()

            self.assertEqual(len(session1_items), 1)
            self.assertEqual(len(session2_items), 1)
            self.assertEqual(session1_items[0]["content"], "Session 1 message")  # type: ignore
            self.assertEqual(session2_items[0]["content"], "Session 2 message")  # type: ignore

        finally:
            await session2.clear_session()
            await session2.close()

    async def test_integration_empty_session_operations(self):
        """Test operations on empty session."""
        # Pop from empty session
        popped = await self.session.pop_item()
        self.assertIsNone(popped)

        # Get items from empty session
        items = await self.session.get_items()
        self.assertEqual(len(items), 0)

        # Get items with limit from empty session
        limited_items = await self.session.get_items(limit=5)
        self.assertEqual(len(limited_items), 0)

        # Clear empty session (should not error)
        await self.session.clear_session()

    async def test_integration_connection_string_with_custom_tables(self):
        """Test creating session with custom table names."""
        custom_session = await PostgreSQLSession.from_connection_string(
            "custom_table_test",
            self.TEST_CONNECTION_STRING,
            sessions_table="custom_sessions_table",
            messages_table="custom_messages_table",
        )

        try:
            # Test basic functionality with custom tables
            test_items = cast(
                list[TResponseInputItem],
                [{"role": "user", "content": "Custom table test", "type": "message"}],
            )

            await custom_session.add_items(test_items)
            stored_items = await custom_session.get_items()

            self.assertEqual(len(stored_items), 1)
            self.assertEqual(stored_items[0]["content"], "Custom table test")  # type: ignore

        finally:
            await custom_session.clear_session()
            await custom_session.close()

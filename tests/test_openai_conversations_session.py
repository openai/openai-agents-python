"""Tests for OpenAI Conversations Session functionality."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import Agent, Runner, TResponseInputItem
from agents.memory.openai_conversations_session import (
    OpenAIConversationsSession,
    start_openai_conversations_session,
)
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message


@pytest.fixture
def mock_openai_client():
    """Create a mock OpenAI client for testing."""
    client = AsyncMock()

    # Mock conversations.create
    client.conversations.create.return_value = MagicMock(id="test_conversation_id")

    # Mock conversations.delete
    client.conversations.delete.return_value = None

    # Mock conversations.items.create
    client.conversations.items.create.return_value = None

    # Mock conversations.items.delete
    client.conversations.items.delete.return_value = None

    return client


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


class TestStartOpenAIConversationsSession:
    """Test the standalone start_openai_conversations_session function."""

    @pytest.mark.asyncio
    async def test_start_with_provided_client(self, mock_openai_client):
        """Test starting a conversation session with a provided client."""
        conversation_id = await start_openai_conversations_session(mock_openai_client)

        assert conversation_id == "test_conversation_id"
        mock_openai_client.conversations.create.assert_called_once_with(items=[])

    @pytest.mark.asyncio
    async def test_start_with_none_client(self):
        """Test starting a conversation session with None client (uses default)."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch("agents.memory.openai_conversations_session.AsyncOpenAI"):
                # Test case 1: get_default_openai_client returns a client
                mock_default_client = AsyncMock()
                mock_default_client.conversations.create.return_value = MagicMock(
                    id="default_client_id"
                )
                mock_get_default.return_value = mock_default_client

                conversation_id = await start_openai_conversations_session(None)

                assert conversation_id == "default_client_id"
                mock_get_default.assert_called_once()
                mock_default_client.conversations.create.assert_called_once_with(items=[])

    @pytest.mark.asyncio
    async def test_start_with_none_client_fallback(self):
        """Test starting a conversation session when get_default_openai_client returns None."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch(
                "agents.memory.openai_conversations_session.AsyncOpenAI"
            ) as mock_async_openai:
                # Test case 2: get_default_openai_client returns None, fallback to AsyncOpenAI()
                mock_get_default.return_value = None
                mock_fallback_client = AsyncMock()
                mock_fallback_client.conversations.create.return_value = MagicMock(
                    id="fallback_client_id"
                )
                mock_async_openai.return_value = mock_fallback_client

                conversation_id = await start_openai_conversations_session(None)

                assert conversation_id == "fallback_client_id"
                mock_get_default.assert_called_once()
                mock_async_openai.assert_called_once()
                mock_fallback_client.conversations.create.assert_called_once_with(items=[])


class TestOpenAIConversationsSessionConstructor:
    """Test OpenAIConversationsSession constructor and client handling."""

    def test_init_with_conversation_id_and_client(self, mock_openai_client):
        """Test constructor with both conversation_id and openai_client provided."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        assert session._session_id == "test_id"
        assert session._openai_client is mock_openai_client

    def test_init_with_conversation_id_only(self):
        """Test constructor with only conversation_id, client should be created."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch("agents.memory.openai_conversations_session.AsyncOpenAI"):
                mock_default_client = AsyncMock()
                mock_get_default.return_value = mock_default_client

                session = OpenAIConversationsSession(conversation_id="test_id")

                assert session._session_id == "test_id"
                assert session._openai_client is mock_default_client
                mock_get_default.assert_called_once()

    def test_init_with_client_only(self, mock_openai_client):
        """Test constructor with only openai_client, no conversation_id."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        assert session._session_id is None
        assert session._openai_client is mock_openai_client

    def test_init_with_no_args_fallback(self):
        """Test constructor with no args, should create default client."""
        with patch(
            "agents.memory.openai_conversations_session.get_default_openai_client"
        ) as mock_get_default:
            with patch(
                "agents.memory.openai_conversations_session.AsyncOpenAI"
            ) as mock_async_openai:
                # Test fallback when get_default_openai_client returns None
                mock_get_default.return_value = None
                mock_fallback_client = AsyncMock()
                mock_async_openai.return_value = mock_fallback_client

                session = OpenAIConversationsSession()

                assert session._session_id is None
                assert session._openai_client is mock_fallback_client
                mock_get_default.assert_called_once()
                mock_async_openai.assert_called_once()


class TestOpenAIConversationsSessionLifecycle:
    """Test session ID lifecycle management."""

    @pytest.mark.asyncio
    async def test_get_session_id_with_existing_id(self, mock_openai_client):
        """Test _get_session_id when session_id already exists."""
        session = OpenAIConversationsSession(
            conversation_id="existing_id", openai_client=mock_openai_client
        )

        session_id = await session._get_session_id()

        assert session_id == "existing_id"
        # Should not call conversations.create since ID already exists
        mock_openai_client.conversations.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_session_id_creates_new_conversation(self, mock_openai_client):
        """Test _get_session_id when session_id is None, should create new conversation."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        session_id = await session._get_session_id()

        assert session_id == "test_conversation_id"
        assert session._session_id == "test_conversation_id"
        mock_openai_client.conversations.create.assert_called_once_with(items=[])

    @pytest.mark.asyncio
    async def test_clear_session_id(self, mock_openai_client):
        """Test _clear_session_id sets session_id to None."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        await session._clear_session_id()

        assert session._session_id is None


class TestOpenAIConversationsSessionBasicOperations:
    """Test basic CRUD operations with simple mocking."""

    @pytest.mark.asyncio
    async def test_add_items_simple(self, mock_openai_client):
        """Test adding items to the conversation."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        await session.add_items(items)

        mock_openai_client.conversations.items.create.assert_called_once_with(
            conversation_id="test_id", items=items
        )

    @pytest.mark.asyncio
    async def test_add_items_creates_session_id(self, mock_openai_client):
        """Test that add_items creates session_id if it doesn't exist."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        items: list[TResponseInputItem] = [{"role": "user", "content": "Hello"}]

        await session.add_items(items)

        # Should create conversation first
        mock_openai_client.conversations.create.assert_called_once_with(items=[])
        # Then add items
        mock_openai_client.conversations.items.create.assert_called_once_with(
            conversation_id="test_conversation_id", items=items
        )

    @pytest.mark.asyncio
    async def test_pop_item_with_items(self, mock_openai_client):
        """Test popping item when items exist using method patching."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        # Mock get_items to return one item
        latest_item = {"id": "item_123", "role": "assistant", "content": "Latest message"}

        with patch.object(session, "get_items", return_value=[latest_item]):
            popped_item = await session.pop_item()

            assert popped_item == latest_item
            mock_openai_client.conversations.items.delete.assert_called_once_with(
                conversation_id="test_id", item_id="item_123"
            )

    @pytest.mark.asyncio
    async def test_pop_item_empty_session(self, mock_openai_client):
        """Test popping item from empty session."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        # Mock get_items to return empty list
        with patch.object(session, "get_items", return_value=[]):
            popped_item = await session.pop_item()

            assert popped_item is None
            mock_openai_client.conversations.items.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_session(self, mock_openai_client):
        """Test clearing the entire session."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        await session.clear_session()

        # Should delete the conversation and clear session ID
        mock_openai_client.conversations.delete.assert_called_once_with(conversation_id="test_id")
        assert session._session_id is None

    @pytest.mark.asyncio
    async def test_clear_session_creates_session_id_first(self, mock_openai_client):
        """Test that clear_session creates session_id if it doesn't exist."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        await session.clear_session()

        # Should create conversation first, then delete it
        mock_openai_client.conversations.create.assert_called_once_with(items=[])
        mock_openai_client.conversations.delete.assert_called_once_with(
            conversation_id="test_conversation_id"
        )
        assert session._session_id is None


class TestOpenAIConversationsSessionRunnerIntegration:
    """Test integration with Agent Runner using simple mocking."""

    @pytest.mark.asyncio
    async def test_runner_integration_basic(self, agent: Agent, mock_openai_client):
        """Test that OpenAIConversationsSession works with Agent Runner."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Mock the session methods to avoid complex async iterator setup
        with patch.object(session, "get_items", return_value=[]):
            with patch.object(session, "add_items") as mock_add_items:
                # Run the agent
                assert isinstance(agent.model, FakeModel)
                agent.model.set_next_output([get_text_message("San Francisco")])

                result = await Runner.run(
                    agent, "What city is the Golden Gate Bridge in?", session=session
                )

                assert result.final_output == "San Francisco"

                # Verify session interactions occurred
                mock_add_items.assert_called()

    @pytest.mark.asyncio
    async def test_runner_with_conversation_history(self, agent: Agent, mock_openai_client):
        """Test that conversation history is preserved across Runner calls."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Mock conversation history
        conversation_history = [
            {"role": "user", "content": "What city is the Golden Gate Bridge in?"},
            {"role": "assistant", "content": "San Francisco"},
        ]

        with patch.object(session, "get_items", return_value=conversation_history):
            with patch.object(session, "add_items"):
                # Second turn - should have access to previous conversation
                assert isinstance(agent.model, FakeModel)
                agent.model.set_next_output([get_text_message("California")])

                result = await Runner.run(agent, "What state is it in?", session=session)

                assert result.final_output == "California"

                # Verify that the model received the conversation history
                last_input = agent.model.last_turn_args["input"]
                assert len(last_input) > 1  # Should include previous messages

                # Check that previous conversation is included
                input_contents = [str(item.get("content", "")) for item in last_input]
                assert any("Golden Gate Bridge" in content for content in input_contents)


class TestOpenAIConversationsSessionErrorHandling:
    """Test error handling for various failure scenarios."""

    @pytest.mark.asyncio
    async def test_api_failure_during_conversation_creation(self, mock_openai_client):
        """Test handling of API failures during conversation creation."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Mock API failure
        mock_openai_client.conversations.create.side_effect = Exception("API Error")

        with pytest.raises(Exception, match="API Error"):
            await session._get_session_id()

    @pytest.mark.asyncio
    async def test_api_failure_during_add_items(self, mock_openai_client):
        """Test handling of API failures during add_items."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        mock_openai_client.conversations.items.create.side_effect = Exception("Add items failed")

        items: list[TResponseInputItem] = [{"role": "user", "content": "Hello"}]

        with pytest.raises(Exception, match="Add items failed"):
            await session.add_items(items)

    @pytest.mark.asyncio
    async def test_api_failure_during_clear_session(self, mock_openai_client):
        """Test handling of API failures during clear_session."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        mock_openai_client.conversations.delete.side_effect = Exception("Clear session failed")

        with pytest.raises(Exception, match="Clear session failed"):
            await session.clear_session()

    @pytest.mark.asyncio
    async def test_invalid_item_id_in_pop_item(self, mock_openai_client):
        """Test handling of invalid item ID during pop_item."""
        session = OpenAIConversationsSession(
            conversation_id="test_id", openai_client=mock_openai_client
        )

        # Mock item without ID
        invalid_item = {"role": "assistant", "content": "No ID"}

        with patch.object(session, "get_items", return_value=[invalid_item]):
            # This should raise a KeyError because 'id' field is missing
            with pytest.raises(KeyError, match="'id'"):
                await session.pop_item()


class TestOpenAIConversationsSessionConcurrentAccess:
    """Test concurrent access patterns with simple scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_sessions_different_conversation_ids(self, mock_openai_client):
        """Test that multiple sessions with different conversation IDs are isolated."""
        session1 = OpenAIConversationsSession(
            conversation_id="conversation_1", openai_client=mock_openai_client
        )
        session2 = OpenAIConversationsSession(
            conversation_id="conversation_2", openai_client=mock_openai_client
        )

        items1: list[TResponseInputItem] = [{"role": "user", "content": "Session 1 message"}]
        items2: list[TResponseInputItem] = [{"role": "user", "content": "Session 2 message"}]

        # Add items to both sessions
        await session1.add_items(items1)
        await session2.add_items(items2)

        # Verify calls were made with correct conversation IDs
        assert mock_openai_client.conversations.items.create.call_count == 2

        # Check the calls
        calls = mock_openai_client.conversations.items.create.call_args_list
        assert calls[0][1]["conversation_id"] == "conversation_1"
        assert calls[0][1]["items"] == items1
        assert calls[1][1]["conversation_id"] == "conversation_2"
        assert calls[1][1]["items"] == items2

    @pytest.mark.asyncio
    async def test_session_id_lazy_creation_consistency(self, mock_openai_client):
        """Test that session ID creation is consistent across multiple calls."""
        session = OpenAIConversationsSession(openai_client=mock_openai_client)

        # Call _get_session_id multiple times
        id1 = await session._get_session_id()
        id2 = await session._get_session_id()
        id3 = await session._get_session_id()

        # All should return the same session ID
        assert id1 == id2 == id3 == "test_conversation_id"

        # Conversation should only be created once
        mock_openai_client.conversations.create.assert_called_once()

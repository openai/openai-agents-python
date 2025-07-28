"""Tests for Redis session memory functionality."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.memory.providers.redis import RedisSession


class TestRedisSession:
    """Test cases for RedisSession class."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        mock_client = AsyncMock()
        mock_client.from_url.return_value = mock_client
        return mock_client

    @pytest.fixture
    def redis_session(self):
        """Create a RedisSession instance for testing."""
        return RedisSession(
            session_id="test_session_123",
            redis_url="redis://localhost:6379",
            db=0,
            session_prefix="test_session",
            messages_prefix="test_messages",
            ttl=3600,
        )

    @pytest.fixture
    def sample_items(self):
        """Create sample items for testing."""
        return [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
            {"role": "user", "content": [{"type": "text", "text": "How are you?"}]},
        ]

    async def test_init(self, redis_session):
        """Test RedisSession initialization."""
        assert redis_session.session_id == "test_session_123"
        assert redis_session.redis_url == "redis://localhost:6379"
        assert redis_session.db == 0
        assert redis_session.session_prefix == "test_session"
        assert redis_session.messages_prefix == "test_messages"
        assert redis_session.ttl == 3600
        assert redis_session.session_key == "test_session:test_session_123"
        assert redis_session.messages_key == "test_messages:test_session_123"
        assert redis_session._redis_client is None

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_redis_client(self, mock_from_url, redis_session, mock_redis):
        """Test Redis client creation."""
        mock_from_url.return_value = mock_redis

        client = await redis_session._get_redis_client()

        assert client == mock_redis
        assert redis_session._redis_client == mock_redis
        mock_from_url.assert_called_once_with(
            "redis://localhost:6379",
            db=0,
            decode_responses=True,
            retry_on_error=[redis_session._get_redis_client.__globals__['redis'].BusyLoadingError,
                           redis_session._get_redis_client.__globals__['redis'].ConnectionError],
            retry_on_timeout=True,
        )

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_redis_client_reuse(self, mock_from_url, redis_session, mock_redis):
        """Test that Redis client is reused."""
        mock_from_url.return_value = mock_redis
        redis_session._redis_client = mock_redis

        client = await redis_session._get_redis_client()

        assert client == mock_redis
        mock_from_url.assert_not_called()

    @patch('agents.memory.providers.redis.time.time')
    async def test_ensure_session_exists_new_session(self, mock_time, redis_session, mock_redis):
        """Test creating new session metadata."""
        mock_time.return_value = 1234567890.0
        # session doesn't exist, messages doesn't exist
        mock_redis.exists.side_effect = [False, False]

        await redis_session._ensure_session_exists(mock_redis)

        # Check that exists was called for both session and messages keys
        assert mock_redis.exists.call_count == 2
        mock_redis.exists.assert_any_call("test_session:test_session_123")
        mock_redis.exists.assert_any_call("test_messages:test_session_123")

        mock_redis.hset.assert_called_once_with(
            "test_session:test_session_123",
            mapping={
                "session_id": "test_session_123",
                "created_at": "1234567890.0",
                "updated_at": "1234567890.0",
            }
        )
        mock_redis.expire.assert_any_call("test_session:test_session_123", 3600)

    async def test_ensure_session_exists_existing_session(self, redis_session, mock_redis):
        """Test handling existing session metadata."""
        mock_redis.exists.side_effect = [True, True]  # session exists, messages exist

        await redis_session._ensure_session_exists(mock_redis)

        mock_redis.hset.assert_not_called()
        assert mock_redis.expire.call_count == 2  # TTL set for both keys

    @patch('agents.memory.providers.redis.time.time')
    async def test_update_session_timestamp(self, mock_time, redis_session, mock_redis):
        """Test updating session timestamp."""
        mock_time.return_value = 1234567890.0

        await redis_session._update_session_timestamp(mock_redis)

        mock_redis.hset.assert_called_once_with(
            "test_session:test_session_123",
            "updated_at",
            "1234567890.0"
        )

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_items_no_limit(self, mock_from_url, redis_session, mock_redis, sample_items):
        """Test retrieving all items without limit."""
        mock_from_url.return_value = mock_redis
        mock_redis.lrange.return_value = [json.dumps(item) for item in sample_items]

        items = await redis_session.get_items()

        mock_redis.lrange.assert_called_once_with("test_messages:test_session_123", 0, -1)
        assert items == sample_items

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_items_with_limit(
        self, mock_from_url, redis_session, mock_redis, sample_items
    ):
        """Test retrieving items with limit."""
        mock_from_url.return_value = mock_redis
        mock_redis.lrange.return_value = [json.dumps(item) for item in sample_items[-2:]]

        items = await redis_session.get_items(limit=2)

        mock_redis.lrange.assert_called_once_with("test_messages:test_session_123", -2, -1)
        assert items == sample_items[-2:]

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_items_empty_list(self, mock_from_url, redis_session, mock_redis):
        """Test retrieving items from empty session."""
        mock_from_url.return_value = mock_redis
        mock_redis.lrange.return_value = []

        items = await redis_session.get_items()

        assert items == []

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_items_invalid_json(
        self, mock_from_url, redis_session, mock_redis, sample_items
    ):
        """Test handling invalid JSON in stored items."""
        mock_from_url.return_value = mock_redis
        mock_redis.lrange.return_value = [
            json.dumps(sample_items[0]),
            "invalid json",
            json.dumps(sample_items[1])
        ]

        items = await redis_session.get_items()

        # Should skip invalid JSON and return valid items
        assert items == [sample_items[0], sample_items[1]]

    @patch('agents.memory.providers.redis.redis.from_url')
    @patch('agents.memory.providers.redis.time.time')
    async def test_add_items(
        self, mock_time, mock_from_url, redis_session, mock_redis, sample_items
    ):
        """Test adding items to session."""
        mock_time.return_value = 1234567890.0
        mock_from_url.return_value = mock_redis

        # Create a mock pipeline that's returned directly (not as context manager)
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

        # Mock _ensure_session_exists method
        redis_session._ensure_session_exists = AsyncMock()

        await redis_session.add_items(sample_items)

        # Verify pipeline operations
        mock_pipeline.rpush.assert_called_once_with(
            "test_messages:test_session_123",
            *[json.dumps(item) for item in sample_items]
        )
        mock_pipeline.hset.assert_called_once_with(
            "test_session:test_session_123",
            "updated_at",
            "1234567890.0"
        )
        mock_pipeline.expire.assert_any_call("test_session:test_session_123", 3600)
        mock_pipeline.expire.assert_any_call("test_messages:test_session_123", 3600)
        mock_pipeline.execute.assert_called_once()

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_add_items_empty_list(self, mock_from_url, redis_session, mock_redis):
        """Test adding empty list of items."""
        mock_from_url.return_value = mock_redis

        await redis_session.add_items([])

        # Should not call Redis operations for empty list
        mock_redis.pipeline.assert_not_called()

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_pop_item(self, mock_from_url, redis_session, mock_redis, sample_items):
        """Test popping most recent item."""
        mock_from_url.return_value = mock_redis
        mock_redis.rpop.return_value = json.dumps(sample_items[-1])
        redis_session._update_session_timestamp = AsyncMock()

        item = await redis_session.pop_item()

        mock_redis.rpop.assert_called_once_with("test_messages:test_session_123")
        redis_session._update_session_timestamp.assert_called_once_with(mock_redis)
        assert item == sample_items[-1]

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_pop_item_empty_session(self, mock_from_url, redis_session, mock_redis):
        """Test popping from empty session."""
        mock_from_url.return_value = mock_redis
        mock_redis.rpop.return_value = None
        redis_session._update_session_timestamp = AsyncMock()

        item = await redis_session.pop_item()

        assert item is None
        redis_session._update_session_timestamp.assert_not_called()

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_pop_item_invalid_json(self, mock_from_url, redis_session, mock_redis):
        """Test popping item with invalid JSON."""
        mock_from_url.return_value = mock_redis
        mock_redis.rpop.return_value = "invalid json"
        redis_session._update_session_timestamp = AsyncMock()

        item = await redis_session.pop_item()

        assert item is None
        redis_session._update_session_timestamp.assert_called_once_with(mock_redis)

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_clear_session(self, mock_from_url, redis_session, mock_redis):
        """Test clearing session data."""
        mock_from_url.return_value = mock_redis

        await redis_session.clear_session()

        mock_redis.delete.assert_called_once_with(
            "test_session:test_session_123",
            "test_messages:test_session_123"
        )

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_session_info(self, mock_from_url, redis_session, mock_redis):
        """Test retrieving session metadata."""
        mock_from_url.return_value = mock_redis
        expected_info = {
            "session_id": "test_session_123",
            "created_at": "1234567890.0",
            "updated_at": "1234567890.0"
        }
        mock_redis.hgetall.return_value = expected_info

        info = await redis_session.get_session_info()

        mock_redis.hgetall.assert_called_once_with("test_session:test_session_123")
        assert info == expected_info

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_session_info_not_exists(self, mock_from_url, redis_session, mock_redis):
        """Test retrieving session metadata when session doesn't exist."""
        mock_from_url.return_value = mock_redis
        mock_redis.hgetall.return_value = {}

        info = await redis_session.get_session_info()

        assert info is None

    @patch('agents.memory.providers.redis.redis.from_url')
    async def test_get_session_size(self, mock_from_url, redis_session, mock_redis):
        """Test getting session message count."""
        mock_from_url.return_value = mock_redis
        mock_redis.llen.return_value = 5

        size = await redis_session.get_session_size()

        mock_redis.llen.assert_called_once_with("test_messages:test_session_123")
        assert size == 5

    async def test_close(self, redis_session):
        """Test closing Redis connection."""
        mock_client = AsyncMock()
        redis_session._redis_client = mock_client

        await redis_session.close()

        mock_client.aclose.assert_called_once()
        assert redis_session._redis_client is None

    async def test_close_no_client(self, redis_session):
        """Test closing when no client exists."""
        await redis_session.close()
        # Should not raise an exception

    async def test_context_manager(self, redis_session):
        """Test async context manager functionality."""
        redis_session.close = AsyncMock()

        async with redis_session as session:
            assert session == redis_session

        redis_session.close.assert_called_once()

    def test_session_without_ttl(self):
        """Test session creation without TTL."""
        session = RedisSession("test_session", ttl=None)
        assert session.ttl is None

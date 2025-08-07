from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from agents.memory.session import SessionABC

try:
    import redis.asyncio as redis

    if TYPE_CHECKING:
        from agents.items import TResponseInputItem

except ImportError as err:
    raise ImportError("redis and openai-agents packages are required") from err


class RedisSession(SessionABC):
    """Redis-based implementation of session storage.

    This implementation stores conversation history in Redis using lists and hashes.
    Each session uses a Redis list to store messages in chronological order and
    a hash to store session metadata.
    """

    def __init__(
        self,
        session_id: str,
        redis_url: str = "redis://localhost:6379",
        db: int = 0,
        session_prefix: str = "agent_session",
        messages_prefix: str = "agent_messages",
        ttl: int | None = None,
    ):
        """Initialize the Redis session.

        Args:
            session_id: Unique identifier for the conversation session
            redis_url: Redis connection URL. Defaults to 'redis://localhost:6379'
            db: Redis database name. Defaults to `default`
            session_prefix: Prefix for session metadata keys. Defaults to 'agent_session'
            messages_prefix: Prefix for message list keys. Defaults to 'agent_messages'
            ttl: Time-to-live for session data in seconds. If None, data persists indefinitely
        """
        self.session_id = session_id
        self.redis_url = redis_url
        self.db = db
        self.session_prefix = session_prefix
        self.messages_prefix = messages_prefix
        self.ttl = ttl

        # Redis keys for this session
        self.session_key = f"{session_prefix}:{session_id}"
        self.messages_key = f"{messages_prefix}:{session_id}"

        self._redis_client: redis.Redis | None = None

    async def _get_redis_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._redis_client is None:
            self._redis_client = redis.from_url(
                self.redis_url,
                db=self.db,
                decode_responses=True,
                retry_on_error=[redis.BusyLoadingError, redis.ConnectionError],
                retry_on_timeout=True,
            )
        return self._redis_client

    async def _ensure_session_exists(self, client: redis.Redis) -> None:
        """Ensure session metadata exists in Redis."""
        current_time = time.time()  # Use float for higher precision

        # Check if session already exists
        exists = await client.exists(self.session_key)
        if not exists:
            # Create session metadata only if it doesn't exist
            await client.hset(  # type: ignore[misc]
                self.session_key,
                mapping={
                    "session_id": self.session_id,
                    "created_at": str(current_time),
                    "updated_at": str(current_time),
                },
            )

        # Set TTL if specified (always refresh TTL)
        if self.ttl is not None:
            await client.expire(self.session_key, self.ttl)
            # For messages key, we only set TTL if it exists
            # If it doesn't exist yet, TTL will be set when first message is added
            messages_exists = await client.exists(self.messages_key)
            if messages_exists:
                await client.expire(self.messages_key, self.ttl)

    async def _update_session_timestamp(self, client: redis.Redis) -> None:
        """Update the session's last updated timestamp."""
        current_time = time.time()  # Use float for higher precision
        await client.hset(self.session_key, "updated_at", str(current_time))  # type: ignore[misc]

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        client = await self._get_redis_client()

        if limit is None:
            # Get all items from the list (oldest to newest)
            raw_items: list[str] = await client.lrange(self.messages_key, 0, -1)  # type: ignore[misc]
        else:
            # Get the latest N items (newest to oldest), then reverse
            raw_items = await client.lrange(self.messages_key, -limit, -1)  # type: ignore[misc]

        items = []
        for raw_item in raw_items:
            try:
                item = json.loads(raw_item)
                items.append(item)
            except json.JSONDecodeError:
                # Skip invalid JSON entries
                continue

        return items

    async def add_item(self, item: TResponseInputItem) -> None:
        """Add a new item to the session's conversation history.

        Args:
            item: The response input item to add
        """
        client = await self._get_redis_client()

        # Serialize and add the item to the messages list
        serialized_item = json.dumps(item)
        pipeline = client.pipeline()
        pipeline.rpush(self.messages_key, serialized_item)

        # Set expiration on the messages key if TTL is configured
        if self.ttl:
            pipeline.expire(self.messages_key, self.ttl)

        await pipeline.execute()

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add multiple items to the session's conversation history.

        Args:
            items: List of response input items to add
        """
        if not items:
            return

        client = await self._get_redis_client()

        # Ensure session exists first
        await self._ensure_session_exists(client)

        # Serialize all items and add them to the messages list in one rpush call
        serialized_items = [json.dumps(item) for item in items]
        pipeline = client.pipeline()
        pipeline.rpush(self.messages_key, *serialized_items)

        # Update session timestamp
        current_time = time.time()
        pipeline.hset(self.session_key, "updated_at", str(current_time))

        # Set expiration on both keys if TTL is configured
        if self.ttl:
            pipeline.expire(self.session_key, self.ttl)
            pipeline.expire(self.messages_key, self.ttl)

        await pipeline.execute()

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        client = await self._get_redis_client()

        # Pop from the right end of the list (most recent item)
        raw_item = await client.rpop(self.messages_key)  # type: ignore[misc]

        if raw_item is None:
            return None

        # Update session timestamp after successful pop
        await self._update_session_timestamp(client)

        try:
            item: TResponseInputItem = json.loads(raw_item)
            return item
        except json.JSONDecodeError:
            # Return None for corrupted JSON entries (already deleted)
            return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        client = await self._get_redis_client()

        # Delete both session metadata and messages
        await client.delete(self.session_key, self.messages_key)

    async def get_session_info(self) -> dict[str, str] | None:
        """Get session metadata.

        Returns:
            Dictionary containing session metadata, or None if session doesn't exist
        """
        client = await self._get_redis_client()
        session_data: dict[str, str] = await client.hgetall(self.session_key)  # type: ignore[misc]

        return session_data if session_data else None

    async def get_session_size(self) -> int:
        """Get the number of messages in the session.

        Returns:
            Number of messages in the session
        """
        client = await self._get_redis_client()
        length: int = await client.llen(self.messages_key)  # type: ignore[misc]
        return length

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis_client is not None:
            await self._redis_client.aclose()
            self._redis_client = None

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

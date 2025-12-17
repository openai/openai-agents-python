"""Redis-powered Session backend.

Usage::

    from agents.extensions.memory import RedisSession

    # Create from Redis URL
    session = RedisSession.from_url(
        session_id="user-123",
        url="redis://localhost:6379/0",
    )

    # Or pass an existing Redis client that your application already manages
    session = RedisSession(
        session_id="user-123",
        redis_client=my_redis_client,
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

try:
    import redis.asyncio as redis
    from redis.asyncio import Redis
except ImportError as e:
    raise ImportError(
        "RedisSession requires the 'redis' package. Install it with: pip install redis"
    ) from e

from ...items import TResponseInputItem
from ...memory.session import SessionABC


class RedisSession(SessionABC):
    """Redis implementation of :pyclass:`agents.memory.session.Session`."""

    def __init__(
        self,
        session_id: str,
        *,
        redis_client: Redis,
        key_prefix: str = "agents:session",
        ttl: int | None = None,
    ):
        """Initializes a new RedisSession.

        Args:
            session_id (str): Unique identifier for the conversation.
            redis_client (Redis[bytes]): A pre-configured Redis async client.
            key_prefix (str, optional): Prefix for Redis keys to avoid collisions.
                Defaults to "agents:session".
            ttl (int | None, optional): Time-to-live in seconds for session data.
                If None, data persists indefinitely. Defaults to None.
        """
        self.session_id = session_id
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._owns_client = False  # Track if we own the Redis client

        # Redis key patterns
        self._session_key = f"{self._key_prefix}:{self.session_id}"
        self._messages_key = f"{self._session_key}:messages"
        self._counter_key = f"{self._session_key}:counter"

    @classmethod
    def from_url(
        cls,
        session_id: str,
        *,
        url: str,
        redis_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> RedisSession:
        """Create a session from a Redis URL string.

        Args:
            session_id (str): Conversation ID.
            url (str): Redis URL, e.g. "redis://localhost:6379/0" or "rediss://host:6380".
            redis_kwargs (dict[str, Any] | None): Additional keyword arguments forwarded to
                redis.asyncio.from_url.
            **kwargs: Additional keyword arguments forwarded to the main constructor
                (e.g., key_prefix, ttl, etc.).

        Returns:
            RedisSession: An instance of RedisSession connected to the specified Redis server.
        """
        redis_kwargs = redis_kwargs or {}

        redis_client = redis.from_url(url, **redis_kwargs)
        session = cls(session_id, redis_client=redis_client, **kwargs)
        session._owns_client = True  # We created the client, so we own it
        return session

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to JSON string. Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))

    async def _deserialize_item(self, item: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item. Can be overridden by subclasses."""
        return json.loads(item)  # type: ignore[no-any-return]  # json.loads returns Any but we know the structure

    async def _get_next_id(self) -> int:
        """Get the next message ID using Redis INCR for atomic increment."""
        result = await self._redis.incr(self._counter_key)
        return int(result)

    async def _set_ttl_if_configured(self, *keys: str) -> None:
        """Set TTL on keys if configured."""
        if self._ttl is not None:
            pipe = self._redis.pipeline()
            for key in keys:
                pipe.expire(key, self._ttl)
            await pipe.execute()

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        async with self._lock:
            if limit is None:
                # Get all messages in chronological order
                raw_messages = await self._redis.lrange(self._messages_key, 0, -1)  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context
            else:
                if limit <= 0:
                    return []
                # Get the latest N messages (Redis list is ordered chronologically)
                # Use negative indices to get from the end - Redis uses -N to -1 for last N items
                raw_messages = await self._redis.lrange(self._messages_key, -limit, -1)  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context

            items: list[TResponseInputItem] = []
            for raw_msg in raw_messages:
                try:
                    # Handle both bytes (default) and str (decode_responses=True) Redis clients
                    if isinstance(raw_msg, bytes):
                        msg_str = raw_msg.decode("utf-8")
                    else:
                        msg_str = raw_msg  # Already a string
                    item = await self._deserialize_item(msg_str)
                    items.append(item)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Skip corrupted messages
                    continue

            return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        async with self._lock:
            pipe = self._redis.pipeline()

            # Set session metadata with current timestamp
            pipe.hset(
                self._session_key,
                mapping={
                    "session_id": self.session_id,
                    "created_at": str(int(time.time())),
                    "updated_at": str(int(time.time())),
                },
            )

            # Add all items to the messages list
            serialized_items = []
            for item in items:
                serialized = await self._serialize_item(item)
                serialized_items.append(serialized)

            if serialized_items:
                pipe.rpush(self._messages_key, *serialized_items)

            # Update the session timestamp
            pipe.hset(self._session_key, "updated_at", str(int(time.time())))

            # Execute all commands
            await pipe.execute()

            # Set TTL if configured
            await self._set_ttl_if_configured(
                self._session_key, self._messages_key, self._counter_key
            )

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        async with self._lock:
            # Use RPOP to atomically remove and return the rightmost (most recent) item
            raw_msg = await self._redis.rpop(self._messages_key)  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context

            if raw_msg is None:
                return None

            try:
                # Handle both bytes (default) and str (decode_responses=True) Redis clients
                if isinstance(raw_msg, bytes):
                    msg_str = raw_msg.decode("utf-8")
                else:
                    msg_str = raw_msg  # Already a string
                return await self._deserialize_item(msg_str)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Return None for corrupted messages (already removed)
                return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
            # Delete all keys associated with this session
            await self._redis.delete(
                self._session_key,
                self._messages_key,
                self._counter_key,
            )

    async def close(self) -> None:
        """Close the Redis connection.

        Only closes the connection if this session owns the Redis client
        (i.e., created via from_url). If the client was injected externally,
        the caller is responsible for managing its lifecycle.
        """
        if self._owns_client:
            await self._redis.aclose()

    async def ping(self) -> bool:
        """Test Redis connectivity.

        Returns:
            True if Redis is reachable, False otherwise.
        """
        try:
            await self._redis.ping()  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context
            return True
        except Exception:
            return False

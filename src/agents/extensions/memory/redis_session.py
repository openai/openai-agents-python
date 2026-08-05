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
from dataclasses import dataclass
from typing import Any

from ._optional_imports import raise_optional_dependency_error

try:
    import redis.asyncio as redis
    from redis.asyncio import BlockingConnectionPool, ConnectionPool, Redis
    from redis.exceptions import ResponseError, WatchError
except ImportError as e:
    raise_optional_dependency_error(
        "RedisSession",
        dependency_name="redis",
        extra_name="redis",
        cause=e,
    )

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import (
    SessionSettings,
    coerce_session_settings,
    resolve_session_limit,
)


@dataclass
class _PipelineAttemptOutcome:
    committed: bool
    operation_error: BaseException | None
    cleanup_error: BaseException | None
    settled: bool


class _PipelineConnectionPool:
    """Track one pipeline's connection release without changing the shared pool."""

    def __init__(self, pool: Any):
        self._pool = pool
        self.release_started = False
        self.release_completed = False
        release_implementation = getattr(pool.release, "__func__", None)
        if release_implementation not in {
            ConnectionPool.release,
            BlockingConnectionPool.release,
        }:
            raise RuntimeError(
                "RedisSession requires the standard redis-py connection pool release method"
            )
        self._uses_standard_release = release_implementation is ConnectionPool.release

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    async def notify_capacity_available(self) -> None:
        if isinstance(self._pool, BlockingConnectionPool):
            async with self._pool._condition:
                self._pool._condition.notify()

    async def release(self, connection: Any) -> None:
        self.release_started = True
        was_in_use = connection in self._pool._in_use_connections
        try:
            await self._pool.release(connection)
        except BaseException:
            if was_in_use and connection in self._pool._available_connections:
                self.release_completed = True
                await self.notify_capacity_available()
            elif (
                was_in_use
                and self._uses_standard_release
                and connection in self._pool._in_use_connections
            ):
                self.release_completed = True
            raise
        else:
            self.release_completed = True


async def _finish_pipeline(
    pipe: Any,
    connection_pool: _PipelineConnectionPool,
) -> tuple[BaseException | None, bool, Any | None]:
    """Reset a pipeline and prove whether its connection left pipeline ownership."""
    if connection_pool.release_completed:
        pipe.connection = None
        return None, True, None

    reset_error: BaseException | None = None
    try:
        await pipe.reset()
    except BaseException as exc:
        reset_error = exc

    if connection_pool.release_completed:
        pipe.connection = None
        return reset_error, True, None

    connection = getattr(pipe, "connection", None)
    if connection is None:
        return reset_error, True, None

    if (
        connection_pool.release_started
        and connection not in connection_pool._pool._in_use_connections
    ):
        try:
            connection._close()
        except BaseException as close_error:
            pipe.connection = None
            return close_error, True, connection
        await connection_pool.notify_capacity_available()
        pipe.connection = None
        if reset_error is None:
            reset_error = RuntimeError("Redis pipeline release did not complete")
        return reset_error, True, None

    try:
        connection.mark_for_reconnect()
    except BaseException as reconnect_error:
        return reconnect_error, False, None

    try:
        await connection_pool.release(connection)
    except BaseException as release_error:
        if (
            not connection_pool.release_completed
            and connection not in connection_pool._pool._in_use_connections
        ):
            try:
                connection._close()
            except BaseException as close_error:
                pipe.connection = None
                return close_error, True, connection
            await connection_pool.notify_capacity_available()
            connection_pool.release_completed = True
        pipe.connection = None
        return release_error, connection_pool.release_completed, None

    pipe.connection = None
    return reset_error, True, None


async def _await_pipeline_attempt(
    attempt: asyncio.Task[_PipelineAttemptOutcome],
    completion_owned: asyncio.Event,
) -> tuple[_PipelineAttemptOutcome, asyncio.CancelledError | None]:
    """Wait for an attempt while preserving only caller-originated cancellation."""
    cancellation: asyncio.CancelledError | None = None
    attempt_cancelled = False

    while True:
        try:
            outcome = await asyncio.shield(attempt)
        except asyncio.CancelledError as exc:
            if attempt.done() and attempt.cancelled():
                if cancellation is not None:
                    raise cancellation from None
                raise
            if cancellation is None:
                cancellation = exc
            if not completion_owned.is_set() and not attempt_cancelled:
                attempt.cancel()
                attempt_cancelled = True
        else:
            return outcome, cancellation


class RedisSession(SessionABC):
    """Redis implementation of [`Session`][agents.memory.session.Session]."""

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        redis_client: Redis,
        key_prefix: str = "agents:session",
        ttl: int | None = None,
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ):
        """Initializes a new RedisSession.

        Args:
            session_id (str): Unique identifier for the conversation.
            redis_client (Redis[bytes]): A pre-configured Redis async client.
            key_prefix (str, optional): Prefix for Redis keys to avoid collisions.
                Defaults to "agents:session".
            ttl (int | None, optional): Time-to-live in seconds for session data.
                If None, data persists indefinitely. Values outside Redis's supported expiration
                range raise ValueError when adding items. Defaults to None.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
        """
        self.session_id = session_id
        self.session_settings = (
            coerce_session_settings(session_settings)
            if session_settings is not None
            else SessionSettings()
        )
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._owns_client = False  # Track if we own the Redis client
        self._closed = False
        self._client_released = False
        self._detached_connections: set[Any] = set()

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
        session_settings: SessionSettings | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> RedisSession:
        """Create a session from a Redis URL string.

        Args:
            session_id (str): Conversation ID.
            url (str): Redis URL, e.g. "redis://localhost:6379/0" or "rediss://host:6380".
            redis_kwargs (dict[str, Any] | None): Additional keyword arguments forwarded to
                redis.asyncio.from_url.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
            **kwargs: Additional keyword arguments forwarded to the main constructor
                (e.g., key_prefix, ttl, etc.).

        Returns:
            RedisSession: An instance of RedisSession connected to the specified Redis server.
        """
        redis_kwargs = redis_kwargs or {}

        redis_client = redis.from_url(url, **redis_kwargs)
        session = cls(
            session_id,
            redis_client=redis_client,
            session_settings=session_settings,
            **kwargs,
        )
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

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    def _check_not_closed(self) -> None:
        """Raise if the session has already been closed."""
        if self._closed:
            raise RuntimeError("RedisSession is closed")

    @staticmethod
    def _key_type_name(key_type: Any) -> str:
        """Normalize Redis TYPE responses from bytes and decoded clients."""
        if isinstance(key_type, bytes):
            return key_type.decode("utf-8")
        return str(key_type)

    async def _write_items_attempt(
        self,
        pipe: Any,
        keys: tuple[str, str, str],
        serialized_items: list[str],
        completion_owned: asyncio.Event,
    ) -> _PipelineAttemptOutcome:
        """Run one watched write attempt and finish its pipeline before returning."""
        committed = False
        operation_error: BaseException | None = None
        batch_response_index: int | None = None
        raise_first_error = pipe.raise_first_error
        raw_connection_pool = pipe.connection_pool
        connection_pool = _PipelineConnectionPool(raw_connection_pool)
        pipe.connection_pool = connection_pool

        def raise_first_error_and_mark(*args: Any, **kwargs: Any) -> Any:
            nonlocal committed
            response = args[1] if len(args) > 1 else kwargs.get("response")
            if (
                batch_response_index is not None
                and isinstance(response, list)
                and batch_response_index < len(response)
                and not isinstance(response[batch_response_index], BaseException)
            ):
                committed = True
            result = raise_first_error(*args, **kwargs)
            committed = True
            return result

        try:
            try:
                await pipe.watch(*keys)
                session_key_type = self._key_type_name(await pipe.type(self._session_key))
                messages_key_type = self._key_type_name(await pipe.type(self._messages_key))
                if session_key_type not in ("none", "hash"):
                    raise ResponseError("WRONGTYPE session metadata key must contain a hash")
                if messages_key_type not in ("none", "list"):
                    raise ResponseError("WRONGTYPE session messages key must contain a list")

                if self._ttl is None:
                    now = str(int(time.time()))
                    expiration_time_ms = None
                else:
                    server_seconds, server_microseconds = await pipe.time()
                    now = str(int(server_seconds))
                    expiration_time_ms = (
                        int(server_seconds) * 1000
                        + int(server_microseconds) // 1000
                        + self._ttl * 1000
                    )
                    min_int64 = -(2**63)
                    max_int64 = 2**63 - 1
                    if not min_int64 <= expiration_time_ms <= max_int64:
                        raise ValueError("ttl is outside Redis's supported expiration range")

                pipe.multi()
                pipe.hset(self._session_key, "session_id", self.session_id)
                pipe.hsetnx(self._session_key, "created_at", now)
                batch_response_index = len(pipe.command_stack)
                pipe.rpush(self._messages_key, *serialized_items)
                pipe.hset(self._session_key, "updated_at", now)
                if expiration_time_ms is not None:
                    for key in keys:
                        pipe.pexpireat(key, expiration_time_ms)

                pipe.raise_first_error = raise_first_error_and_mark
                completion_owned.set()
                await pipe.execute()
                committed = True
            except WatchError as exc:
                operation_error = exc
            except BaseException as exc:
                operation_error = exc
        finally:
            completion_owned.set()
            pipe.raise_first_error = raise_first_error
            cleanup_error, settled, detached_connection = await _finish_pipeline(
                pipe, connection_pool
            )
            if detached_connection is not None:
                self._detached_connections.add(detached_connection)
            pipe.connection_pool = raw_connection_pool

        return _PipelineAttemptOutcome(
            committed=committed,
            operation_error=operation_error,
            cleanup_error=cleanup_error,
            settled=settled,
        )

    async def _write_items(
        self,
        serialized_items: list[str],
    ) -> None:
        """Validate key types and atomically write one batch with optimistic locking."""
        keys = (self._session_key, self._messages_key, self._counter_key)
        while True:
            pipe = self._redis.pipeline()
            completion_owned = asyncio.Event()
            attempt = asyncio.create_task(
                self._write_items_attempt(pipe, keys, serialized_items, completion_owned)
            )
            outcome, cancellation = await _await_pipeline_attempt(attempt, completion_owned)

            if not outcome.settled:
                if outcome.cleanup_error is not None:
                    raise outcome.cleanup_error
                raise RuntimeError("Redis pipeline cleanup did not settle its connection")
            if outcome.committed:
                if cancellation is not None:
                    raise cancellation
                return
            if cancellation is not None:
                raise cancellation
            if outcome.cleanup_error is not None:
                raise outcome.cleanup_error
            if isinstance(outcome.operation_error, WatchError):
                continue
            if outcome.operation_error is not None:
                raise outcome.operation_error
            return

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, uses session_settings.limit.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        session_limit = resolve_session_limit(limit, self.session_settings)

        async def _decode_messages(raw_messages: list[Any]) -> list[TResponseInputItem]:
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

        async with self._lock:
            self._check_not_closed()
            if session_limit is None:
                # Get all messages in chronological order
                raw_messages = await self._redis.lrange(self._messages_key, 0, -1)  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context
                return await _decode_messages(raw_messages)

            if session_limit <= 0:
                return []

            # Get the latest N messages (Redis list is ordered chronologically)
            # Use negative indices to get from the end - Redis uses -N to -1 for last N items.
            # Expand the fetch window when corrupt messages sit among the newest entries so
            # limit counts valid conversation items, matching pop_item and the other backends.
            window = session_limit
            while True:
                raw_messages = await self._redis.lrange(self._messages_key, -window, -1)  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context
                items = await _decode_messages(raw_messages)
                if len(items) >= session_limit:
                    return items[-session_limit:]
                if len(raw_messages) < window:
                    return items
                window *= 2

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        self._check_not_closed()
        if not items:
            return

        async with self._lock:
            self._check_not_closed()
            serialized_items = []
            for item in items:
                serialized = await self._serialize_item(item)
                serialized_items.append(serialized)

            await self._write_items(serialized_items)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        async with self._lock:
            self._check_not_closed()
            while True:
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
                    # Drop corrupted messages and keep looking for a valid item.
                    continue

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
            self._check_not_closed()
            # Delete all keys associated with this session
            await self._redis.delete(
                self._session_key,
                self._messages_key,
                self._counter_key,
            )

    async def close(self) -> None:
        """Close the Redis connection.

        Only closes the connection if this session owns the Redis client
        (i.e., created via from_url). In that case the session becomes terminal
        and subsequent operations raise RuntimeError. If the client was injected
        externally, the caller is responsible for managing its lifecycle and
        this is a no-op.

        The session is terminal from the first close attempt. If releasing the
        client fails or is cancelled, operations still raise and a later close()
        retries the unfinished cleanup. Once the client is released, repeated and
        concurrent calls are safe no-ops.
        """
        async with self._lock:
            detached_error: BaseException | None = None
            for connection in tuple(self._detached_connections):
                try:
                    connection._close()
                except BaseException as exc:
                    if detached_error is None:
                        detached_error = exc
                else:
                    self._detached_connections.discard(connection)

            if not self._owns_client:
                if detached_error is not None:
                    raise detached_error
                return
            self._closed = True
            if not self._client_released:
                await self._redis.aclose()
                self._client_released = True
            if detached_error is not None:
                raise detached_error

    async def ping(self) -> bool:
        """Test Redis connectivity.

        Returns:
            True if Redis is reachable, False otherwise.

        Raises:
            RuntimeError: If the session owns its client and has been closed.
        """
        async with self._lock:
            # Checked outside the try block; the except clause below would swallow it.
            self._check_not_closed()
            try:
                await self._redis.ping()  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context
                return True
            except Exception:
                return False

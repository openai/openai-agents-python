"""Valkey-powered Session backend.

Valkey is the Linux Foundation's open-source, BSD-licensed, high-performance
key-value store, forked from Redis 7.2.5. It is wire-compatible with Redis.

Usage::

    from agents.extensions.memory import ValkeySession

    # Create from Valkey URL (async – GlideClient.create is async)
    session = await ValkeySession.from_url(
        session_id="user-123",
        url="valkey://localhost:6379/0",
    )

    # Or pass an existing GlideClient that your application already manages
    session = ValkeySession(
        session_id="user-123",
        glide_client=my_glide_client,
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlparse

from ._optional_imports import raise_optional_dependency_error

try:
    from glide import (
        Batch,
        GlideClient,
        GlideClientConfiguration,
        NodeAddress,
        ServerCredentials,
    )
except ImportError as e:
    raise_optional_dependency_error(
        "ValkeySession",
        dependency_name="valkey-glide",
        extra_name="valkey",
        cause=e,
    )

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import SessionSettings, resolve_session_limit


class ValkeySession(SessionABC):
    """Valkey implementation of [`Session`][agents.memory.session.Session].

    Uses the [valkey-glide](https://github.com/valkey-io/valkey-glide) client
    library, which is purpose-built for Valkey and will track Valkey-specific
    features as they are released.
    """

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        glide_client: GlideClient,
        key_prefix: str = "agents:session",
        ttl: int | None = None,
        session_settings: SessionSettings | None = None,
    ):
        """Initializes a new ValkeySession.

        Args:
            session_id (str): Unique identifier for the conversation.
            glide_client (GlideClient): A pre-configured Valkey GlideClient.
            key_prefix (str, optional): Prefix for Valkey keys to avoid collisions.
                Defaults to "agents:session".
            ttl (int | None, optional): Time-to-live in seconds for session data.
                If None, data persists indefinitely. Defaults to None.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
        """
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._glide = glide_client
        self._key_prefix = key_prefix
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._owns_client = False  # Track if we own the GlideClient

        # Valkey key patterns
        self._session_key = f"{self._key_prefix}:{self.session_id}"
        self._messages_key = f"{self._session_key}:messages"
        self._counter_key = f"{self._session_key}:counter"

    @classmethod
    async def from_url(
        cls,
        session_id: str,
        *,
        url: str,
        glide_kwargs: dict[str, Any] | None = None,
        session_settings: SessionSettings | None = None,
        **kwargs: Any,
    ) -> ValkeySession:
        """Create a session from a Valkey/Redis URL string.

        Supports the following URL schemes:

        * ``valkey://`` – plain-text connection to a Valkey server.
        * ``valkeys://`` – TLS-encrypted connection to a Valkey server.
        * ``redis://`` – plain-text connection (compatibility, connects to Valkey).
        * ``rediss://`` – TLS-encrypted connection (compatibility).

        Args:
            session_id (str): Conversation ID.
            url (str): Valkey/Redis URL, e.g. ``"valkey://localhost:6379/0"`` or
                ``"valkeys://host:6380"``.
            glide_kwargs (dict[str, Any] | None): Additional keyword arguments forwarded to
                :class:`GlideClientConfiguration`.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
            **kwargs: Additional keyword arguments forwarded to the main constructor
                (e.g., ``key_prefix``, ``ttl``, etc.).

        Returns:
            ValkeySession: An instance of ValkeySession connected to the specified server.
        """
        parsed = urlparse(url)

        # Determine TLS from scheme
        use_tls = parsed.scheme in ("valkeys", "rediss")

        # Get host and port
        host = parsed.hostname or "localhost"
        port = parsed.port or (6380 if use_tls else 6379)

        # Parse database ID from path
        database_id = None
        if parsed.path and parsed.path not in ("", "/"):
            db_str = parsed.path.lstrip("/")
            try:
                database_id = int(db_str)
            except ValueError:
                pass

        glide_kwargs = glide_kwargs or {}

        # Build configuration
        config_kwargs: dict[str, Any] = {
            "addresses": [NodeAddress(host, port)],
            "use_tls": use_tls,
        }

        if database_id is not None:
            config_kwargs["database_id"] = database_id

        if parsed.username or parsed.password:
            config_kwargs["credentials"] = ServerCredentials(
                password=parsed.password,
                username=parsed.username or None,
            )

        # Merge any additional glide kwargs (user overrides)
        config_kwargs.update(glide_kwargs)

        config = GlideClientConfiguration(**config_kwargs)
        glide_client = await GlideClient.create(config)

        session = cls(
            session_id,
            glide_client=glide_client,
            session_settings=session_settings,
            **kwargs,
        )
        session._owns_client = True  # We created the client, so we own it
        return session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _serialize_item(self, item: TResponseInputItem) -> bytes:
        """Serialize an item to bytes."""
        return json.dumps(item, separators=(",", ":")).encode("utf-8")

    async def _deserialize_item(self, item: bytes) -> TResponseInputItem:
        """Deserialize bytes to an item."""
        return json.loads(item.decode("utf-8"))  # type: ignore[no-any-return]

    async def _get_next_id(self) -> int:
        """Get the next message ID using Valkey INCR for atomic increment."""
        result = await self._glide.incr(self._counter_key)
        return int(result)

    async def _set_ttl_if_configured(self, *keys: str) -> None:
        """Set TTL on keys if configured."""
        if self._ttl is not None:
            for key in keys:
                await self._glide.expire(key, self._ttl)

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, uses session_settings.limit.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        session_limit = resolve_session_limit(limit, self.session_settings)

        async with self._lock:
            if session_limit is None:
                # Get all messages in chronological order
                raw_messages = await self._glide.lrange(self._messages_key, 0, -1)
            else:
                if session_limit <= 0:
                    return []
                # Get the latest N messages (Valkey list is ordered chronologically)
                raw_messages = await self._glide.lrange(
                    self._messages_key, -session_limit, -1
                )

            items: list[TResponseInputItem] = []
            for raw_msg in raw_messages:
                try:
                    item = await self._deserialize_item(raw_msg)
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
            now = str(int(time.time()))

            # Build a non-atomic batch (pipeline) for efficiency
            batch = Batch(is_atomic=False)

            # Set session metadata, preserving created_at across subsequent writes
            batch.hset(self._session_key, {"session_id": self.session_id})
            batch.hsetnx(self._session_key, "created_at", now)

            # Serialize and add all items to the messages list
            serialized_items: list[bytes] = []
            for item in items:
                serialized = await self._serialize_item(item)
                serialized_items.append(serialized)

            if serialized_items:
                batch.rpush(self._messages_key, serialized_items)

            # Update the session timestamp
            batch.hset(self._session_key, {"updated_at": now})

            # Execute all commands in one round-trip
            await self._glide.exec(batch, raise_on_error=False)

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
            while True:
                raw_msg = await self._glide.rpop(self._messages_key)

                if raw_msg is None:
                    return None

                try:
                    return await self._deserialize_item(raw_msg)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Drop corrupted messages and keep looking for a valid item
                    continue

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
            # Delete all keys associated with this session
            await self._glide.delete(
                [
                    self._session_key,
                    self._messages_key,
                    self._counter_key,
                ]
            )

    async def close(self) -> None:
        """Close the Valkey connection.

        Only closes the connection if this session owns the GlideClient
        (i.e., created via :meth:`from_url`). If the client was injected
        externally, the caller is responsible for managing its lifecycle.
        """
        if self._owns_client:
            await self._glide.close()

    async def ping(self) -> bool:
        """Test Valkey connectivity.

        Returns:
            True if Valkey is reachable, False otherwise.
        """
        try:
            await self._glide.ping()
            return True
        except Exception:
            return False

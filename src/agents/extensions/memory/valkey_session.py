"""Valkey-powered Session backend.

Usage::

    from agents.extensions.memory import ValkeySession

    # Create from Valkey URL
    session = ValkeySession.from_url(
        session_id="user-123",
        url="valkey://localhost:6379/0",
    )

    # Or pass an existing GlideClient that your application already manages
    session = ValkeySession(
        session_id="user-123",
        valkey_client=my_glide_client,
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import ParseResult, urlparse

try:
    from glide import (
        Batch,
        GlideClient,
        GlideClientConfiguration,
        NodeAddress,
        ServerCredentials,
    )
except ImportError as e:
    raise ImportError(
        "ValkeySession requires the 'valkey-glide' package. "
        "Install it with: pip install openai-agents[valkey]"
    ) from e

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import SessionSettings, resolve_session_limit


def _parse_valkey_url(url: str) -> dict[str, Any]:
    """Parse a Valkey/Redis-style URL into connection parameters.

    Supports schemes: valkey://, valkeys://, redis://, rediss://.
    The 's' suffix indicates TLS.

    Args:
        url: Connection URL string.

    Returns:
        Dictionary with host, port, db, password, and use_tls keys.
    """
    parsed: ParseResult = urlparse(url)
    scheme = parsed.scheme.lower()

    use_tls = scheme in ("valkeys", "rediss")

    host = parsed.hostname or "localhost"
    port = parsed.port or 6379

    # Extract database number from the path (e.g., /0, /15).
    db: int = 0
    if parsed.path and parsed.path.strip("/"):
        try:
            db = int(parsed.path.strip("/"))
        except ValueError:
            db = 0

    password: str | None = parsed.password

    return {
        "host": host,
        "port": port,
        "db": db,
        "password": password,
        "use_tls": use_tls,
    }


class ValkeySession(SessionABC):
    """Valkey implementation of :pyclass:`agents.memory.session.Session`."""

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        valkey_client: GlideClient,
        key_prefix: str = "agents:session",
        ttl: int | None = None,
        session_settings: SessionSettings | None = None,
    ):
        """Initialise a new ValkeySession.

        Args:
            session_id (str): Unique identifier for the conversation.
            valkey_client (GlideClient): A pre-configured Valkey GLIDE client.
            key_prefix (str, optional): Prefix for Valkey keys to avoid collisions.
                Defaults to "agents:session".
            ttl (int | None, optional): Time-to-live in seconds for session data.
                If None, data persists indefinitely. Defaults to None.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
        """
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._client = valkey_client
        self._key_prefix = key_prefix
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._owns_client = False  # Track if we own the Valkey client.

        # Valkey key patterns.
        self._session_key = f"{self._key_prefix}:{self.session_id}"
        self._messages_key = f"{self._session_key}:messages"
        self._counter_key = f"{self._session_key}:counter"

    @classmethod
    async def from_url(
        cls,
        session_id: str,
        *,
        url: str,
        session_settings: SessionSettings | None = None,
        **kwargs: Any,
    ) -> ValkeySession:
        """Create a session from a Valkey URL string.

        Args:
            session_id (str): Conversation ID.
            url (str): Valkey URL, e.g. "valkey://localhost:6379/0" or "valkeys://host:6380".
                Also accepts "redis://" and "rediss://" schemes for compatibility.
                Note: the database number in the path (e.g. ``/0``) is parsed but ignored
                because GlideClient does not support the ``SELECT`` command.
            session_settings (SessionSettings | None): Session configuration settings including
                default limit for retrieving items. If None, uses default SessionSettings().
            **kwargs: Additional keyword arguments forwarded to the main constructor
                (e.g., key_prefix, ttl, etc.).

        Returns:
            ValkeySession: An instance of ValkeySession connected to the specified Valkey server.
        """
        params = _parse_valkey_url(url)

        addresses = [NodeAddress(params["host"], params["port"])]

        # Build credentials when a password is present in the URL.
        credentials = ServerCredentials(password=params["password"]) if params["password"] else None

        config = GlideClientConfiguration(
            addresses=addresses,
            use_tls=params["use_tls"],
            credentials=credentials,
        )

        client = await GlideClient.create(config)
        session = cls(
            session_id,
            valkey_client=client,
            session_settings=session_settings,
            **kwargs,
        )
        session._owns_client = True  # We created the client, so we own it.
        return session

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to JSON string. Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))

    async def _deserialize_item(self, item: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item. Can be overridden by subclasses."""
        return json.loads(item)  # type: ignore[no-any-return]  # json.loads returns Any but we know the structure

    async def _get_next_id(self) -> int:
        """Get the next message ID using Valkey INCR for atomic increment."""
        result = await self._client.incr(self._counter_key)
        return int(result)

    async def _set_ttl_if_configured(self, *keys: str) -> None:
        """Set TTL on keys if configured, using a pipeline for efficiency."""
        if self._ttl is not None:
            pipe = Batch(is_atomic=False)
            for key in keys:
                pipe.expire(key, self._ttl)
            await self._client.exec(pipe, raise_on_error=True)

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, uses session_settings.limit.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history.
        """
        session_limit = resolve_session_limit(limit, self.session_settings)

        async with self._lock:
            if session_limit is None:
                # Get all messages in chronological order.
                raw_messages = await self._client.lrange(self._messages_key, 0, -1)
            else:
                if session_limit <= 0:
                    return []
                # Get the latest N messages using negative indices.
                raw_messages = await self._client.lrange(self._messages_key, -session_limit, -1)

            items: list[TResponseInputItem] = []
            for raw_msg in raw_messages:
                try:
                    # Handle both bytes (default) and str responses from the client.
                    if isinstance(raw_msg, bytes):
                        msg_str = raw_msg.decode("utf-8")
                    else:
                        msg_str = str(raw_msg)
                    item = await self._deserialize_item(msg_str)
                    items.append(item)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Skip corrupted messages.
                    continue

            return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history.
        """
        if not items:
            return

        async with self._lock:
            # Serialize all items.
            serialized_items: list[str] = []
            for item in items:
                serialized = await self._serialize_item(item)
                serialized_items.append(serialized)

            now = str(int(time.time()))

            # Build a pipeline so all mutations go in a single round-trip.
            pipe = Batch(is_atomic=False)

            # Set session metadata — always update updated_at, set created_at
            # only when the key does not yet exist (via a separate HSETNX-style
            # approach is not available in Batch, so we set both and accept that
            # created_at is overwritten — same behaviour as RedisSession).
            pipe.hset(
                self._session_key,
                {
                    "session_id": self.session_id,
                    "created_at": now,
                    "updated_at": now,
                },
            )

            # Add all items to the messages list.
            if serialized_items:
                pipe.rpush(self._messages_key, serialized_items)

            # Update the session timestamp.
            pipe.hset(self._session_key, {"updated_at": now})

            # Set TTL if configured.
            if self._ttl is not None:
                pipe.expire(self._session_key, self._ttl)
                pipe.expire(self._messages_key, self._ttl)
                pipe.expire(self._counter_key, self._ttl)

            await self._client.exec(pipe, raise_on_error=True)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty.
        """
        async with self._lock:
            # Use RPOP to atomically remove and return the rightmost (most recent) item.
            raw_msg = await self._client.rpop(self._messages_key)

            if raw_msg is None:
                return None

            try:
                # Handle both bytes (default) and str responses from the client.
                if isinstance(raw_msg, bytes):
                    msg_str = raw_msg.decode("utf-8")
                else:
                    msg_str = str(raw_msg)
                return await self._deserialize_item(msg_str)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Return None for corrupted messages (already removed).
                return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
            # Delete all keys associated with this session.
            await self._client.delete([self._session_key, self._messages_key, self._counter_key])

    async def close(self) -> None:
        """Close the Valkey connection.

        Only closes the connection if this session owns the Valkey client
        (i.e., created via from_url). If the client was injected externally,
        the caller is responsible for managing its lifecycle.
        """
        if self._owns_client:
            await self._client.close()

    async def ping(self) -> bool:
        """Test Valkey connectivity.

        Returns:
            True if Valkey is reachable, False otherwise.
        """
        try:
            await self._client.custom_command(["PING"])
            return True
        except Exception:
            return False

"""MongoDB-powered Session backend.

Requires ``pymongo>=4.13``, which ships the native async API
(``AsyncMongoClient``).  Install it with::

    pip install openai-agents[mongodb]

Usage::

    from agents.extensions.memory import MongoDBSession

    # Create from MongoDB URI
    session = MongoDBSession.from_uri(
        session_id="user-123",
        uri="mongodb://localhost:27017",
        database="agents",
    )

    # Or pass an existing AsyncMongoClient that your application already manages
    from pymongo.asynchronous.mongo_client import AsyncMongoClient

    client = AsyncMongoClient("mongodb://localhost:27017")
    session = MongoDBSession(
        session_id="user-123",
        client=client,
        database="agents",
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

try:
    from pymongo.asynchronous.collection import AsyncCollection
    from pymongo.asynchronous.mongo_client import AsyncMongoClient
except ImportError as e:
    raise ImportError(
        "MongoDBSession requires the 'pymongo' package (>=4.13). "
        "Install it with: pip install openai-agents[mongodb]"
    ) from e

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import SessionSettings, resolve_session_limit


class MongoDBSession(SessionABC):
    """MongoDB implementation of :pyclass:`agents.memory.session.Session`.

    Conversation items are stored as individual documents in a ``messages``
    collection.  A lightweight ``sessions`` collection tracks metadata
    (creation time, last-updated time) for each session.

    Indexes are created once per ``(database, sessions_collection,
    messages_collection)`` combination on the first call to any of the
    session protocol methods.  Subsequent calls skip the setup entirely.
    """

    # Class-level registry so index creation only runs once per unique key.
    _initialized_keys: set[tuple[str, str, str]] = set()
    _init_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
    _init_locks_guard: asyncio.Lock = asyncio.Lock()

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        client: AsyncMongoClient,
        database: str = "agents",
        sessions_collection: str = "agent_sessions",
        messages_collection: str = "agent_messages",
        session_settings: SessionSettings | None = None,
    ):
        """Initialize a new MongoDBSession.

        Args:
            session_id: Unique identifier for the conversation.
            client: A pre-configured ``AsyncMongoClient`` instance.
            database: Name of the MongoDB database to use.
                Defaults to ``"agents"``.
            sessions_collection: Name of the collection that stores session
                metadata. Defaults to ``"agent_sessions"``.
            messages_collection: Name of the collection that stores individual
                conversation items. Defaults to ``"agent_messages"``.
            session_settings: Optional session configuration. When ``None`` a
                default :class:`~agents.memory.session_settings.SessionSettings`
                is used (no item limit).
        """
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._client = client
        self._owns_client = False

        db = client[database]
        self._sessions: AsyncCollection = db[sessions_collection]
        self._messages: AsyncCollection = db[messages_collection]

        self._init_key = (database, sessions_collection, messages_collection)

    # ------------------------------------------------------------------
    # Convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_uri(
        cls,
        session_id: str,
        *,
        uri: str,
        database: str = "agents",
        client_kwargs: dict[str, Any] | None = None,
        session_settings: SessionSettings | None = None,
        **kwargs: Any,
    ) -> MongoDBSession:
        """Create a session from a MongoDB URI string.

        Args:
            session_id: Conversation ID.
            uri: MongoDB connection URI,
                e.g. ``"mongodb://localhost:27017"`` or
                ``"mongodb+srv://user:pass@cluster.example.com"``.
            database: Name of the MongoDB database to use.
            client_kwargs: Additional keyword arguments forwarded to
                :class:`pymongo.asynchronous.mongo_client.AsyncMongoClient`.
            session_settings: Optional session configuration settings.
            **kwargs: Additional keyword arguments forwarded to the main
                constructor (e.g. ``sessions_collection``,
                ``messages_collection``).

        Returns:
            A :class:`MongoDBSession` connected to the specified MongoDB server.
        """
        client_kwargs = client_kwargs or {}
        client: AsyncMongoClient = AsyncMongoClient(uri, **client_kwargs)
        session = cls(
            session_id,
            client=client,
            database=database,
            session_settings=session_settings,
            **kwargs,
        )
        session._owns_client = True
        return session

    # ------------------------------------------------------------------
    # Index initialisation
    # ------------------------------------------------------------------

    async def _get_init_lock(self) -> asyncio.Lock:
        """Return (creating if necessary) the per-init-key asyncio Lock."""
        async with self._init_locks_guard:
            lock = self._init_locks.get(self._init_key)
            if lock is None:
                lock = asyncio.Lock()
                self._init_locks[self._init_key] = lock
            return lock

    async def _ensure_indexes(self) -> None:
        """Create required indexes the first time this key is accessed."""
        if self._init_key in self._initialized_keys:
            return

        lock = await self._get_init_lock()
        async with lock:
            # Double-checked locking: another coroutine may have finished first.
            if self._init_key in self._initialized_keys:
                return

            # sessions: unique index on session_id.
            await self._sessions.create_index("session_id", unique=True)

            # messages: compound index for efficient per-session retrieval and sorting.
            await self._messages.create_index([("session_id", 1), ("_id", 1)])

            self._initialized_keys.add(self._init_key)

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    async def _serialize_item(self, item: TResponseInputItem) -> str:
        """Serialize an item to a JSON string. Can be overridden by subclasses."""
        return json.dumps(item, separators=(",", ":"))

    async def _deserialize_item(self, raw: str) -> TResponseInputItem:
        """Deserialize a JSON string to an item. Can be overridden by subclasses."""
        return json.loads(raw)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. When ``None``, the
                effective limit is taken from :attr:`session_settings`.
                If that is also ``None``, all items are returned.
                The returned list is always in chronological (oldest-first)
                order.

        Returns:
            List of input items representing the conversation history.
        """
        await self._ensure_indexes()

        session_limit = resolve_session_limit(limit, self.session_settings)

        if session_limit is not None and session_limit <= 0:
            return []

        query = {"session_id": self.session_id}

        if session_limit is None:
            cursor = self._messages.find(query).sort("_id", 1)
            docs = await cursor.to_list()
        else:
            # Fetch the latest N documents in reverse order, then reverse the
            # list to restore chronological order.
            cursor = self._messages.find(query).sort("_id", -1).limit(session_limit)
            docs = await cursor.to_list()
            docs.reverse()

        items: list[TResponseInputItem] = []
        for doc in docs:
            try:
                items.append(await self._deserialize_item(doc["message_data"]))
            except (json.JSONDecodeError, KeyError):
                # Skip corrupted or malformed documents.
                continue

        return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to append to the session.
        """
        if not items:
            return

        await self._ensure_indexes()

        # Upsert the session metadata document.
        await self._sessions.update_one(
            {"session_id": self.session_id},
            {"$setOnInsert": {"session_id": self.session_id}},
            upsert=True,
        )

        payload = [
            {
                "session_id": self.session_id,
                "message_data": await self._serialize_item(item),
            }
            for item in items
        ]

        await self._messages.insert_many(payload, ordered=True)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, ``None`` if the session is empty.
        """
        await self._ensure_indexes()

        doc = await self._messages.find_one_and_delete(
            {"session_id": self.session_id},
            sort=[("_id", -1)],
        )

        if doc is None:
            return None

        try:
            return await self._deserialize_item(doc["message_data"])
        except (json.JSONDecodeError, KeyError):
            return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        await self._ensure_indexes()
        await self._messages.delete_many({"session_id": self.session_id})
        await self._sessions.delete_one({"session_id": self.session_id})

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying MongoDB connection.

        Only closes the client if this session owns it (i.e. it was created
        via :meth:`from_uri`).  If the client was injected externally the
        caller is responsible for managing its lifecycle.
        """
        if self._owns_client:
            await self._client.aclose()

    async def ping(self) -> bool:
        """Test MongoDB connectivity.

        Returns:
            ``True`` if the server is reachable, ``False`` otherwise.
        """
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            return False

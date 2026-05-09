"""MongoDB-powered Session backend.

Requires ``pymongo>=4.14``, which ships the native async API
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

import json
import threading
import weakref
from datetime import datetime, timezone
from typing import Any

try:
    from importlib.metadata import version as _get_version

    _VERSION: str | None = _get_version("openai-agents")
except Exception:
    _VERSION = None

try:
    from pymongo.asynchronous.collection import AsyncCollection
    from pymongo.asynchronous.mongo_client import AsyncMongoClient
    from pymongo.driver_info import DriverInfo
except ImportError as e:
    raise ImportError(
        "MongoDBSession requires the 'pymongo' package (>=4.14). "
        "Install it with: pip install openai-agents[mongodb]"
    ) from e

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import SessionSettings, resolve_session_limit

# Identifies this library in the MongoDB handshake for server-side telemetry.
_DRIVER_INFO = DriverInfo(name="openai-agents", version=_VERSION)


class MongoDBSession(SessionABC):
    """MongoDB implementation of :pyclass:`agents.memory.session.Session`.

    Conversation items are stored as individual documents in a ``messages``
    collection.  A lightweight ``sessions`` collection tracks metadata
    (creation time, last-updated time) for each session.

    Indexes are created once per ``(client, database, sessions_collection,
    messages_collection)`` combination on the first call to any of the
    session protocol methods.  Subsequent calls skip the setup entirely.

    Each message document carries a ``seq`` field — an integer assigned by
    atomically incrementing a counter on the session metadata document.  This
    guarantees a strictly monotonic insertion order that is safe across
    multiple writers and processes, unlike sorting by ``_id`` / ObjectId which
    is only second-level accurate and non-monotonic across machines.
    """

    # Class-level registry so index creation runs only once per unique
    # (client, database, sessions_collection, messages_collection) combination.
    #
    # Design notes:
    # - Keyed on id(client) so two distinct AsyncMongoClient objects that happen
    #   to compare equal (same host/port) never share a cache entry.  A
    #   weakref.finalize callback removes the entry when the client is GC'd,
    #   preventing stale id() values from being reused by a future client.
    # - Only a threading.Lock (never an asyncio.Lock) touches the registry.
    #   asyncio.Lock is bound to the event loop that first acquires it; reusing
    #   one across loops raises RuntimeError.  create_index is idempotent, so
    #   we only need the threading lock to guard the boolean done flag — no
    #   async coordination is required.
    _init_state: dict[int, dict[tuple[str, str, str], bool]] = {}
    _init_guard: threading.Lock = threading.Lock()

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        client: AsyncMongoClient[Any],
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

        client.append_metadata(_DRIVER_INFO)

        db = client[database]
        self._sessions: AsyncCollection[Any] = db[sessions_collection]
        self._messages: AsyncCollection[Any] = db[messages_collection]

        self._client_id = id(client)
        self._init_sub_key = (database, sessions_collection, messages_collection)

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
        client_kwargs.setdefault("driver", _DRIVER_INFO)
        client: AsyncMongoClient[Any] = AsyncMongoClient(uri, **client_kwargs)
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

    def _is_init_done(self) -> bool:
        """Return True if indexes have already been created for this (client, sub_key)."""
        with self._init_guard:
            per_client = self._init_state.get(self._client_id)
            return per_client is not None and per_client.get(self._init_sub_key, False)

    def _mark_init_done(self) -> None:
        """Record that index creation is complete for this (client, sub_key)."""
        with self._init_guard:
            per_client = self._init_state.get(self._client_id)
            if per_client is None:
                per_client = {}
                self._init_state[self._client_id] = per_client
                # Register the cleanup finalizer exactly once per client identity,
                # not once per session, to avoid unbounded growth when many
                # sessions share a single long-lived client.
                weakref.finalize(self._client, self._init_state.pop, self._client_id, None)
            per_client[self._init_sub_key] = True

    async def _ensure_indexes(self) -> None:
        """Create required indexes the first time this (client, sub_key) is accessed.

        ``create_index`` is idempotent on the server side, so concurrent calls
        from different coroutines or event loops are safe — at most a redundant
        round-trip is issued.  The threading-lock-guarded boolean prevents that
        extra round-trip after the first call completes.
        """
        if self._is_init_done():
            return

        # sessions: unique index on session_id.
        await self._sessions.create_index("session_id", unique=True)

        # messages: compound index for efficient per-session retrieval and
        # sorting by the explicit seq counter.
        await self._messages.create_index([("session_id", 1), ("seq", 1)])

        self._mark_init_done()

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
            cursor = self._messages.find(query).sort("seq", 1)
            docs = await cursor.to_list()
        else:
            # Fetch the latest N documents in reverse order, then reverse the
            # list to restore chronological order.
            cursor = self._messages.find(query).sort("seq", -1).limit(session_limit)
            docs = await cursor.to_list()
            docs.reverse()

        items: list[TResponseInputItem] = []
        for doc in docs:
            try:
                items.append(await self._deserialize_item(doc["message_data"]))
            except (json.JSONDecodeError, KeyError, TypeError):
                # Skip corrupted or malformed documents (including non-string BSON values).
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

        now = datetime.now(timezone.utc)

        # Atomically reserve a block of sequence numbers for this batch.
        # $inc returns the new value, so subtract len(items) to get the first
        # number in the block.
        result = await self._sessions.find_one_and_update(
            {"session_id": self.session_id},
            {
                "$setOnInsert": {"session_id": self.session_id, "created_at": now},
                "$set": {"updated_at": now},
                "$inc": {"_seq": len(items)},
            },
            upsert=True,
            return_document=True,
        )
        next_seq: int = (result["_seq"] if result else len(items)) - len(items)

        payload = [
            {
                "session_id": self.session_id,
                "seq": next_seq + i,
                "message_data": await self._serialize_item(item),
            }
            for i, item in enumerate(items)
        ]

        await self._messages.insert_many(payload, ordered=True)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, ``None`` if the session is empty.

        Corrupt documents (invalid JSON, missing/non-string ``message_data``)
        are silently discarded and the next-most-recent item is returned.  This
        matches :meth:`get_items`, which also skips corrupt documents, so a
        single bad row cannot make a non-empty session look empty to callers.
        """
        await self._ensure_indexes()

        while True:
            doc = await self._messages.find_one_and_delete(
                {"session_id": self.session_id},
                sort=[("seq", -1)],
            )
            if doc is None:
                return None
            try:
                return await self._deserialize_item(doc["message_data"])
            except (json.JSONDecodeError, KeyError, TypeError):
                # Corrupt — drop it and try the next-most-recent document.
                continue

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
            await self._client.close()

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

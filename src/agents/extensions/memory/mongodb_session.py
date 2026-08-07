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

import asyncio
import json
import threading
import weakref
from datetime import datetime, timezone
from typing import Any, ClassVar

from ._optional_imports import raise_optional_dependency_error

try:
    from importlib.metadata import version as _get_version

    _VERSION: str | None = _get_version("openai-agents")
except Exception:
    _VERSION = None

try:
    from pymongo.asynchronous.collection import AsyncCollection
    from pymongo.asynchronous.mongo_client import AsyncMongoClient
    from pymongo.driver_info import DriverInfo
    from pymongo.read_preferences import ReadPreference
except ImportError as e:
    raise_optional_dependency_error(
        "MongoDBSession",
        dependency_name="mongodb",
        extra_name="mongodb",
        cause=e,
    )

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import (
    SessionSettings,
    coerce_session_settings,
    resolve_session_limit,
)
from ...memory.sqlite_session import _await_mutation

# Identifies this library in the MongoDB handshake for server-side telemetry.
_DRIVER_INFO = DriverInfo(name="openai-agents", version=_VERSION)


class MongoDBSession(SessionABC):
    """MongoDB implementation of [`Session`][agents.memory.session.Session].

    Conversation items are stored as logical-batch documents in a ``messages``
    collection. Legacy per-item documents remain readable. A lightweight
    ``sessions`` collection tracks metadata (creation time, last-updated time)
    for each session. Each logical batch must fit within MongoDB's single-document
    size limit; an oversized batch fails atomically without storing a partial batch.

    Indexes are created once per ``(client, database, sessions_collection,
    messages_collection)`` combination on the first call to any of the
    session protocol methods.  Subsequent calls skip the setup entirely.

    Each message document carries a ``seq`` field for the final item in that
    document. Sequence ranges are assigned by atomically incrementing a counter
    on the session metadata document. This guarantees a strictly monotonic
    insertion order that is safe across multiple writers and processes, unlike
    sorting by ``_id`` / ObjectId which is only second-level accurate and
    non-monotonic across machines.
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
    _init_state: ClassVar[dict[int, dict[tuple[str, str, str], bool]]] = {}
    _init_guard: ClassVar[threading.Lock] = threading.Lock()

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        client: AsyncMongoClient[Any],
        database: str = "agents",
        sessions_collection: str = "agent_sessions",
        messages_collection: str = "agent_messages",
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ):
        """Initialize a new MongoDBSession.

        Args:
            session_id: Unique identifier for the conversation.
            client: A pre-configured ``AsyncMongoClient`` instance.
            database: Name of the MongoDB database to use.
                Defaults to ``"agents"``.
            sessions_collection: Name of the collection that stores session
                metadata. Defaults to ``"agent_sessions"``.
            messages_collection: Name of the collection that stores logical
                conversation batches and legacy per-item records. Defaults to
                ``"agent_messages"``.
            session_settings: Optional session configuration. When ``None`` a
                default [`SessionSettings`][agents.memory.session_settings.SessionSettings]
                is used (no item limit).
        """
        self.session_id = session_id
        self.session_settings = (
            coerce_session_settings(session_settings)
            if session_settings is not None
            else SessionSettings()
        )
        self._client = client
        self._owns_client = False
        self._closed = False

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
        session_settings: SessionSettings | dict[str, Any] | None = None,
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
                `pymongo.asynchronous.mongo_client.AsyncMongoClient`.
            session_settings: Optional session configuration settings.
            **kwargs: Additional keyword arguments forwarded to the main
                constructor (e.g. ``sessions_collection``,
                ``messages_collection``).

        Returns:
            A [`MongoDBSession`][agents.extensions.memory.mongodb_session.MongoDBSession]
                connected to the specified MongoDB server.
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

    def _check_not_closed(self) -> None:
        """Raise if the session has already been closed."""
        if self._closed:
            raise RuntimeError("MongoDBSession is closed")

    async def _ensure_indexes(self) -> None:
        """Create required indexes the first time this (client, sub_key) is accessed.

        ``create_index`` is idempotent on the server side, so concurrent calls
        from different coroutines or event loops are safe — at most a redundant
        round-trip is issued.  The threading-lock-guarded boolean prevents that
        extra round-trip after the first call completes.

        Session operations that require index initialization go through here, so
        this is also where they reject a closed session. The empty ``add_items``
        fast path and ``ping`` check the closed state directly.
        """
        self._check_not_closed()

        if self._is_init_done():
            return

        # sessions: unique index on session_id.
        await self._sessions.create_index("session_id", unique=True)

        # messages: compound index for efficient active-generation retrieval
        # and sorting by the explicit seq counter.
        await self._messages.create_index([("session_id", 1), ("generation", 1), ("seq", 1)])

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

    async def _get_generation(self) -> int:
        """Return the authoritative history generation for this session."""
        sessions = self._sessions.with_options(read_preference=ReadPreference.PRIMARY)
        docs = await sessions.find({"session_id": self.session_id}).limit(1).to_list()
        if not docs:
            return 0
        generation = docs[0].get("_generation", 0)
        return generation if isinstance(generation, int) else 0

    def _generation_query(self, generation: int) -> dict[str, Any]:
        """Match the active generation while retaining legacy generation-zero data."""
        if generation == 0:
            return {"generation": {"$in": [0, None]}}
        return {"generation": generation}

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

        generation = await self._get_generation()
        query = {
            "session_id": self.session_id,
            **self._generation_query(generation),
        }

        async def _decode_docs(docs: list[Any]) -> list[TResponseInputItem]:
            items: list[TResponseInputItem] = []
            for doc in docs:
                raw = doc.get("message_data")
                raw_items = raw if isinstance(raw, list) else [raw]
                for raw_item in raw_items:
                    try:
                        items.append(await self._deserialize_item(raw_item))
                    except (json.JSONDecodeError, TypeError):
                        # Skip corrupted or malformed entries, including legacy non-string values.
                        continue
            return items

        if session_limit is None:
            cursor = self._messages.find(query).sort("seq", 1)
            return await _decode_docs(await cursor.to_list())

        # Fetch the latest N documents in reverse order, then reverse the
        # list to restore chronological order. Expand the fetch window when corrupt
        # documents sit among the newest entries so limit counts valid conversation
        # items, matching pop_item and the SQLite backends.
        window = session_limit
        while True:
            cursor = self._messages.find(query).sort("seq", -1).limit(window)
            docs = await cursor.to_list()
            items = await _decode_docs(docs[::-1])
            if len(items) >= session_limit:
                return items[-session_limit:]
            if len(docs) < window:
                return items
            window *= 2

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items and wait until the batch outcome is known."""
        self._check_not_closed()
        if not items:
            return
        await self._ensure_indexes()
        serialized_items = [await self._serialize_item(item) for item in items]
        await _await_mutation(self._add_items(serialized_items))

    async def _add_items(self, serialized_items: list[str]) -> None:
        """Store one pre-serialized logical batch."""
        now = datetime.now(timezone.utc)

        # Atomically reserve a block of sequence numbers for this batch.
        # $inc returns the new value, so subtract the batch size to get the
        # first number in the block.
        result = await self._sessions.find_one_and_update(
            {"session_id": self.session_id},
            {
                "$setOnInsert": {
                    "session_id": self.session_id,
                    "created_at": now,
                    "_generation": 0,
                },
                "$set": {"updated_at": now},
                "$inc": {"_seq": len(serialized_items)},
            },
            upsert=True,
            return_document=True,
        )
        next_seq: int = (result["_seq"] if result else len(serialized_items)) - len(
            serialized_items
        )
        generation = result.get("_generation", 0) if result else 0
        if not isinstance(generation, int):
            generation = 0

        # One document is the commit boundary for the logical batch. This keeps
        # standalone MongoDB deployments failure-atomic without requiring transactions.
        await self._messages.insert_one(
            {
                "session_id": self.session_id,
                "seq": next_seq + len(serialized_items) - 1,
                "generation": generation,
                "message_data": serialized_items,
            }
        )

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove the most recent item after the destructive claim settles."""
        await self._ensure_indexes()
        return await _await_mutation(self._pop_item())

    async def _pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, ``None`` if the session is empty.

        New list-valued logical batches and legacy string-valued items are both
        supported. Malformed entries (invalid JSON, missing ``message_data``, or
        other value types) are silently discarded and the next-most-recent item
        is returned. This matches :meth:`get_items`, which also skips malformed
        entries, so one bad record cannot make a non-empty session look empty.
        """
        generation = await self._get_generation()

        # Retry cleanup left by a prior post-claim failure. Empty markers are
        # never model-visible or claimable, so cleanup failure must not block a
        # later valid tail claim.
        try:
            await self._messages.delete_many(
                {
                    "session_id": self.session_id,
                    **self._generation_query(generation),
                    "message_data": [],
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

        while True:
            doc = await self._messages.find_one_and_update(
                {
                    "session_id": self.session_id,
                    **self._generation_query(generation),
                    "message_data": {"$ne": []},
                },
                [
                    {
                        "$set": {
                            "message_data": {
                                "$cond": [
                                    {"$isArray": "$message_data"},
                                    {
                                        "$slice": [
                                            "$message_data",
                                            {
                                                "$subtract": [
                                                    {"$size": "$message_data"},
                                                    1,
                                                ]
                                            },
                                        ]
                                    },
                                    [],
                                ]
                            }
                        }
                    }
                ],
                sort=[("seq", -1)],
                return_document=False,
            )
            if doc is None:
                current_generation = await self._get_generation()
                if current_generation != generation:
                    generation = current_generation
                    continue
                return None

            current_generation = await self._get_generation()
            if current_generation != generation:
                try:
                    await self._messages.delete_one({"_id": doc["_id"]})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                generation = current_generation
                continue
            raw = doc.get("message_data")

            if isinstance(raw, list):
                if not raw:
                    continue
                claimed_raw = raw[-1]
                exhausted = len(raw) == 1
            else:
                claimed_raw = raw
                exhausted = True

            if exhausted:
                # The atomic claim above leaves an empty marker so another pop cannot
                # claim the same item. Remove that marker before returning to avoid
                # accumulating exhausted logical-batch and legacy documents.
                try:
                    await self._messages.delete_one({"_id": doc["_id"], "message_data": []})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The item is already claimed. Do not turn a known destructive
                    # outcome into a retry-visible failure; the next pop retries the
                    # best-effort empty-marker sweep above.
                    pass

            try:
                return await self._deserialize_item(claimed_raw)
            except (json.JSONDecodeError, TypeError):
                # Corrupt — drop it and try the next-most-recent document.
                continue

    async def clear_session(self) -> None:
        """Clear history after the authoritative delete settles."""
        await self._ensure_indexes()
        await _await_mutation(self._clear_session())

    async def _clear_session(self) -> None:
        """Advance the authoritative generation and clean obsolete history."""
        now = datetime.now(timezone.utc)
        result = await self._sessions.find_one_and_update(
            {"session_id": self.session_id},
            {
                "$setOnInsert": {
                    "session_id": self.session_id,
                    "created_at": now,
                    "_seq": 0,
                },
                "$set": {"updated_at": now},
                "$inc": {"_generation": 1},
            },
            upsert=True,
            return_document=True,
        )
        generation = result.get("_generation", 1) if result else 1
        if not isinstance(generation, int):
            generation = 1

        # The metadata update above is the single-document clear boundary.
        # Obsolete batches are no longer visible, so physical deletion is best effort.
        try:
            await self._messages.delete_many(
                {
                    "session_id": self.session_id,
                    "$or": [
                        {"generation": {"$lt": generation}},
                        {"generation": {"$exists": False}},
                    ],
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying MongoDB connection.

        Only closes the client if this session owns it (i.e. it was created
        via :meth:`from_uri`).  In that case the session becomes terminal and
        subsequent operations raise ``RuntimeError``.  If the client was injected
        externally the caller is responsible for managing its lifecycle and this
        is a no-op.

        The session is terminal from the first close attempt.  If releasing the
        client fails or is cancelled, operations still raise and a later close()
        retries the release, which ``AsyncMongoClient.close`` allows.
        """
        if not self._owns_client:
            return

        self._closed = True
        await self._client.close()

    async def ping(self) -> bool:
        """Test MongoDB connectivity.

        Returns:
            ``True`` if the server is reachable, ``False`` otherwise.

        Raises:
            RuntimeError: If the session owns its client and has been closed.
        """
        # Checked outside the try block; the except clause below would swallow it.
        self._check_not_closed()
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            return False

"""Dakera-powered Session backend.

[Dakera](https://github.com/dakera-ai/dakera-deploy) is a self-hosted memory
server for AI agents that exposes a REST API for persistent, decay-weighted
memory. This backend stores each conversation item as a memory in a per-session
Dakera namespace, so session history survives process restarts and can be shared
across workers that point at the same Dakera server.

Usage::

    from agents.extensions.memory import DakeraSession

    # Create and own a client from connection details
    session = DakeraSession.from_url(
        session_id="user-123",
        base_url="http://localhost:3000",
        api_key="dk-...",
    )

    # Or pass an ``AsyncDakeraClient`` your application already manages
    from dakera import AsyncDakeraClient

    client = AsyncDakeraClient(base_url="http://localhost:3000", api_key="dk-...")
    session = DakeraSession(session_id="user-123", client=client)

    await Runner.run(agent, "Hello", session=session)

Run the server locally with the ``dakera-ai/dakera-deploy`` docker-compose stack
(Dakera server + MinIO); it listens on port 3000 by default.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from ._optional_imports import raise_optional_dependency_error

try:
    from dakera import AsyncDakeraClient
except ImportError as e:
    raise_optional_dependency_error(
        "DakeraSession",
        dependency_name="dakera",
        extra_name="dakera",
        cause=e,
    )

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import SessionSettings, resolve_session_limit

_DEFAULT_KEY_PREFIX = "agents:session"
_SEQ_KEY = "seq"
_SESSION_TAG = "oai-session"


class DakeraSession(SessionABC):
    """Dakera implementation of [`Session`][agents.memory.session.Session].

    Each conversation item is persisted as an individual memory in a Dakera
    namespace derived from ``session_id`` (``"{key_prefix}:{session_id}"``).
    Isolating every session in its own namespace keeps histories separate and
    makes ``get_items``/``pop_item``/``clear_session`` restart-safe: the same
    ``session_id`` always resolves to the same stored history regardless of the
    process that wrote it.
    """

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        *,
        client: AsyncDakeraClient,
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        importance: float = 0.6,
        session_settings: SessionSettings | None = None,
    ):
        """Initialize a new DakeraSession.

        Args:
            session_id: Unique identifier for the conversation.
            client: A pre-configured ``AsyncDakeraClient`` pointed at a Dakera
                server.
            key_prefix: Prefix used to build the per-session Dakera namespace,
                avoiding collisions with other agents on the same server.
                Defaults to ``"agents:session"``.
            importance: Importance score (0.0-1.0) attached to each stored item.
                Defaults to ``0.6`` to keep conversation turns above Dakera's
                decay floor without crowding higher-importance memories.
            session_settings: Session configuration (e.g. default retrieval
                limit). If None, uses default ``SessionSettings()``.
        """
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._client = client
        self._key_prefix = key_prefix
        self._namespace = f"{key_prefix}:{session_id}"
        self._importance = importance
        self._lock = asyncio.Lock()
        self._owns_client = False
        self._next_seq: int | None = None

    @classmethod
    def from_url(
        cls,
        session_id: str,
        *,
        base_url: str,
        api_key: str | None = None,
        client_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DakeraSession:
        """Create a session from Dakera connection details.

        The created ``AsyncDakeraClient`` is owned by the session and closed by
        :meth:`close`.

        Args:
            session_id: Conversation ID.
            base_url: Base URL of the Dakera server, e.g. ``"http://localhost:3000"``.
            api_key: Optional API key for authentication.
            client_kwargs: Additional keyword arguments forwarded to
                ``AsyncDakeraClient`` (e.g. ``timeout``, ``max_retries``).
            **kwargs: Additional keyword arguments forwarded to the main
                constructor (e.g. ``key_prefix``, ``importance``).

        Returns:
            A ``DakeraSession`` that owns its underlying client.
        """
        client = AsyncDakeraClient(base_url=base_url, api_key=api_key, **(client_kwargs or {}))
        session = cls(session_id, client=client, **kwargs)
        session._owns_client = True
        return session

    # ------------------------------------------------------------------
    # Serialization helpers (overridable by subclasses)
    # ------------------------------------------------------------------

    def _serialize_item(self, item: TResponseInputItem) -> str:
        return json.dumps(item, separators=(",", ":"))

    def _deserialize_item(self, raw: str) -> TResponseInputItem:
        return json.loads(raw)  # type: ignore[no-any-return]

    @staticmethod
    def _seq_of(memory: dict[str, Any]) -> int:
        metadata = memory.get("metadata") or {}
        seq = metadata.get(_SEQ_KEY)
        if isinstance(seq, bool):  # bool is an int subclass; treat as unset
            return 0
        if isinstance(seq, int | float):
            return int(seq)
        return 0

    async def _fetch_sorted(self) -> list[dict[str, Any]]:
        """Return this session's memories in chronological (write) order."""
        memories = await self._client.agent_memories(self._namespace)
        return sorted(memories, key=lambda m: (self._seq_of(m), m.get("created_at") or ""))

    async def _ensure_next_seq(self) -> int:
        """Lazily seed the monotonic sequence counter from stored history."""
        if self._next_seq is None:
            memories = await self._client.agent_memories(self._namespace)
            max_seq = max((self._seq_of(m) for m in memories), default=-1)
            self._next_seq = max_seq + 1
        return self._next_seq

    # ------------------------------------------------------------------
    # Session protocol implementation
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, uses
                ``session_settings.limit``. When specified, returns the latest N
                items in chronological order.

        Returns:
            List of input items representing the conversation history.
        """
        session_limit = resolve_session_limit(limit, self.session_settings)
        if session_limit is not None and session_limit <= 0:
            return []

        async with self._lock:
            memories = await self._fetch_sorted()

        items: list[TResponseInputItem] = []
        for memory in memories:
            raw = memory.get("content")
            if not isinstance(raw, str):
                continue
            try:
                items.append(self._deserialize_item(raw))
            except json.JSONDecodeError:
                # Skip corrupted entries rather than failing the whole read.
                continue

        if session_limit is None:
            return items
        return items[-session_limit:]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history.
        """
        if not items:
            return

        async with self._lock:
            seq = await self._ensure_next_seq()
            for item in items:
                await self._client.store_memory(
                    agent_id=self._namespace,
                    content=self._serialize_item(item),
                    memory_type="episodic",
                    importance=self._importance,
                    metadata={_SEQ_KEY: seq, "oai_session_id": self.session_id},
                    tags=[_SESSION_TAG],
                )
                seq += 1
            self._next_seq = seq

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty.
        """
        async with self._lock:
            memories = await self._fetch_sorted()
            while memories:
                memory = memories.pop()
                memory_id = memory.get("id")
                if memory_id:
                    with contextlib.suppress(Exception):
                        await self._client.forget(self._namespace, memory_id)
                raw = memory.get("content")
                if not isinstance(raw, str):
                    continue
                try:
                    return self._deserialize_item(raw)
                except json.JSONDecodeError:
                    # Corrupted entry already removed; keep looking for a valid one.
                    continue
            return None

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        async with self._lock:
            memories = await self._client.agent_memories(self._namespace)
            for memory in memories:
                memory_id = memory.get("id")
                if memory_id:
                    with contextlib.suppress(Exception):
                        await self._client.forget(self._namespace, memory_id)
            self._next_seq = 0

    async def close(self) -> None:
        """Close the underlying Dakera client.

        Only closes the client when this session created it (via
        :meth:`from_url`). If the client was injected externally, the caller is
        responsible for its lifecycle.
        """
        if self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.close()

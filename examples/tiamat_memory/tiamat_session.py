"""TIAMAT-powered Session backend for persistent cross-session memory.

TIAMAT (https://memory.tiamat.live) provides a free, cloud-based memory API
with full-text search, knowledge triples, and persistent storage for AI agents.

Usage::

    from tiamat_session import TiamatSession

    session = TiamatSession(
        session_id="user-123",
        api_key="your-tiamat-api-key",
    )

    await Runner.run(agent, "Hello", session=session)

Get a free API key::

    import httpx
    resp = httpx.post("https://memory.tiamat.live/api/keys/register",
                      json={"agent_name": "my-agent", "purpose": "memory"})
    api_key = resp.json()["api_key"]
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from agents.items import TResponseInputItem
from agents.memory.session import SessionABC
from agents.memory.session_settings import SessionSettings, resolve_session_limit


TIAMAT_BASE_URL = "https://memory.tiamat.live"


class TiamatSession(SessionABC):
    """TIAMAT Memory API implementation of the Session protocol.

    Stores conversation history as tagged memories in TIAMAT's cloud memory service,
    providing persistent cross-session and cross-device agent memory with full-text search.
    """

    def __init__(
        self,
        session_id: str,
        *,
        api_key: str,
        base_url: str = TIAMAT_BASE_URL,
        session_settings: SessionSettings | None = None,
    ):
        """Initialize a TiamatSession.

        Args:
            session_id: Unique identifier for the conversation session.
            api_key: TIAMAT API key (get one free at /api/keys/register).
            base_url: Base URL for the TIAMAT Memory API.
            session_settings: Session configuration settings including
                default limit for retrieving items.
        """
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
            timeout=30.0,
        )
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        session_id: str,
        *,
        agent_name: str = "openai-agents",
        purpose: str = "session memory",
        base_url: str = TIAMAT_BASE_URL,
        session_settings: SessionSettings | None = None,
    ) -> TiamatSession:
        """Create a TiamatSession with auto-registered API key.

        Args:
            session_id: Unique identifier for the conversation session.
            agent_name: Name to register the API key under.
            purpose: Purpose description for the API key.
            base_url: Base URL for the TIAMAT Memory API.
            session_settings: Session configuration settings.

        Returns:
            A configured TiamatSession with a freshly registered API key.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/api/keys/register",
                json={"agent_name": agent_name, "purpose": purpose},
            )
            resp.raise_for_status()
            api_key = resp.json()["api_key"]

        return cls(
            session_id,
            api_key=api_key,
            base_url=base_url,
            session_settings=session_settings,
        )

    def _make_tag(self) -> str:
        """Generate the session-specific tag for memory storage."""
        return f"session:{self.session_id}"

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve conversation history from TIAMAT memory.

        Args:
            limit: Maximum number of items to retrieve. If None, uses session_settings.limit.

        Returns:
            List of input items representing the conversation history.
        """
        session_limit = resolve_session_limit(limit, self.session_settings)

        async with self._lock:
            return await self._get_items_unlocked(session_limit)

    async def _fetch_active_items_unlocked(self) -> list[tuple[int, dict[str, Any]]]:
        """Parse memories, applying clear and pop tombstone filters.

        Returns a list of ``(seq, item)`` pairs sorted by sequence number,
        with internal markers (clear, pop) already filtered out.

        Must be called while holding ``self._lock``.
        """
        resp = await self._client.post(
            "/api/memory/recall",
            json={
                "query": self._make_tag(),
                "limit": 10000,
            },
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        memories = data.get("memories", [])

        last_clear_seq: int = -1
        popped_seqs: set[int] = set()

        # First pass: parse all items, collect clear and pop markers
        parsed: list[tuple[int, dict[str, Any]]] = []
        for memory in memories:
            content = memory.get("content", "")
            try:
                item = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue

            seq = item.get("_tiamat_seq", 0)

            if item.get("_tiamat_clear"):
                last_clear_seq = max(last_clear_seq, seq)
                continue

            if item.get("_tiamat_popped"):
                popped_seqs.add(item.get("_tiamat_popped_seq", -1))
                continue

            parsed.append((seq, item))

        # Second pass: keep only items after the last clear and not popped
        active: list[tuple[int, dict[str, Any]]] = []
        for seq, item in parsed:
            if seq > last_clear_seq and seq not in popped_seqs:
                active.append((seq, item))

        # Sort by insertion order
        active.sort(key=lambda pair: pair[0])
        return active

    async def _get_items_unlocked(
        self, session_limit: int | None
    ) -> list[TResponseInputItem]:
        """Internal item retrieval — must be called while holding ``self._lock``."""
        active = await self._fetch_active_items_unlocked()

        items: list[TResponseInputItem] = []
        for _seq, item in active:
            item.pop("_tiamat_seq", None)
            items.append(item)

        if session_limit is not None and session_limit > 0:
            items = items[-session_limit:]

        return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Store conversation items in TIAMAT memory.

        Args:
            items: List of input items to add to the history.
        """
        if not items:
            return

        async with self._lock:
            # Get current max sequence number
            seq = await self._next_seq_unlocked()

            for item in items:
                # Add sequence number for ordering
                item_with_seq = {**item, "_tiamat_seq": seq}
                seq += 1

                await self._client.post(
                    "/api/memory/store",
                    json={
                        "content": json.dumps(item_with_seq, separators=(",", ":")),
                        "tags": [self._make_tag(), f"seq:{seq}"],
                        "importance": 0.7,
                    },
                )

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Persists a tombstone marker so the item is excluded from future reads.
        This ensures ``rewind_session_items`` terminates correctly.

        Returns:
            The most recent item if it exists, None if the session is empty.
        """
        async with self._lock:
            active = await self._fetch_active_items_unlocked()
            if not active:
                return None

            last_seq, last_item = active[-1]

            # Store a tombstone so this item is permanently excluded
            await self._client.post(
                "/api/memory/store",
                json={
                    "content": json.dumps({
                        "_tiamat_popped": True,
                        "_tiamat_popped_seq": last_seq,
                        "_tiamat_ts": time.time(),
                    }),
                    "tags": [self._make_tag(), "pop_marker"],
                    "importance": 0.0,
                },
            )

            # Remove internal metadata before returning
            last_item.pop("_tiamat_seq", None)
            return last_item

    async def clear_session(self) -> None:
        """Clear all items for this session.

        Stores a clear marker with a sequence number higher than all existing
        items, so subsequent reads filter out everything before it.
        """
        async with self._lock:
            clear_seq = await self._next_seq_unlocked()
            await self._client.post(
                "/api/memory/store",
                json={
                    "content": json.dumps({
                        "_tiamat_clear": True,
                        "_tiamat_seq": clear_seq,
                        "_tiamat_ts": time.time(),
                    }),
                    "tags": [self._make_tag(), "clear_marker"],
                    "importance": 1.0,
                },
            )

    async def _next_seq_unlocked(self) -> int:
        """Compute the next available sequence number.

        Scans all raw memories (including markers) to find the highest
        ``_tiamat_seq`` and returns one past it. Must be called while
        holding ``self._lock``.
        """
        resp = await self._client.post(
            "/api/memory/recall",
            json={"query": self._make_tag(), "limit": 10000},
        )
        if resp.status_code != 200:
            return 0

        max_seq = -1
        for memory in resp.json().get("memories", []):
            try:
                item = json.loads(memory.get("content", ""))
                seq = item.get("_tiamat_seq", 0)
                if seq > max_seq:
                    max_seq = seq
            except (json.JSONDecodeError, TypeError):
                continue
        return max_seq + 1

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def ping(self) -> bool:
        """Test TIAMAT API connectivity.

        Returns:
            True if the API is reachable, False otherwise.
        """
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200
        except Exception:
            return False

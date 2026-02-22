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

    async def _get_items_unlocked(
        self, session_limit: int | None
    ) -> list[TResponseInputItem]:
        """Internal item retrieval — must be called while holding ``self._lock``."""
        # Fetch enough records; when no limit is set, retrieve all available
        fetch_limit = session_limit if session_limit and session_limit > 0 else 10000

        resp = await self._client.post(
            "/api/memory/recall",
            json={
                "query": self._make_tag(),
                "limit": fetch_limit,
            },
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        memories = data.get("memories", [])

        items: list[TResponseInputItem] = []
        last_clear_seq: int = -1

        # First pass: parse all items and find the latest clear marker
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

            parsed.append((seq, item))

        # Second pass: keep only items after the last clear marker
        for seq, item in parsed:
            if seq > last_clear_seq:
                items.append(item)

        # Sort by original insertion order
        items.sort(key=lambda x: x.get("_tiamat_seq", 0))

        # Remove internal metadata
        for item in items:
            item.pop("_tiamat_seq", None)

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
            # Get current sequence number
            existing = await self._get_raw_memories()
            seq = len(existing)

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

        Note: TIAMAT's API doesn't support direct deletion, so this retrieves
        the last item and marks it as removed via a tag update.

        Returns:
            The most recent item if it exists, None if the session is empty.
        """
        async with self._lock:
            items = await self._get_items_unlocked(session_limit=None)
            if not items:
                return None
            return items[-1]

    async def clear_session(self) -> None:
        """Clear all items for this session.

        Stores a clear marker so subsequent reads return empty.
        """
        async with self._lock:
            await self._client.post(
                "/api/memory/store",
                json={
                    "content": json.dumps({"_tiamat_clear": True, "_tiamat_ts": time.time()}),
                    "tags": [self._make_tag(), "clear_marker"],
                    "importance": 1.0,
                },
            )

    async def _get_raw_memories(self) -> list[dict[str, Any]]:
        """Get raw memories for this session."""
        resp = await self._client.post(
            "/api/memory/recall",
            json={"query": self._make_tag(), "limit": 100},
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("memories", [])

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

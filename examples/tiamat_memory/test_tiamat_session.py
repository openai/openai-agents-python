"""Tests for TiamatSession — verifies SessionABC contract compliance.

These tests use a mock HTTP transport so they run without network access.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tiamat_session import TiamatSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MemoryStore:
    """In-memory mock of the TIAMAT Memory API for testing."""

    def __init__(self) -> None:
        self.memories: list[dict[str, Any]] = []

    def store(self, content: str, tags: list[str], importance: float) -> dict[str, Any]:
        entry = {"content": content, "tags": tags, "importance": importance}
        self.memories.append(entry)
        return {"status": "stored"}

    def recall(self, query: str, limit: int) -> dict[str, Any]:
        matched = [m for m in self.memories if query in str(m.get("tags", []))]
        return {"memories": matched[:limit]}


class MockTransport(httpx.AsyncBaseTransport):
    """httpx transport backed by an in-memory MemoryStore."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/health":
            return httpx.Response(200, json={"status": "healthy"})

        if path == "/api/memory/store":
            body = json.loads(request.content)
            result = self.store.store(body["content"], body["tags"], body["importance"])
            return httpx.Response(200, json=result)

        if path == "/api/memory/recall":
            body = json.loads(request.content)
            result = self.store.recall(body["query"], body.get("limit", 100))
            return httpx.Response(200, json=result)

        return httpx.Response(404, json={"error": "not found"})


def make_session(store: MemoryStore | None = None) -> tuple[TiamatSession, MemoryStore]:
    """Create a TiamatSession wired to an in-memory store."""
    if store is None:
        store = MemoryStore()
    transport = MockTransport(store)
    session = TiamatSession(
        session_id="test-session",
        api_key="test-key",
        base_url="https://memory.tiamat.live",
    )
    # Replace the real HTTP client with our mock transport
    session._client = httpx.AsyncClient(
        transport=transport,
        base_url="https://memory.tiamat.live",
        headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
        timeout=30.0,
    )
    return session, store


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_and_get_items():
    session, _ = make_session()
    items = [_msg("user", "hello"), _msg("assistant", "hi")]
    await session.add_items(items)

    result = await session.get_items()
    assert len(result) == 2
    assert result[0]["content"] == "hello"
    assert result[1]["content"] == "hi"
    await session.close()


@pytest.mark.asyncio
async def test_pop_item_is_destructive():
    """pop_item must remove the returned item — calling it twice on a
    single-item session must return None the second time."""
    session, _ = make_session()
    await session.add_items([_msg("user", "only item")])

    popped = await session.pop_item()
    assert popped is not None
    assert popped["content"] == "only item"

    # Second pop — item is gone
    popped_again = await session.pop_item()
    assert popped_again is None

    # get_items also sees empty
    items = await session.get_items()
    assert len(items) == 0
    await session.close()


@pytest.mark.asyncio
async def test_pop_item_multiple():
    """pop_item removes items one at a time from the tail."""
    session, _ = make_session()
    await session.add_items([
        _msg("user", "first"),
        _msg("assistant", "second"),
        _msg("user", "third"),
    ])

    p3 = await session.pop_item()
    assert p3 is not None
    assert p3["content"] == "third"

    p2 = await session.pop_item()
    assert p2 is not None
    assert p2["content"] == "second"

    remaining = await session.get_items()
    assert len(remaining) == 1
    assert remaining[0]["content"] == "first"
    await session.close()


@pytest.mark.asyncio
async def test_clear_session_hides_all_items():
    """clear_session must hide all existing items regardless of their
    sequence numbers."""
    session, _ = make_session()
    await session.add_items([
        _msg("user", "msg1"),
        _msg("assistant", "msg2"),
        _msg("user", "msg3"),
    ])

    items_before = await session.get_items()
    assert len(items_before) == 3

    await session.clear_session()

    items_after = await session.get_items()
    assert len(items_after) == 0

    # New items after clear are visible
    await session.add_items([_msg("user", "fresh")])
    items_fresh = await session.get_items()
    assert len(items_fresh) == 1
    assert items_fresh[0]["content"] == "fresh"
    await session.close()


@pytest.mark.asyncio
async def test_pop_after_clear():
    """pop_item on an empty (cleared) session returns None."""
    session, _ = make_session()
    await session.add_items([_msg("user", "old")])
    await session.clear_session()

    popped = await session.pop_item()
    assert popped is None
    await session.close()


@pytest.mark.asyncio
async def test_sequence_numbers_exceed_100():
    """Sequence numbers must remain monotonic even past 100 items,
    verifying the _get_raw_memories limit fix."""
    session, _ = make_session()

    # Add 105 items
    batch = [_msg("user", f"msg-{i}") for i in range(105)]
    await session.add_items(batch)

    items = await session.get_items()
    assert len(items) == 105

    # Verify last item is correct
    assert items[-1]["content"] == "msg-104"

    # pop_item should return the last one
    popped = await session.pop_item()
    assert popped is not None
    assert popped["content"] == "msg-104"

    items_after = await session.get_items()
    assert len(items_after) == 104
    await session.close()


@pytest.mark.asyncio
async def test_pop_item_no_internal_metadata():
    """Popped items must not contain internal _tiamat_* keys."""
    session, _ = make_session()
    await session.add_items([_msg("user", "test")])

    popped = await session.pop_item()
    assert popped is not None
    assert "_tiamat_seq" not in popped
    assert "_tiamat_popped" not in popped
    assert "_tiamat_clear" not in popped
    await session.close()


@pytest.mark.asyncio
async def test_ping():
    session, _ = make_session()
    assert await session.ping() is True
    await session.close()

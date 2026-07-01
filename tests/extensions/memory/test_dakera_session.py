"""Tests for DakeraSession using an in-process fake Dakera client.

All tests run without a real Dakera server by injecting a lightweight fake
``AsyncDakeraClient`` that emulates the three REST operations the session relies
on (``store_memory``, ``agent_memories``, ``forget``). This keeps the suite fast
and network-free while exercising the full session logic.
"""

from __future__ import annotations

import itertools
from typing import Any, cast
from unittest.mock import patch

import pytest

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory import DakeraSession
from agents.extensions.memory.dakera_session import DakeraSession as DakeraSessionDirect
from agents.memory.session_settings import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

pytestmark = pytest.mark.asyncio


class FakeDakeraClient:
    """In-memory stand-in for ``dakera.AsyncDakeraClient``.

    Stores memories per ``agent_id`` (namespace) and returns them in a
    deliberately non-chronological order so the session's own sequence-based
    sorting is what's under test.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._created = itertools.count(1)
        self.closed = False

    async def store_memory(
        self,
        agent_id: str,
        content: str,
        memory_type: str = "episodic",
        importance: float | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        memory = {
            "id": f"mem-{next(self._ids)}",
            "content": content,
            "memory_type": memory_type,
            "importance": importance,
            "metadata": dict(metadata or {}),
            "created_at": f"2026-01-01T00:00:{next(self._created):02d}Z",
            "tags": list(tags or []),
        }
        self._store.setdefault(agent_id, []).append(memory)
        return memory

    async def agent_memories(
        self,
        agent_id: str,
        memory_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [dict(m) for m in self._store.get(agent_id, [])]
        # Return reversed to prove DakeraSession does not rely on server order.
        return list(reversed(rows))

    async def forget(self, agent_id: str, memory_id: str) -> dict[str, Any]:
        rows = self._store.get(agent_id, [])
        self._store[agent_id] = [m for m in rows if m.get("id") != memory_id]
        return {"forgotten": memory_id}

    async def close(self) -> None:
        self.closed = True


def _message(role: str, text: str) -> TResponseInputItem:
    return cast("TResponseInputItem", {"role": role, "content": text})


@pytest.fixture
def client() -> FakeDakeraClient:
    return FakeDakeraClient()


@pytest.fixture
def session(client: FakeDakeraClient) -> DakeraSession:
    return DakeraSession("conv-1", client=client)  # type: ignore[arg-type]


@pytest.fixture
def agent() -> Agent:
    return Agent(name="test", model=FakeModel())


# ---------------------------------------------------------------------------
# Core protocol behavior
# ---------------------------------------------------------------------------


async def test_add_and_get_items_roundtrip(session: DakeraSession) -> None:
    items: list[TResponseInputItem] = [
        _message("user", "Hello"),
        _message("assistant", "Hi there"),
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert retrieved == items


async def test_items_are_chronological_across_add_calls(session: DakeraSession) -> None:
    await session.add_items([_message("user", "first")])
    await session.add_items([_message("assistant", "second")])
    await session.add_items([_message("user", "third")])

    retrieved = await session.get_items()
    assert retrieved == [
        _message("user", "first"),
        _message("assistant", "second"),
        _message("user", "third"),
    ]


async def test_add_empty_list_is_noop(session: DakeraSession) -> None:
    await session.add_items([])
    assert await session.get_items() == []


async def test_get_items_empty_session(session: DakeraSession) -> None:
    assert await session.get_items() == []


async def test_get_items_with_explicit_limit(session: DakeraSession) -> None:
    await session.add_items([_message("user", f"m{i}") for i in range(5)])

    latest = await session.get_items(limit=2)
    assert latest == [_message("user", "m3"), _message("user", "m4")]


async def test_get_items_limit_zero(session: DakeraSession) -> None:
    await session.add_items([_message("user", "m0")])
    assert await session.get_items(limit=0) == []


async def test_get_items_limit_exceeds_count(session: DakeraSession) -> None:
    await session.add_items([_message("user", "only")])
    assert len(await session.get_items(limit=10)) == 1


async def test_session_settings_limit_used_as_default(client: FakeDakeraClient) -> None:
    session = DakeraSession(
        "conv-limit",
        client=client,  # type: ignore[arg-type]
        session_settings=SessionSettings(limit=1),
    )
    await session.add_items([_message("user", "a"), _message("assistant", "b")])

    retrieved = await session.get_items()
    assert retrieved == [_message("assistant", "b")]


async def test_explicit_limit_overrides_session_settings(client: FakeDakeraClient) -> None:
    session = DakeraSession(
        "conv-limit-2",
        client=client,  # type: ignore[arg-type]
        session_settings=SessionSettings(limit=1),
    )
    await session.add_items([_message("user", "a"), _message("assistant", "b")])
    assert len(await session.get_items(limit=2)) == 2


# ---------------------------------------------------------------------------
# pop_item / clear_session
# ---------------------------------------------------------------------------


async def test_pop_item_returns_last(session: DakeraSession) -> None:
    await session.add_items([_message("user", "first"), _message("assistant", "second")])

    popped = await session.pop_item()
    assert popped == _message("assistant", "second")

    remaining = await session.get_items()
    assert remaining == [_message("user", "first")]


async def test_pop_then_add_keeps_order(session: DakeraSession) -> None:
    await session.add_items([_message("user", "a"), _message("assistant", "b")])
    await session.pop_item()
    await session.add_items([_message("assistant", "c")])

    assert await session.get_items() == [_message("user", "a"), _message("assistant", "c")]


async def test_pop_item_empty_session(session: DakeraSession) -> None:
    assert await session.pop_item() is None


async def test_clear_session(session: DakeraSession) -> None:
    await session.add_items([_message("user", "a"), _message("assistant", "b")])
    await session.clear_session()
    assert await session.get_items() == []


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


async def test_sessions_are_isolated(client: FakeDakeraClient) -> None:
    session_a = DakeraSession("conv-a", client=client)  # type: ignore[arg-type]
    session_b = DakeraSession("conv-b", client=client)  # type: ignore[arg-type]

    await session_a.add_items([_message("user", "for a")])
    await session_b.add_items([_message("user", "for b")])

    assert await session_a.get_items() == [_message("user", "for a")]
    assert await session_b.get_items() == [_message("user", "for b")]


async def test_clear_does_not_affect_other_sessions(client: FakeDakeraClient) -> None:
    session_a = DakeraSession("conv-a", client=client)  # type: ignore[arg-type]
    session_b = DakeraSession("conv-b", client=client)  # type: ignore[arg-type]
    await session_a.add_items([_message("user", "for a")])
    await session_b.add_items([_message("user", "for b")])

    await session_a.clear_session()
    assert await session_a.get_items() == []
    assert len(await session_b.get_items()) == 1


async def test_custom_key_prefix_changes_namespace(client: FakeDakeraClient) -> None:
    session = DakeraSession("conv-1", client=client, key_prefix="tenant-x")  # type: ignore[arg-type]
    await session.add_items([_message("user", "hi")])
    assert "tenant-x:conv-1" in client._store


# ---------------------------------------------------------------------------
# Serialization fidelity & corruption handling
# ---------------------------------------------------------------------------


async def test_unicode_and_special_characters_roundtrip(session: DakeraSession) -> None:
    item = _message("user", 'emoji 🎉 and "quotes" and \\backslash and \n newline')
    await session.add_items([item])
    assert (await session.get_items())[0] == item


async def test_complex_item_shape_roundtrip(session: DakeraSession) -> None:
    item = cast(
        "TResponseInputItem",
        {
            "type": "function_call",
            "call_id": "call_123",
            "name": "get_weather",
            "arguments": '{"city": "SF"}',
        },
    )
    await session.add_items([item])
    assert (await session.get_items())[0] == item


async def test_corrupted_entry_is_skipped(session: DakeraSession, client: FakeDakeraClient) -> None:
    await session.add_items([_message("user", "good")])
    # Inject a corrupted memory directly into the namespace.
    client._store[session._namespace].append(
        {
            "id": "mem-corrupt",
            "content": "not-json{",
            "metadata": {"seq": 99},
            "created_at": "2026-01-01T00:00:99Z",
        }
    )
    retrieved = await session.get_items()
    assert retrieved == [_message("user", "good")]


async def test_pop_skips_corrupt_most_recent(
    session: DakeraSession, client: FakeDakeraClient
) -> None:
    await session.add_items([_message("user", "good")])
    client._store[session._namespace].append(
        {
            "id": "mem-corrupt",
            "content": "not-json{",
            "metadata": {"seq": 99},
            "created_at": "2026-01-01T00:00:99Z",
        }
    )
    popped = await session.pop_item()
    assert popped == _message("user", "good")
    # Both the corrupt entry and the popped good entry are gone.
    assert await session.get_items() == []


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------


async def test_external_client_not_closed(client: FakeDakeraClient) -> None:
    session = DakeraSession("conv-1", client=client)  # type: ignore[arg-type]
    await session.close()
    assert client.closed is False


async def test_from_url_owns_and_closes_client() -> None:
    fake = FakeDakeraClient()
    with patch(
        "agents.extensions.memory.dakera_session.AsyncDakeraClient",
        return_value=fake,
    ) as mock_ctor:
        session = DakeraSessionDirect.from_url(
            "conv-1",
            base_url="http://localhost:3000",
            api_key="dk-test",
        )
    mock_ctor.assert_called_once()
    assert session._owns_client is True
    await session.close()
    assert fake.closed is True


# ---------------------------------------------------------------------------
# End-to-end Runner integration
# ---------------------------------------------------------------------------


async def test_runner_integration(session: DakeraSession, agent: Agent) -> None:
    assert isinstance(agent.model, FakeModel)

    agent.model.set_next_output([get_text_message("San Francisco")])
    result1 = await Runner.run(agent, "Where is the Golden Gate Bridge?", session=session)
    assert result1.final_output == "San Francisco"

    agent.model.set_next_output([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"

    # user + assistant for each of the two turns
    assert len(await session.get_items()) == 4


async def test_runner_session_isolation(client: FakeDakeraClient, agent: Agent) -> None:
    assert isinstance(agent.model, FakeModel)
    session_a = DakeraSession("conv-a", client=client)  # type: ignore[arg-type]
    session_b = DakeraSession("conv-b", client=client)  # type: ignore[arg-type]

    agent.model.set_next_output([get_text_message("I like cats.")])
    await Runner.run(agent, "Remember: I like cats.", session=session_a)

    assert len(await session_a.get_items()) == 2
    assert await session_b.get_items() == []

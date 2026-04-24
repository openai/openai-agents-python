"""Unit tests for ValkeySession.

These tests verify the ValkeySession logic using a lightweight mock client
that stores data in plain Python dicts.  They do NOT test the real
valkey-glide wire protocol — see ``test_valkey_integration.py`` for that.

The mock is intentionally thin: each Valkey command is backed by a simple
in-memory implementation so that the *session* logic (serialisation,
key layout, TTL calls, ownership tracking, etc.) is exercised against
realistic return values without over-mocking individual call sites.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("glide")  # Skip the whole module when valkey-glide is absent.

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory.valkey_session import ValkeySession, _parse_valkey_url
from agents.memory import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Serial marker keeps these off the xdist parallel workers.
pytestmark = [pytest.mark.asyncio, pytest.mark.serial]


# ---------------------------------------------------------------------------
# Lightweight in-memory Valkey stub
# ---------------------------------------------------------------------------


def _make_stub_client() -> AsyncMock:
    """Return an ``AsyncMock`` that behaves like a minimal ``GlideClient``.

    Every Valkey command used by ``ValkeySession`` is backed by a trivial
    Python implementation so that the session's own logic is the thing
    under test — not the mock wiring.

    Pipeline (``Batch``) execution is handled by replaying the batch's
    recorded commands against the same in-memory store.  The opcode
    mapping is intentionally minimal — only the commands that
    ``ValkeySession`` actually pipelines are supported.
    """
    client = AsyncMock()
    store: dict[str, list[bytes]] = {}
    hashes: dict[str, dict[str, str]] = {}

    def _do_rpush(key: str, values: list[str]) -> int:
        store.setdefault(key, [])
        for v in values:
            store[key].append(v.encode())
        return len(store[key])

    def _do_hset(key: str, mapping: dict[str, str]) -> int:
        hashes.setdefault(key, {}).update(mapping)
        return len(mapping)

    def _do_expire(_key: str, _seconds: int) -> bool:
        return True

    # -- async wrappers for commands called outside of pipelines ------

    async def _lrange(key: str, start: int, end: int) -> list[bytes]:
        lst = store.get(key, [])
        if not lst:
            return []
        n = len(lst)
        if start < 0:
            start = max(n + start, 0)
        if end < 0:
            end = n + end
        return lst[start : end + 1]

    async def _rpop(key: str) -> bytes | None:
        lst = store.get(key, [])
        return lst.pop() if lst else None

    async def _delete(keys: list[str]) -> int:
        removed = 0
        for k in keys:
            removed += int(k in store) + int(k in hashes)
            store.pop(k, None)
            hashes.pop(k, None)
        return removed

    async def _incr(key: str) -> int:
        tag = f"__ctr__{key}"
        store.setdefault(tag, [b"0"])
        val = int(store[tag][0]) + 1
        store[tag] = [str(val).encode()]
        return val

    async def _custom_command(args: list[str]) -> str:
        return "PONG" if args and args[0].upper() == "PING" else "OK"

    # -- batch (pipeline) replay --------------------------------------
    # Batch.commands is a list of (opcode, [arg, ...]) tuples.
    # We only map the three opcodes ValkeySession actually pipelines.

    from glide import Batch

    _sample = Batch(is_atomic=False)
    _sample.hset("_", {"_": "_"})
    _OP_HSET = _sample.commands[0][0]
    _sample.clear()
    _sample.rpush("_", ["_"])
    _OP_RPUSH = _sample.commands[0][0]
    _sample.clear()
    _sample.expire("_", 1)
    _OP_EXPIRE = _sample.commands[0][0]
    del _sample

    async def _exec(batch: Any, raise_on_error: bool = True) -> list[Any]:
        results: list[Any] = []
        for opcode, args in batch.commands:
            if opcode == _OP_HSET:
                key, fields = args[0], dict(zip(args[1::2], args[2::2], strict=False))
                results.append(_do_hset(key, fields))
            elif opcode == _OP_RPUSH:
                results.append(_do_rpush(args[0], list(args[1:])))
            elif opcode == _OP_EXPIRE:
                results.append(_do_expire(args[0], int(args[1])))
            else:
                results.append(None)
        return results

    client.lrange = AsyncMock(side_effect=_lrange)
    client.rpop = AsyncMock(side_effect=_rpop)
    client.delete = AsyncMock(side_effect=_delete)
    client.incr = AsyncMock(side_effect=_incr)
    client.custom_command = AsyncMock(side_effect=_custom_command)
    client.exec = AsyncMock(side_effect=_exec)
    client.close = AsyncMock()

    # Expose internals so tests can inject corrupted data.
    client._store = store
    client._hashes = hashes
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub() -> AsyncMock:
    """Fresh stub client for every test."""
    return _make_stub_client()


@pytest.fixture
def agent() -> Agent:
    return Agent(name="test", model=FakeModel())


def _session(
    session_id: str,
    client: AsyncMock,
    *,
    key_prefix: str = "test:",
    ttl: int | None = None,
    session_settings: SessionSettings | None = None,
) -> ValkeySession:
    """Shorthand factory — no async needed for the constructor path."""
    return ValkeySession(
        session_id=session_id,
        valkey_client=client,
        key_prefix=key_prefix,
        ttl=ttl,
        session_settings=session_settings,
    )


# ===================================================================
# Core CRUD
# ===================================================================


async def test_add_get_pop_clear(stub: AsyncMock):
    """Full lifecycle: add → get → pop → clear."""
    s = _session("crud", stub)

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await s.add_items(items)

    got = await s.get_items()
    assert len(got) == 2
    assert got[0].get("content") == "Hello"
    assert got[1].get("content") == "Hi there!"

    popped = await s.pop_item()
    assert popped is not None and popped.get("content") == "Hi there!"
    assert len(await s.get_items()) == 1

    await s.clear_session()
    assert await s.get_items() == []


async def test_add_empty_list_is_noop(stub: AsyncMock):
    """Adding an empty list must not touch Valkey at all."""
    s = _session("noop", stub)
    await s.add_items([])
    # No pipeline should have been executed.
    stub.exec.assert_not_called()


async def test_pop_from_empty_returns_none(stub: AsyncMock):
    s = _session("empty", stub)
    assert await s.pop_item() is None


# ===================================================================
# Limit / SessionSettings
# ===================================================================


async def test_get_items_with_explicit_limit(stub: AsyncMock):
    s = _session("limit", stub)
    await s.add_items([{"role": "user", "content": str(i)} for i in range(6)])

    last3 = await s.get_items(limit=3)
    assert [it.get("content") for it in last3] == ["3", "4", "5"]

    all_items = await s.get_items()
    assert len(all_items) == 6

    assert await s.get_items(limit=0) == []
    assert len(await s.get_items(limit=100)) == 6  # More than available.


async def test_session_settings_limit_used_as_default(stub: AsyncMock):
    s = _session("ss", stub, session_settings=SessionSettings(limit=2))
    await s.add_items([{"role": "user", "content": str(i)} for i in range(5)])

    got = await s.get_items()  # No explicit limit → uses settings.
    assert len(got) == 2
    assert got[0].get("content") == "3"
    assert got[1].get("content") == "4"


async def test_explicit_limit_overrides_session_settings(stub: AsyncMock):
    s = _session("override", stub, session_settings=SessionSettings(limit=10))
    await s.add_items([{"role": "user", "content": str(i)} for i in range(10)])

    got = await s.get_items(limit=2)
    assert len(got) == 2
    assert got[0].get("content") == "8"


# ===================================================================
# TTL
# ===================================================================


async def test_ttl_session_uses_pipeline(stub: AsyncMock):
    """When ttl is set, add_items must call exec (pipeline) and the data must land."""
    s = _session("ttl", stub, ttl=300)
    await s.add_items([{"role": "user", "content": "hi"}])

    # The pipeline was executed.
    stub.exec.assert_called()
    # And the data actually landed in the store.
    got = await s.get_items()
    assert len(got) == 1
    assert got[0].get("content") == "hi"


async def test_no_ttl_session_still_works(stub: AsyncMock):
    """Without ttl the pipeline still executes and data lands correctly."""
    s = _session("nottl", stub, ttl=None)
    await s.add_items([{"role": "user", "content": "hi"}])

    got = await s.get_items()
    assert len(got) == 1


# ===================================================================
# Key isolation
# ===================================================================


async def test_different_session_ids_are_isolated(stub: AsyncMock):
    s1 = _session("a", stub)
    s2 = _session("b", stub)

    await s1.add_items([{"role": "user", "content": "from a"}])
    await s2.add_items([{"role": "user", "content": "from b"}])

    assert len(await s1.get_items()) == 1
    assert (await s1.get_items())[0].get("content") == "from a"
    assert (await s2.get_items())[0].get("content") == "from b"


async def test_different_key_prefixes_are_isolated(stub: AsyncMock):
    s1 = _session("same", stub, key_prefix="app1")
    s2 = _session("same", stub, key_prefix="app2")

    await s1.add_items([{"role": "user", "content": "app1"}])
    await s2.add_items([{"role": "user", "content": "app2"}])

    assert (await s1.get_items())[0].get("content") == "app1"
    assert (await s2.get_items())[0].get("content") == "app2"


# ===================================================================
# Client ownership & close()
# ===================================================================


async def test_external_client_not_closed(stub: AsyncMock):
    """When the caller provides the client, close() must not shut it down."""
    s = _session("ext", stub)
    assert s._owns_client is False
    await s.close()
    stub.close.assert_not_called()


async def test_owned_client_closed():
    """Clients created via from_url must be closed when the session is closed."""
    with (
        patch("agents.extensions.memory.valkey_session.GlideClient") as MockGlide,
        patch("agents.extensions.memory.valkey_session.ServerCredentials"),
    ):
        mock = _make_stub_client()
        MockGlide.create = AsyncMock(return_value=mock)

        s = await ValkeySession.from_url("owned", url="valkey://localhost:6379/0")
        assert s._owns_client is True
        await s.close()
        mock.close.assert_called_once()


# ===================================================================
# ping()
# ===================================================================


async def test_ping_success(stub: AsyncMock):
    s = _session("ping", stub)
    assert await s.ping() is True


async def test_ping_failure(stub: AsyncMock):
    s = _session("ping_fail", stub)
    stub.custom_command = AsyncMock(side_effect=ConnectionError("gone"))
    assert await s.ping() is False


# ===================================================================
# Corrupted data handling
# ===================================================================


async def test_get_items_skips_corrupted_json(stub: AsyncMock):
    s = _session("corrupt_get", stub)
    await s.add_items([{"role": "user", "content": "ok"}])

    # Inject garbage directly into the backing store.
    stub._store[s._messages_key].append(b"not json")
    stub._store[s._messages_key].append(b"{broken")

    items = await s.get_items()
    assert len(items) == 1
    assert items[0].get("content") == "ok"


async def test_pop_returns_none_for_corrupted_item(stub: AsyncMock):
    s = _session("corrupt_pop", stub)
    # Push raw garbage — pop should return None, not raise.
    stub._store.setdefault(s._messages_key, []).append(b"%%%")
    assert await s.pop_item() is None


# ===================================================================
# Data integrity — unicode, special chars
# ===================================================================


async def test_unicode_roundtrip(stub: AsyncMock):
    s = _session("uni", stub)
    await s.add_items(
        [
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "😊👍"},
            {"role": "user", "content": "Привет"},
        ]
    )
    got = await s.get_items()
    assert got[0].get("content") == "こんにちは"
    assert got[1].get("content") == "😊👍"
    assert got[2].get("content") == "Привет"


async def test_special_characters_roundtrip(stub: AsyncMock):
    payloads = [
        "O'Reilly",
        '{"nested": "json"}',
        'Quote: "Hello world"',
        "Line1\nLine2\tTabbed",
        "Robert'); DROP TABLE students;--",
        "\\n\\t\\r literal backslashes",
    ]
    s = _session("special", stub)
    items: list[TResponseInputItem] = [{"role": "user", "content": p} for p in payloads]
    await s.add_items(items)

    got = await s.get_items()
    for expected, actual in zip(payloads, got, strict=False):
        assert actual.get("content") == expected


# ===================================================================
# Concurrent access
# ===================================================================


async def test_concurrent_add_items(stub: AsyncMock):
    import asyncio

    s = _session("conc", stub)

    async def _add(start: int, n: int) -> None:
        await s.add_items([{"role": "user", "content": f"m{start + i}"} for i in range(n)])

    await asyncio.gather(_add(0, 5), _add(5, 5), _add(10, 5))

    got = await s.get_items()
    assert len(got) == 15
    contents = {str(it.get("content")) for it in got}
    assert contents == {f"m{i}" for i in range(15)}


# ===================================================================
# Runner integration (with FakeModel, no real LLM)
# ===================================================================


async def test_runner_preserves_history(stub: AsyncMock, agent: Agent):
    s = _session("runner", stub)
    model = agent.model
    assert isinstance(model, FakeModel)

    model.set_next_output([get_text_message("San Francisco")])
    r1 = await Runner.run(agent, "Where is the Golden Gate Bridge?", session=s)
    assert r1.final_output == "San Francisco"

    model.set_next_output([get_text_message("California")])
    r2 = await Runner.run(agent, "What state?", session=s)
    assert r2.final_output == "California"

    # The second turn should have received the first turn's history.
    last_input = model.last_turn_args["input"]
    assert any("Golden Gate Bridge" in str(it.get("content", "")) for it in last_input)


async def test_runner_session_settings_override(stub: AsyncMock):
    from agents import RunConfig

    s = _session("run_ss", stub, session_settings=SessionSettings(limit=100))
    await s.add_items([{"role": "user", "content": f"Turn {i}"} for i in range(10)])

    model = FakeModel()
    ag = Agent(name="test", model=model)
    model.set_next_output([get_text_message("Got it")])

    await Runner.run(
        ag,
        "New question",
        session=s,
        run_config=RunConfig(session_settings=SessionSettings(limit=2)),
    )

    history = [it for it in model.last_turn_args["input"] if it.get("content") != "New question"]
    assert len(history) == 2


# ===================================================================
# from_url / URL parsing
# ===================================================================


async def test_from_url_creates_session():
    with (
        patch("agents.extensions.memory.valkey_session.GlideClient") as MockGlide,
        patch("agents.extensions.memory.valkey_session.ServerCredentials"),
    ):
        MockGlide.create = AsyncMock(return_value=_make_stub_client())
        s = await ValkeySession.from_url("u", url="valkey://localhost:6379/0")
        assert s.session_id == "u"
        assert s._owns_client is True
        await s.close()


async def test_from_url_forwards_session_settings():
    with (
        patch("agents.extensions.memory.valkey_session.GlideClient") as MockGlide,
        patch("agents.extensions.memory.valkey_session.ServerCredentials"),
    ):
        MockGlide.create = AsyncMock(return_value=_make_stub_client())
        s = await ValkeySession.from_url(
            "ss", url="valkey://h:6379/0", session_settings=SessionSettings(limit=7)
        )
        assert s.session_settings is not None and s.session_settings.limit == 7
        await s.close()


async def test_from_url_with_username():
    """from_url should forward the username to ServerCredentials for ACL auth."""
    with (
        patch("agents.extensions.memory.valkey_session.GlideClient") as MockGlide,
        patch("agents.extensions.memory.valkey_session.ServerCredentials") as MockCreds,
    ):
        MockGlide.create = AsyncMock(return_value=_make_stub_client())
        s = await ValkeySession.from_url("u", url="valkey://alice:secret@localhost:6379/0")
        MockCreds.assert_called_once_with(password="secret", username="alice")
        assert s._owns_client is True
        await s.close()


async def test_from_url_rejects_nonzero_db():
    """from_url should raise ValueError when the URL specifies a non-zero database."""
    with pytest.raises(ValueError, match="does not support database selection"):
        await ValkeySession.from_url("u", url="valkey://localhost:6379/5")


def test_parse_url_basic():
    r = _parse_valkey_url("valkey://localhost:6379/0")
    assert (r["host"], r["port"], r["db"]) == ("localhost", 6379, 0)
    assert r["password"] is None and r["use_tls"] is False
    assert r["username"] is None


def test_parse_url_password_and_db():
    r = _parse_valkey_url("valkey://:secret@host:6380/5")
    assert r["password"] == "secret"
    assert r["host"] == "host"
    assert r["port"] == 6380
    assert r["db"] == 5


def test_parse_url_username_and_password():
    r = _parse_valkey_url("valkey://alice:secret@host:6379/0")
    assert r["username"] == "alice"
    assert r["password"] == "secret"
    assert r["host"] == "host"


def test_parse_url_tls_schemes():
    assert _parse_valkey_url("valkeys://h:6379/0")["use_tls"] is True
    assert _parse_valkey_url("rediss://h:6379/0")["use_tls"] is True
    assert _parse_valkey_url("redis://h:6379/0")["use_tls"] is False


def test_parse_url_defaults():
    r = _parse_valkey_url("valkey://myhost")
    assert (r["host"], r["port"], r["db"]) == ("myhost", 6379, 0)


def test_parse_url_invalid_scheme():
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        _parse_valkey_url("http://localhost:6379/0")
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        _parse_valkey_url("ftp://localhost:6379/0")


# ===================================================================
# _get_next_id (internal helper)
# ===================================================================


async def test_get_next_id_sequential(stub: AsyncMock):
    s = _session("ctr", stub)
    assert await s._get_next_id() == 1
    assert await s._get_next_id() == 2
    assert await s._get_next_id() == 3


# ===================================================================
# SessionSettings.resolve (pure logic, no Valkey)
# ===================================================================


def test_session_settings_resolve():
    base = SessionSettings(limit=100)
    assert base.resolve(SessionSettings(limit=50)).limit == 50
    assert base.resolve(None).limit == 100
    assert base.limit == 100  # Original unchanged.

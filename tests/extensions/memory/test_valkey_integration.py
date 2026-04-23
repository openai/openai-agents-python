"""Integration tests for ValkeySession with a real Valkey server using testcontainers.

These tests use a ``valkey/valkey-bundle`` Docker container to exercise every
public method of ``ValkeySession`` against a live server.  They are
automatically **skipped** when Docker is not available or when ``valkey-glide``
/ ``testcontainers`` are not installed, so they never break ``make tests`` on
machines without Docker.

Run explicitly with::

    pytest tests/extensions/memory/test_valkey_integration.py -v
"""

from __future__ import annotations

import asyncio
import shutil
import sys

import pytest
import pytest_asyncio

pytest.importorskip("glide")  # Skip when valkey-glide is not installed.
pytest.importorskip("testcontainers")  # Skip when testcontainers is not installed.

if sys.platform == "win32":
    pytest.skip(
        "Valkey Docker integration tests are not supported on Windows",
        allow_module_level=True,
    )
if shutil.which("docker") is None:
    pytest.skip(
        "Docker executable is not available; skipping Valkey integration tests",
        allow_module_level=True,
    )

import docker as docker_lib  # type: ignore[import-untyped]
from docker.errors import DockerException  # type: ignore[import-untyped]

try:
    _client = docker_lib.from_env()
    _client.ping()
except DockerException:
    pytest.skip(
        "Docker daemon is not available; skipping Valkey integration tests",
        allow_module_level=True,
    )
else:
    _client.close()

from glide import GlideClient, GlideClientConfiguration, NodeAddress
from testcontainers.core.container import DockerContainer  # type: ignore[import-untyped]
from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-untyped]

from agents import Agent, RunConfig, Runner, TResponseInputItem
from agents.extensions.memory.valkey_session import ValkeySession
from agents.memory import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Docker-backed integration tests should stay on the serial test path.
pytestmark = [pytest.mark.asyncio, pytest.mark.serial]

VALKEY_IMAGE = "docker.io/valkey/valkey-bundle:9.1.0-rc1"
VALKEY_PORT = 6379


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def valkey_container():
    """Start a Valkey container for the whole module and tear it down after."""
    container = DockerContainer(VALKEY_IMAGE).with_exposed_ports(VALKEY_PORT)
    container.start()
    wait_for_logs(container, "Ready to accept connections", timeout=30)
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def valkey_server(valkey_container) -> tuple[str, int]:
    """Return (host, port) reachable from the test process."""
    host = valkey_container.get_container_host_ip()
    port = int(valkey_container.get_exposed_port(VALKEY_PORT))
    return host, port


@pytest_asyncio.fixture(loop_scope="function")
async def glide_client(valkey_server: tuple[str, int]) -> GlideClient:
    """Create a GlideClient connected to the Valkey container.

    Uses ``loop_scope="function"`` because GlideClient's internal Rust event
    loop does not work correctly on the shared session-scoped loop that
    pyproject.toml configures as the default.
    """
    host, port = valkey_server
    config = GlideClientConfiguration(addresses=[NodeAddress(host, port)])
    client = await GlideClient.create(config)
    try:
        yield client  # type: ignore[misc]
    finally:
        try:
            await client.close()
        except Exception:
            pass


@pytest_asyncio.fixture(loop_scope="function")
async def session(glide_client: GlideClient) -> ValkeySession:
    """Provide a clean ValkeySession. Clears data before and after each test."""
    s = ValkeySession(
        session_id="integration_test",
        valkey_client=glide_client,
        key_prefix="test:",
    )
    await s.clear_session()
    try:
        yield s  # type: ignore[misc]
    finally:
        await s.clear_session()


@pytest.fixture
def agent() -> Agent:
    return Agent(name="test", model=FakeModel())


# ===================================================================
# ping / connectivity
# ===================================================================


async def test_ping(session: ValkeySession):
    """Verify that ping() returns True against a live server."""
    assert await session.ping() is True


# ===================================================================
# add_items / get_items
# ===================================================================


async def test_add_and_get_items(session: ValkeySession):
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    got = await session.get_items()
    assert len(got) == 2
    assert got[0].get("content") == "Hello"
    assert got[1].get("content") == "Hi there!"


async def test_add_empty_list_is_noop(session: ValkeySession):
    await session.add_items([])
    assert await session.get_items() == []


async def test_add_items_multiple_batches(session: ValkeySession):
    """Items added in separate calls appear in chronological order."""
    await session.add_items([{"role": "user", "content": "first"}])
    await session.add_items([{"role": "assistant", "content": "second"}])
    await session.add_items([{"role": "user", "content": "third"}])

    got = await session.get_items()
    assert [it.get("content") for it in got] == ["first", "second", "third"]


# ===================================================================
# get_items with limit
# ===================================================================


async def test_get_items_with_limit(session: ValkeySession):
    await session.add_items([{"role": "user", "content": str(i)} for i in range(6)])

    last2 = await session.get_items(limit=2)
    assert len(last2) == 2
    assert last2[0].get("content") == "4"
    assert last2[1].get("content") == "5"


async def test_get_items_limit_zero(session: ValkeySession):
    await session.add_items([{"role": "user", "content": "x"}])
    assert await session.get_items(limit=0) == []


async def test_get_items_limit_exceeds_count(session: ValkeySession):
    await session.add_items([{"role": "user", "content": str(i)} for i in range(3)])
    got = await session.get_items(limit=100)
    assert len(got) == 3


# ===================================================================
# pop_item
# ===================================================================


async def test_pop_item(session: ValkeySession):
    await session.add_items(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
    )

    popped = await session.pop_item()
    assert popped is not None and popped.get("content") == "b"

    remaining = await session.get_items()
    assert len(remaining) == 1
    assert remaining[0].get("content") == "a"


async def test_pop_from_empty_session(session: ValkeySession):
    assert await session.pop_item() is None


async def test_pop_all_items(session: ValkeySession):
    """Popping every item leaves the session empty."""
    await session.add_items(
        [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ]
    )
    assert await session.pop_item() is not None
    assert await session.pop_item() is not None
    assert await session.pop_item() is None
    assert await session.get_items() == []


# ===================================================================
# clear_session
# ===================================================================


async def test_clear_session(session: ValkeySession):
    await session.add_items([{"role": "user", "content": "gone"}])
    await session.clear_session()
    assert await session.get_items() == []


async def test_clear_empty_session(session: ValkeySession):
    """Clearing an already-empty session must not raise."""
    await session.clear_session()
    assert await session.get_items() == []


# ===================================================================
# TTL
# ===================================================================


async def test_ttl_sets_expiry(glide_client: GlideClient):
    """Keys should have a TTL after add_items when ttl is configured."""
    s = ValkeySession(
        session_id="ttl_test",
        valkey_client=glide_client,
        key_prefix="test:",
        ttl=60,
    )
    try:
        await s.clear_session()
        await s.add_items([{"role": "user", "content": "expires"}])

        ttl_val = await glide_client.ttl(s._messages_key)
        assert ttl_val is not None and ttl_val > 0

        session_ttl = await glide_client.ttl(s._session_key)
        assert session_ttl is not None and session_ttl > 0
    finally:
        await s.clear_session()


async def test_no_ttl_means_persistent(glide_client: GlideClient):
    """Without ttl, keys should have no expiry (TTL == -1)."""
    s = ValkeySession(
        session_id="no_ttl_test",
        valkey_client=glide_client,
        key_prefix="test:",
        ttl=None,
    )
    try:
        await s.clear_session()
        await s.add_items([{"role": "user", "content": "forever"}])

        ttl_val = await glide_client.ttl(s._messages_key)
        assert ttl_val == -1  # -1 means no expiry.
    finally:
        await s.clear_session()


# ===================================================================
# Session isolation
# ===================================================================


async def test_session_id_isolation(glide_client: GlideClient):
    s1 = ValkeySession(session_id="iso_a", valkey_client=glide_client, key_prefix="test:")
    s2 = ValkeySession(session_id="iso_b", valkey_client=glide_client, key_prefix="test:")
    try:
        await s1.clear_session()
        await s2.clear_session()

        await s1.add_items([{"role": "user", "content": "from a"}])
        await s2.add_items([{"role": "user", "content": "from b"}])

        assert (await s1.get_items())[0].get("content") == "from a"
        assert (await s2.get_items())[0].get("content") == "from b"
    finally:
        await s1.clear_session()
        await s2.clear_session()


async def test_key_prefix_isolation(glide_client: GlideClient):
    s1 = ValkeySession(session_id="same", valkey_client=glide_client, key_prefix="app1:")
    s2 = ValkeySession(session_id="same", valkey_client=glide_client, key_prefix="app2:")
    try:
        await s1.clear_session()
        await s2.clear_session()

        await s1.add_items([{"role": "user", "content": "app1"}])
        await s2.add_items([{"role": "user", "content": "app2"}])

        assert (await s1.get_items())[0].get("content") == "app1"
        assert (await s2.get_items())[0].get("content") == "app2"
    finally:
        await s1.clear_session()
        await s2.clear_session()


# ===================================================================
# Data integrity — unicode, special characters
# ===================================================================


async def test_unicode_roundtrip(session: ValkeySession):
    await session.add_items(
        [
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "😊👍"},
            {"role": "user", "content": "Привет"},
        ]
    )
    got = await session.get_items()
    assert got[0].get("content") == "こんにちは"
    assert got[1].get("content") == "😊👍"
    assert got[2].get("content") == "Привет"


async def test_special_characters(session: ValkeySession):
    payloads = [
        "O'Reilly",
        '{"nested": "json"}',
        'Quote: "Hello world"',
        "Line1\nLine2\tTabbed",
        "Robert'); DROP TABLE students;--",
        "\\n\\t\\r literal backslashes",
    ]
    items: list[TResponseInputItem] = [{"role": "user", "content": p} for p in payloads]
    await session.add_items(items)

    got = await session.get_items()
    for expected, actual in zip(payloads, got, strict=False):
        assert actual.get("content") == expected


# ===================================================================
# Corrupted data (injected directly into Valkey)
# ===================================================================


async def test_get_items_skips_corrupted_data(glide_client: GlideClient):
    """Corrupted entries in the list are silently skipped by get_items."""
    s = ValkeySession(
        session_id="corrupt_get",
        valkey_client=glide_client,
        key_prefix="test:",
    )
    try:
        await s.clear_session()
        await s.add_items([{"role": "user", "content": "valid"}])

        # Inject garbage directly into the Valkey list.
        await glide_client.rpush(s._messages_key, ["not-json", "{broken"])

        got = await s.get_items()
        assert len(got) == 1
        assert got[0].get("content") == "valid"
    finally:
        await s.clear_session()


async def test_pop_returns_none_for_corrupted_data(glide_client: GlideClient):
    """pop_item returns None (not an exception) when the popped entry is garbage."""
    s = ValkeySession(
        session_id="corrupt_pop",
        valkey_client=glide_client,
        key_prefix="test:",
    )
    try:
        await s.clear_session()
        await glide_client.rpush(s._messages_key, ["%%%invalid%%%"])

        assert await s.pop_item() is None
        # The garbage was consumed; list should be empty now.
        assert await s.get_items() == []
    finally:
        await s.clear_session()


# ===================================================================
# Concurrent access
# ===================================================================


async def test_concurrent_add_items(glide_client: GlideClient):
    s = ValkeySession(
        session_id="concurrent",
        valkey_client=glide_client,
        key_prefix="test:",
    )
    try:
        await s.clear_session()

        async def _add(start: int, n: int) -> None:
            await s.add_items([{"role": "user", "content": f"m{start + i}"} for i in range(n)])

        await asyncio.gather(_add(0, 5), _add(5, 5), _add(10, 5))

        got = await s.get_items()
        assert len(got) == 15
        contents = {it.get("content") for it in got}
        assert contents == {f"m{i}" for i in range(15)}
    finally:
        await s.clear_session()


# ===================================================================
# from_url
# ===================================================================


async def test_from_url(valkey_server: tuple[str, int]):
    """from_url should create a working session against the real server."""
    host, port = valkey_server
    url = f"valkey://{host}:{port}/0"

    s = await ValkeySession.from_url("from_url_test", url=url, key_prefix="test:")
    try:
        assert s._owns_client is True
        assert await s.ping() is True

        await s.clear_session()
        await s.add_items([{"role": "user", "content": "via url"}])
        got = await s.get_items()
        assert len(got) == 1
        assert got[0].get("content") == "via url"
    finally:
        await s.clear_session()
        await s.close()


async def test_from_url_with_session_settings(valkey_server: tuple[str, int]):
    host, port = valkey_server
    url = f"valkey://{host}:{port}/0"

    s = await ValkeySession.from_url(
        "from_url_ss",
        url=url,
        key_prefix="test:",
        session_settings=SessionSettings(limit=2),
    )
    try:
        await s.clear_session()
        await s.add_items([{"role": "user", "content": str(i)} for i in range(5)])

        got = await s.get_items()  # Should use limit=2 from settings.
        assert len(got) == 2
        assert got[0].get("content") == "3"
        assert got[1].get("content") == "4"
    finally:
        await s.clear_session()
        await s.close()


async def test_from_url_with_ttl(valkey_server: tuple[str, int]):
    host, port = valkey_server
    url = f"valkey://{host}:{port}/0"

    s = await ValkeySession.from_url("from_url_ttl", url=url, key_prefix="test:", ttl=120)
    try:
        await s.clear_session()
        await s.add_items([{"role": "user", "content": "ttl via url"}])

        got = await s.get_items()
        assert len(got) == 1
    finally:
        await s.clear_session()
        await s.close()


# ===================================================================
# Client ownership
# ===================================================================


async def test_close_does_not_close_external_client(glide_client: GlideClient):
    """Closing a session with an external client must leave the client usable."""
    s = ValkeySession(
        session_id="ext_close",
        valkey_client=glide_client,
        key_prefix="test:",
    )
    await s.close()

    # The shared client should still work.
    pong = await glide_client.custom_command(["PING"])
    assert pong == b"PONG"


# ===================================================================
# Runner integration (FakeModel, real Valkey)
# ===================================================================


async def test_runner_integration(session: ValkeySession, agent: Agent):
    model = agent.model
    assert isinstance(model, FakeModel)

    model.set_next_output([get_text_message("San Francisco")])
    r1 = await Runner.run(agent, "Where is the Golden Gate Bridge?", session=session)
    assert r1.final_output == "San Francisco"

    model.set_next_output([get_text_message("California")])
    r2 = await Runner.run(agent, "What state?", session=session)
    assert r2.final_output == "California"

    last_input = model.last_turn_args["input"]
    assert any("Golden Gate Bridge" in str(it.get("content", "")) for it in last_input)


async def test_runner_session_settings_override(glide_client: GlideClient):
    s = ValkeySession(
        session_id="runner_ss",
        valkey_client=glide_client,
        key_prefix="test:",
        session_settings=SessionSettings(limit=100),
    )
    try:
        await s.clear_session()
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

        history = [
            it for it in model.last_turn_args["input"] if it.get("content") != "New question"
        ]
        assert len(history) == 2
    finally:
        await s.clear_session()


# ===================================================================
# SessionSettings with real Valkey
# ===================================================================


async def test_session_settings_limit(glide_client: GlideClient):
    s = ValkeySession(
        session_id="ss_limit",
        valkey_client=glide_client,
        key_prefix="test:",
        session_settings=SessionSettings(limit=3),
    )
    try:
        await s.clear_session()
        await s.add_items([{"role": "user", "content": str(i)} for i in range(5)])

        got = await s.get_items()
        assert len(got) == 3
        assert got[0].get("content") == "2"
    finally:
        await s.clear_session()


async def test_explicit_limit_overrides_settings(glide_client: GlideClient):
    s = ValkeySession(
        session_id="ss_override",
        valkey_client=glide_client,
        key_prefix="test:",
        session_settings=SessionSettings(limit=10),
    )
    try:
        await s.clear_session()
        await s.add_items([{"role": "user", "content": str(i)} for i in range(10)])

        got = await s.get_items(limit=2)
        assert len(got) == 2
        assert got[0].get("content") == "8"
    finally:
        await s.clear_session()

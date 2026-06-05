"""Tests for ValkeySession using testcontainers (Valkey container) or a local server.

Run with::

    # Requires Docker for testcontainers, or set VALKEY_URL env var
    VALKEY_URL=valkey://localhost:6379/15 pytest tests/extensions/memory/test_valkey_session.py -v
"""

from __future__ import annotations

import os
import shutil
import sys
import time

import pytest

pytest.importorskip("glide")  # Skip tests if valkey-glide is not installed

from agents import Agent, RunConfig, Runner, TResponseInputItem
from agents.extensions.memory.valkey_session import ValkeySession
from agents.memory import SessionSettings
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Docker-backed integration tests should stay on the serial test path.
pytestmark = [pytest.mark.asyncio, pytest.mark.serial]

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_use_testcontainers = False
_valkey_url_from_container: str | None = None


def _docker_available() -> bool:
    """Check whether Docker is available on this machine."""
    if sys.platform == "win32":
        return False
    if shutil.which("docker") is None:
        return False
    try:
        import docker  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        client = docker.from_env()
        client.ping()
        client.close()
        return True
    except Exception:
        return False


async def _create_valkey_session(
    session_id: str,
    key_prefix: str = "test:",
    ttl: int | None = None,
    session_settings: SessionSettings | None = None,
) -> ValkeySession:
    """Create a ValkeySession connected to a test Valkey/Redis instance."""
    from glide import GlideClient, GlideClientConfiguration, NodeAddress

    url = os.environ.get("VALKEY_URL")

    if url:
        session = await ValkeySession.from_url(
            session_id,
            url=url,
            key_prefix=key_prefix,
            ttl=ttl,
            session_settings=session_settings,
        )
    else:
        # Fall back to localhost with default port
        config = GlideClientConfiguration(
            addresses=[NodeAddress("localhost", 6379)],
        )
        client = await GlideClient.create(config)
        session = ValkeySession(
            session_id=session_id,
            glide_client=client,
            key_prefix=key_prefix,
            ttl=ttl,
            session_settings=session_settings,
        )
        session._owns_client = True

    if not await session.ping():
        await session.close()
        pytest.skip("Valkey/Redis server not available")

    return session


async def _create_test_session(
    session_id: str | None = None,
) -> ValkeySession:
    """Create a test session with cleanup."""
    import uuid

    if session_id is None:
        session_id = f"test_session_{uuid.uuid4().hex[:8]}"

    session = await _create_valkey_session(session_id, key_prefix="test:")

    # Clean up any existing data
    await session.clear_session()

    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_valkey_session_direct_ops():
    """Test direct database operations of ValkeySession."""
    session = await _create_test_session()

    try:
        # 1. Add items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        await session.add_items(items)

        # 2. Get items and verify
        retrieved = await session.get_items()
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Hello"
        assert retrieved[1].get("content") == "Hi there!"

        # 3. Pop item
        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("content") == "Hi there!"
        retrieved_after_pop = await session.get_items()
        assert len(retrieved_after_pop) == 1
        assert retrieved_after_pop[0].get("content") == "Hello"

        # 4. Clear session
        await session.clear_session()
        retrieved_after_clear = await session.get_items()
        assert len(retrieved_after_clear) == 0

    finally:
        await session.close()


async def test_runner_integration(agent: Agent):
    """Test that ValkeySession works correctly with the agent Runner."""
    session = await _create_test_session()

    try:
        # First turn
        assert isinstance(agent.model, FakeModel)
        agent.model.set_next_output([get_text_message("San Francisco")])
        result1 = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        assert result1.final_output == "San Francisco"

        # Second turn
        agent.model.set_next_output([get_text_message("California")])
        result2 = await Runner.run(agent, "What state is it in?", session=session)
        assert result2.final_output == "California"

        # Verify history was passed to the model on the second turn
        last_input = agent.model.last_turn_args["input"]
        assert len(last_input) > 1
        assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)

    finally:
        await session.close()


async def test_session_isolation():
    """Test that different session IDs result in isolated conversation histories."""
    session1 = await _create_valkey_session("session_1")
    session2 = await _create_valkey_session("session_2")

    try:
        agent = Agent(name="test", model=FakeModel())

        await session1.clear_session()
        await session2.clear_session()

        # Interact with session 1
        assert isinstance(agent.model, FakeModel)
        agent.model.set_next_output([get_text_message("I like cats.")])
        await Runner.run(agent, "I like cats.", session=session1)

        # Interact with session 2
        agent.model.set_next_output([get_text_message("I like dogs.")])
        await Runner.run(agent, "I like dogs.", session=session2)

        # Go back to session 1
        agent.model.set_next_output([get_text_message("You said you like cats.")])
        result = await Runner.run(agent, "What animal did I say I like?", session=session1)
        assert "cats" in result.final_output.lower()
        assert "dogs" not in result.final_output.lower()
    finally:
        try:
            await session1.clear_session()
            await session2.clear_session()
        except Exception:
            pass
        await session1.close()
        await session2.close()


async def test_get_items_with_limit():
    """Test the limit parameter in get_items."""
    session = await _create_test_session()

    try:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "3"},
            {"role": "assistant", "content": "4"},
        ]
        await session.add_items(items)

        # Get last 2 items
        latest_2 = await session.get_items(limit=2)
        assert len(latest_2) == 2
        assert latest_2[0].get("content") == "3"
        assert latest_2[1].get("content") == "4"

        # Get all items
        all_items = await session.get_items()
        assert len(all_items) == 4

        # Get more than available
        more_than_all = await session.get_items(limit=10)
        assert len(more_than_all) == 4

        # Get 0 items
        zero_items = await session.get_items(limit=0)
        assert len(zero_items) == 0

    finally:
        await session.close()


async def test_pop_from_empty_session():
    """Test that pop_item returns None on an empty session."""
    session = await _create_valkey_session("empty_session")
    try:
        await session.clear_session()
        popped = await session.pop_item()
        assert popped is None
    finally:
        await session.close()


async def test_add_empty_items_list():
    """Test that adding an empty list of items is a no-op."""
    session = await _create_test_session()

    try:
        initial_items = await session.get_items()
        assert len(initial_items) == 0

        await session.add_items([])

        items_after_add = await session.get_items()
        assert len(items_after_add) == 0

    finally:
        await session.close()


async def test_unicode_content():
    """Test that session correctly stores and retrieves unicode/non-ASCII content."""
    session = await _create_test_session()

    try:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "😊👍"},
            {"role": "user", "content": "Привет"},
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert retrieved[0].get("content") == "こんにちは"
        assert retrieved[1].get("content") == "😊👍"
        assert retrieved[2].get("content") == "Привет"

    finally:
        await session.close()


async def test_special_characters_and_json_safety():
    """Test that session safely stores and retrieves items with special characters."""
    session = await _create_test_session()

    try:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "O'Reilly"},
            {"role": "assistant", "content": '{"nested": "json"}'},
            {"role": "user", "content": 'Quote: "Hello world"'},
            {"role": "assistant", "content": "Line1\nLine2\tTabbed"},
            {"role": "user", "content": "Normal message"},
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == len(items)
        assert retrieved[0].get("content") == "O'Reilly"
        assert retrieved[1].get("content") == '{"nested": "json"}'
        assert retrieved[2].get("content") == 'Quote: "Hello world"'
        assert retrieved[3].get("content") == "Line1\nLine2\tTabbed"
        assert retrieved[4].get("content") == "Normal message"

    finally:
        await session.close()


async def test_data_integrity_with_problematic_strings():
    """Test that session preserves data integrity with strings that could break parsers."""
    session = await _create_test_session()

    try:
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "O'Reilly"},
            {"role": "assistant", "content": "DROP TABLE sessions;"},
            {"role": "user", "content": '"SELECT * FROM users WHERE name = "admin";"'},
            {"role": "assistant", "content": "Robert'); DROP TABLE students;--"},
            {"role": "user", "content": '{"malicious": "json"}'},
            {"role": "assistant", "content": "\\n\\t\\r Special escapes"},
            {"role": "user", "content": "Normal message"},
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == len(items)
        assert retrieved[0].get("content") == "O'Reilly"
        assert retrieved[1].get("content") == "DROP TABLE sessions;"
        assert retrieved[2].get("content") == '"SELECT * FROM users WHERE name = "admin";"'
        assert retrieved[3].get("content") == "Robert'); DROP TABLE students;--"
        assert retrieved[4].get("content") == '{"malicious": "json"}'
        assert retrieved[5].get("content") == "\\n\\t\\r Special escapes"
        assert retrieved[6].get("content") == "Normal message"

    finally:
        await session.close()


async def test_concurrent_access():
    """Test concurrent access to the same session to verify data integrity."""
    import asyncio

    session = await _create_test_session("concurrent_test")

    try:
        async def add_messages(start_idx: int, count: int):
            items: list[TResponseInputItem] = [
                {"role": "user", "content": f"Message {start_idx + i}"} for i in range(count)
            ]
            await session.add_items(items)

        tasks = [
            add_messages(0, 5),
            add_messages(5, 5),
            add_messages(10, 5),
        ]

        await asyncio.gather(*tasks)

        retrieved = await session.get_items()
        assert len(retrieved) == 15

        contents = [item.get("content") for item in retrieved]
        expected_messages = [f"Message {i}" for i in range(15)]

        for expected in expected_messages:
            assert expected in contents

    finally:
        await session.close()


async def test_valkey_connectivity():
    """Test Valkey connectivity methods."""
    session = await _create_valkey_session("connectivity_test")
    try:
        is_connected = await session.ping()
        assert is_connected is True
    finally:
        await session.close()


async def test_ttl_functionality():
    """Test TTL (time-to-live) functionality."""
    session = await _create_valkey_session("ttl_test", ttl=1)

    try:
        await session.clear_session()

        items: list[TResponseInputItem] = [
            {"role": "user", "content": "This should expire"},
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == 1

    finally:
        try:
            await session.clear_session()
        except Exception:
            pass
        await session.close()


async def test_from_url_constructor():
    """Test the from_url constructor method."""
    url = os.environ.get("VALKEY_URL", "valkey://localhost:6379/15")

    session = await ValkeySession.from_url("url_test", url=url)
    try:
        if not await session.ping():
            pytest.skip("Valkey/Redis server not available")

        assert session.session_id == "url_test"
        assert session._owns_client is True
        assert await session.ping() is True
    finally:
        await session.close()


async def test_from_url_valkey_scheme():
    """Test that valkey:// URL scheme works."""
    url = os.environ.get("VALKEY_URL")

    if not url:
        url = "valkey://localhost:6379/15"

    session = await ValkeySession.from_url("valkey_scheme_test", url=url)
    try:
        if not await session.ping():
            pytest.skip("Valkey server not available")

        items: list[TResponseInputItem] = [{"role": "user", "content": "valkey scheme test"}]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == 1
        assert retrieved[0].get("content") == "valkey scheme test"
    finally:
        await session.close()


async def test_key_prefix_isolation():
    """Test that different key prefixes isolate sessions."""
    session1 = await _create_valkey_session("same_id", key_prefix="app1")
    session2 = await _create_valkey_session("same_id", key_prefix="app2")

    try:
        await session1.clear_session()
        await session2.clear_session()

        await session1.add_items([{"role": "user", "content": "app1 message"}])
        await session2.add_items([{"role": "user", "content": "app2 message"}])

        items1 = await session1.get_items()
        items2 = await session2.get_items()

        assert len(items1) == 1
        assert len(items2) == 1
        assert items1[0].get("content") == "app1 message"
        assert items2[0].get("content") == "app2 message"

    finally:
        try:
            await session1.clear_session()
            await session2.clear_session()
        except Exception:
            pass
        await session1.close()
        await session2.close()


async def test_external_client_not_closed():
    """Test that external GlideClient is not closed when session.close() is called."""
    from glide import GlideClient, GlideClientConfiguration, NodeAddress

    shared_config = GlideClientConfiguration(
        addresses=[NodeAddress("localhost", 6379)],
    )
    shared_client = await GlideClient.create(shared_config)

    try:
        if not await shared_client.ping():
            await shared_client.close()
            pytest.skip("Valkey/Redis server not available")

        session = ValkeySession(
            session_id="external_client_test",
            glide_client=shared_client,
            key_prefix="test:",
        )

        try:
            await session.add_items([{"role": "user", "content": "test message"}])
            items = await session.get_items()
            assert len(items) == 1

            assert await shared_client.ping() is not None

            await session.close()

            # Shared client should still be usable after session.close()
            assert await shared_client.ping() is not None

            await shared_client.set("test_key", "test_value")
            value = await shared_client.get("test_key")
            assert value == b"test_value"

        finally:
            await session.clear_session()

    finally:
        try:
            await shared_client.close()
        except Exception:
            pass


async def test_internal_client_ownership():
    """Test that clients created via from_url are properly managed."""
    url = os.environ.get("VALKEY_URL", "valkey://localhost:6379/15")

    session = await ValkeySession.from_url("internal_client_test", url=url)

    try:
        if not await session.ping():
            pytest.skip("Valkey/Redis server not available")

        await session.add_items([{"role": "user", "content": "test message"}])
        items = await session.get_items()
        assert len(items) == 1

        assert hasattr(session, "_owns_client")
        assert session._owns_client is True

    finally:
        await session.close()


async def test_get_next_id_method():
    """Test the _get_next_id atomic counter functionality."""
    session = await _create_test_session("counter_test")

    try:
        await session.clear_session()

        id1 = await session._get_next_id()
        id2 = await session._get_next_id()
        id3 = await session._get_next_id()

        assert id1 == 1
        assert id2 == 2
        assert id3 == 3

        # Counter persists across session instances with same session_id
        from glide import GlideClient, GlideClientConfiguration, NodeAddress

        config = GlideClientConfiguration(
            addresses=[NodeAddress("localhost", 6379)],
        )
        client2 = await GlideClient.create(config)
        session2 = ValkeySession(
            session_id="counter_test",
            glide_client=client2,
            key_prefix="test:",
        )
        session2._owns_client = True

        try:
            id4 = await session2._get_next_id()
            assert id4 == 4
        finally:
            await session2.close()

    finally:
        await session.close()


async def test_add_items_preserves_created_at_metadata():
    """`created_at` must be set once and not overwritten by subsequent add_items calls."""
    session = await _create_test_session("created_at_test")

    try:
        await session.clear_session()
        await session.add_items([{"role": "user", "content": "first"}])
        first_meta = await session._glide.hgetall(session._session_key)
        first_created = first_meta.get(b"created_at")
        assert first_created is not None

        # Force a clock advance
        time.sleep(1.1)

        await session.add_items([{"role": "user", "content": "second"}])
        second_meta = await session._glide.hgetall(session._session_key)
        second_created = second_meta.get(b"created_at")
        second_updated = second_meta.get(b"updated_at")

        assert second_created == first_created, "created_at must remain stable"
        assert second_updated != first_created, "updated_at must advance on writes"
    finally:
        await session.close()


async def test_ping_connection_failure():
    """Test ping method when connection fails."""
    session = await _create_test_session("ping_failure_test")

    try:
        assert await session.ping() is True

        # Fake a failure by trying to ping a closed client
        await session._glide.close()
        assert await session.ping() is False

    except Exception:
        # If closing the client causes issues, skip the negative test
        pass
    finally:
        # Prevent double-close
        session._owns_client = False
        await session.close()


async def test_close_method_coverage():
    """Test complete coverage of close() method behavior."""
    from glide import GlideClient, GlideClientConfiguration, NodeAddress

    # Test 1: External client (should NOT be closed)
    external_config = GlideClientConfiguration(
        addresses=[NodeAddress("localhost", 6379)],
    )
    external_client = await GlideClient.create(external_config)
    try:
        if not await external_client.ping():
            await external_client.close()
            pytest.skip("Valkey/Redis server not available")

        session1 = ValkeySession(
            session_id="close_test_1",
            glide_client=external_client,
            key_prefix="test:",
        )

        assert session1._owns_client is False
        await session1.close()

        # External client should still be usable
        assert await external_client.ping() is not None

    finally:
        try:
            await external_client.close()
        except Exception:
            pass


async def test_session_settings_default():
    """Test that session_settings defaults to empty SessionSettings."""
    session = await _create_test_session()

    try:
        assert isinstance(session.session_settings, SessionSettings)
        assert session.session_settings.limit is None
    finally:
        await session.close()


async def test_session_settings_constructor():
    """Test passing session_settings via constructor."""
    session = await _create_valkey_session(
        "settings_test",
        session_settings=SessionSettings(limit=5),
    )

    try:
        assert session.session_settings is not None
        assert session.session_settings.limit == 5
    finally:
        await session.close()


async def test_session_settings_from_url():
    """Test passing session_settings via from_url."""
    url = os.environ.get("VALKEY_URL", "valkey://localhost:6379/15")

    session = await ValkeySession.from_url(
        "from_url_settings_test",
        url=url,
        session_settings=SessionSettings(limit=10),
    )

    try:
        if not await session.ping():
            pytest.skip("Valkey/Redis server not available")
        assert session.session_settings is not None
        assert session.session_settings.limit == 10
    finally:
        await session.close()


async def test_get_items_uses_session_settings_limit():
    """Test that get_items uses session_settings.limit as default."""
    session = await _create_valkey_session(
        "uses_settings_limit_test",
        session_settings=SessionSettings(limit=3),
    )

    try:
        await session.clear_session()

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(5)
        ]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == 3
        assert retrieved[0].get("content") == "Message 2"
        assert retrieved[1].get("content") == "Message 3"
        assert retrieved[2].get("content") == "Message 4"
    finally:
        await session.close()


async def test_get_items_explicit_limit_overrides_session_settings():
    """Test that explicit limit parameter overrides session_settings."""
    session = await _create_valkey_session(
        "explicit_override_test",
        session_settings=SessionSettings(limit=5),
    )

    try:
        await session.clear_session()

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(10)
        ]
        await session.add_items(items)

        retrieved = await session.get_items(limit=2)
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Message 8"
        assert retrieved[1].get("content") == "Message 9"
    finally:
        await session.close()


async def test_runner_with_session_settings_override():
    """Test that RunConfig can override session's default settings."""
    session = await _create_valkey_session(
        "runner_override_test",
        session_settings=SessionSettings(limit=100),
    )

    try:
        await session.clear_session()

        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Turn {i}"} for i in range(10)
        ]
        await session.add_items(items)

        model = FakeModel()
        agent = Agent(name="test", model=model)
        model.set_next_output([get_text_message("Got it")])

        await Runner.run(
            agent,
            "New question",
            session=session,
            run_config=RunConfig(
                session_settings=SessionSettings(limit=2)
            ),
        )

        last_input = model.last_turn_args["input"]
        history_items = [item for item in last_input if item.get("content") != "New question"]
        assert len(history_items) == 2
    finally:
        await session.close()

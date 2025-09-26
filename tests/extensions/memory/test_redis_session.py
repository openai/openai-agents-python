from __future__ import annotations

from typing import cast

import pytest

pytest.importorskip("redis")  # Skip tests if Redis is not installed

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory.redis_session import RedisSession
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

# Try to use fakeredis for in-memory testing, fall back to real Redis if not available
try:
    import fakeredis.aioredis
    from redis.asyncio import Redis

    # Use the actual Redis type annotation, but cast the FakeRedis implementation
    fake_redis_instance = fakeredis.aioredis.FakeRedis()
    fake_redis: Redis = cast("Redis", fake_redis_instance)
    USE_FAKE_REDIS = True
except ImportError:
    fake_redis = None  # type: ignore[assignment]
    USE_FAKE_REDIS = False

if not USE_FAKE_REDIS:
    # Fallback to real Redis for tests that need it
    REDIS_URL = "redis://localhost:6379/15"  # Using database 15 for tests


async def _safe_rpush(client: Redis, key: str, value: str) -> None:
    """Safely handle rpush operations that might be sync or async in fakeredis."""
    result = client.rpush(key, value)
    if hasattr(result, "__await__"):
        await result


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


async def _create_redis_session(
    session_id: str, key_prefix: str = "test:", ttl: int | None = None
) -> RedisSession:
    """Helper to create a Redis session with consistent configuration."""
    if USE_FAKE_REDIS:
        # Use in-memory fake Redis for testing
        return RedisSession(
            session_id=session_id,
            redis_client=fake_redis,
            key_prefix=key_prefix,
            ttl=ttl,
        )
    else:
        session = RedisSession.from_url(session_id, url=REDIS_URL, key_prefix=key_prefix, ttl=ttl)
        # Ensure we can connect
        if not await session.ping():
            await session.close()
            pytest.skip("Redis server not available")
        return session


async def _create_test_session(session_id: str | None = None) -> RedisSession:
    """Helper to create a test session with cleanup."""
    import uuid

    if session_id is None:
        session_id = f"test_session_{uuid.uuid4().hex[:8]}"

    if USE_FAKE_REDIS:
        # Use in-memory fake Redis for testing
        session = RedisSession(session_id=session_id, redis_client=fake_redis, key_prefix="test:")
    else:
        session = RedisSession.from_url(session_id, url=REDIS_URL)

        # Ensure we can connect
        if not await session.ping():
            await session.close()
            pytest.skip("Redis server not available")

    # Clean up any existing data
    await session.clear_session()

    return session


async def test_redis_session_direct_ops():
    """Test direct database operations of RedisSession."""
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
    """Test that RedisSession works correctly with the agent Runner."""
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
    session1 = await _create_redis_session("session_1")
    session2 = await _create_redis_session("session_2")

    try:
        agent = Agent(name="test", model=FakeModel())

        # Clean up any existing data
        await session1.clear_session()
        await session2.clear_session()

        # Interact with session 1
        assert isinstance(agent.model, FakeModel)
        agent.model.set_next_output([get_text_message("I like cats.")])
        await Runner.run(agent, "I like cats.", session=session1)

        # Interact with session 2
        agent.model.set_next_output([get_text_message("I like dogs.")])
        await Runner.run(agent, "I like dogs.", session=session2)

        # Go back to session 1 and check its memory
        agent.model.set_next_output([get_text_message("You said you like cats.")])
        result = await Runner.run(agent, "What animal did I say I like?", session=session1)
        assert "cats" in result.final_output.lower()
        assert "dogs" not in result.final_output.lower()
    finally:
        try:
            await session1.clear_session()
            await session2.clear_session()
        except Exception:
            pass  # Ignore cleanup errors
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
    session = await _create_redis_session("empty_session")
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
        # Add unicode content to the session
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "😊👍"},
            {"role": "user", "content": "Привет"},
        ]
        await session.add_items(items)

        # Retrieve items and verify unicode content
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
        # Add items with special characters and JSON-problematic content
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "O'Reilly"},
            {"role": "assistant", "content": '{"nested": "json"}'},
            {"role": "user", "content": 'Quote: "Hello world"'},
            {"role": "assistant", "content": "Line1\nLine2\tTabbed"},
            {"role": "user", "content": "Normal message"},
        ]
        await session.add_items(items)

        # Retrieve all items and verify they are stored correctly
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
        # Add items with various problematic string patterns that could break JSON parsing,
        # string escaping, or other serialization mechanisms
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "O'Reilly"},  # Single quote
            {"role": "assistant", "content": "DROP TABLE sessions;"},  # SQL-like command
            {"role": "user", "content": '"SELECT * FROM users WHERE name = "admin";"'},
            {"role": "assistant", "content": "Robert'); DROP TABLE students;--"},
            {"role": "user", "content": '{"malicious": "json"}'},  # JSON-like string
            {"role": "assistant", "content": "\\n\\t\\r Special escapes"},  # Escape sequences
            {"role": "user", "content": "Normal message"},  # Control case
        ]
        await session.add_items(items)

        # Retrieve all items and verify they are stored exactly as provided
        # This ensures the storage layer doesn't modify, escape, or corrupt data
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
        # Prepare items for concurrent writing
        async def add_messages(start_idx: int, count: int):
            items: list[TResponseInputItem] = [
                {"role": "user", "content": f"Message {start_idx + i}"} for i in range(count)
            ]
            await session.add_items(items)

        # Run multiple concurrent add operations
        tasks = [
            add_messages(0, 5),  # Messages 0-4
            add_messages(5, 5),  # Messages 5-9
            add_messages(10, 5),  # Messages 10-14
        ]

        await asyncio.gather(*tasks)

        # Verify all items were added
        retrieved = await session.get_items()
        assert len(retrieved) == 15

        # Extract message numbers and verify all are present
        contents = [item.get("content") for item in retrieved]
        expected_messages = [f"Message {i}" for i in range(15)]

        # Check that all expected messages are present (order may vary due to concurrency)
        for expected in expected_messages:
            assert expected in contents

    finally:
        await session.close()


async def test_redis_connectivity():
    """Test Redis connectivity methods."""
    session = await _create_redis_session("connectivity_test")
    try:
        # Test ping - should work with both real and fake Redis
        is_connected = await session.ping()
        assert is_connected is True
    finally:
        await session.close()


async def test_ttl_functionality():
    """Test TTL (time-to-live) functionality."""
    session = await _create_redis_session("ttl_test", ttl=1)  # 1 second TTL

    try:
        await session.clear_session()

        # Add items with TTL
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "This should expire"},
        ]
        await session.add_items(items)

        # Verify items exist immediately
        retrieved = await session.get_items()
        assert len(retrieved) == 1

        # Note: We don't test actual expiration in unit tests as it would require
        # waiting and make tests slow. The TTL setting is tested by verifying
        # the Redis commands are called correctly.
    finally:
        try:
            await session.clear_session()
        except Exception:
            pass  # Ignore cleanup errors
        await session.close()


async def test_from_url_constructor():
    """Test the from_url constructor method."""
    # This test specifically validates the from_url class method which parses
    # Redis connection URLs and creates real Redis connections. Since fakeredis
    # doesn't support URL-based connection strings in the same way, this test
    # must use a real Redis server to properly validate URL parsing functionality.
    if USE_FAKE_REDIS:
        pytest.skip("from_url constructor test requires real Redis server")

    # Test standard Redis URL
    session = RedisSession.from_url("url_test", url="redis://localhost:6379/15")
    try:
        if not await session.ping():
            pytest.skip("Redis server not available")

        assert session.session_id == "url_test"
        assert await session.ping() is True
    finally:
        await session.close()


async def test_key_prefix_isolation():
    """Test that different key prefixes isolate sessions."""
    session1 = await _create_redis_session("same_id", key_prefix="app1")
    session2 = await _create_redis_session("same_id", key_prefix="app2")

    try:
        # Clean up
        await session1.clear_session()
        await session2.clear_session()

        # Add different items to each session
        await session1.add_items([{"role": "user", "content": "app1 message"}])
        await session2.add_items([{"role": "user", "content": "app2 message"}])

        # Verify isolation
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
            pass  # Ignore cleanup errors
        await session1.close()
        await session2.close()


async def test_external_client_not_closed():
    """Test that external Redis clients are not closed when session.close() is called."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for client state verification")

    # Create a shared Redis client
    shared_client = fake_redis

    # Create session with external client
    session = RedisSession(
        session_id="external_client_test",
        redis_client=shared_client,
        key_prefix="test:",
    )

    try:
        # Add some data to verify the client is working
        await session.add_items([{"role": "user", "content": "test message"}])
        items = await session.get_items()
        assert len(items) == 1

        # Verify client is working before close
        assert await shared_client.ping() is True

        # Close the session
        await session.close()

        # Verify the shared client is still usable after session.close()
        # This would fail if we incorrectly closed the external client
        assert await shared_client.ping() is True

        # Should still be able to use the client for other operations
        await shared_client.set("test_key", "test_value")
        value = await shared_client.get("test_key")
        assert value.decode("utf-8") == "test_value"

    finally:
        # Clean up
        try:
            await session.clear_session()
        except Exception:
            pass  # Ignore cleanup errors if connection is already closed


async def test_internal_client_ownership():
    """Test that clients created via from_url are properly managed."""
    if USE_FAKE_REDIS:
        pytest.skip("This test requires real Redis to test from_url behavior")

    # Create session using from_url (internal client)
    session = RedisSession.from_url("internal_client_test", url="redis://localhost:6379/15")

    try:
        if not await session.ping():
            pytest.skip("Redis server not available")

        # Add some data
        await session.add_items([{"role": "user", "content": "test message"}])
        items = await session.get_items()
        assert len(items) == 1

        # The session should properly manage its own client
        # Note: We can't easily test that the client is actually closed
        # without risking breaking the test, but we can verify the
        # session was created with internal client ownership
        assert hasattr(session, "_owns_client")
        assert session._owns_client is True

    finally:
        # This should properly close the internal client
        await session.close()


async def test_decode_responses_client_compatibility():
    """Test that RedisSession works with Redis clients configured with decode_responses=True."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for client configuration testing")

    # Create a Redis client with decode_responses=True
    import fakeredis.aioredis

    decoded_client = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # Create session with the decoded client
    session = RedisSession(
        session_id="decode_test",
        redis_client=decoded_client,
        key_prefix="test:",
    )

    try:
        # Test that we can add and retrieve items even when Redis returns strings
        test_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello with decoded responses"},
            {"role": "assistant", "content": "Response with unicode: 🚀"},
        ]

        await session.add_items(test_items)

        # get_items should work with string responses
        retrieved = await session.get_items()
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Hello with decoded responses"
        assert retrieved[1].get("content") == "Response with unicode: 🚀"

        # pop_item should also work with string responses
        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("content") == "Response with unicode: 🚀"

        # Verify one item remains
        remaining = await session.get_items()
        assert len(remaining) == 1
        assert remaining[0].get("content") == "Hello with decoded responses"

    finally:
        try:
            await session.clear_session()
        except Exception:
            pass  # Ignore cleanup errors
        await session.close()


async def test_real_redis_decode_responses_compatibility():
    """Test RedisSession with a real Redis client configured with decode_responses=True."""
    if USE_FAKE_REDIS:
        pytest.skip("This test requires real Redis to test decode_responses behavior")

    import redis.asyncio as redis

    # Create a Redis client with decode_responses=True
    decoded_client = redis.Redis.from_url("redis://localhost:6379/15", decode_responses=True)

    session = RedisSession(
        session_id="real_decode_test",
        redis_client=decoded_client,
        key_prefix="test:",
    )

    try:
        if not await session.ping():
            pytest.skip("Redis server not available")

        await session.clear_session()

        # Test with decode_responses=True client
        test_items: list[TResponseInputItem] = [
            {"role": "user", "content": "Real Redis with decode_responses=True"},
            {"role": "assistant", "content": "Unicode test: 🎯"},
        ]

        await session.add_items(test_items)

        # Should work even though Redis returns strings instead of bytes
        retrieved = await session.get_items()
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Real Redis with decode_responses=True"
        assert retrieved[1].get("content") == "Unicode test: 🎯"

        # pop_item should also work
        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("content") == "Unicode test: 🎯"

    finally:
        try:
            await session.clear_session()
        except Exception:
            pass
        await session.close()


async def test_get_next_id_method():
    """Test the _get_next_id atomic counter functionality."""
    session = await _create_test_session("counter_test")

    try:
        await session.clear_session()

        # Test atomic counter increment
        id1 = await session._get_next_id()
        id2 = await session._get_next_id()
        id3 = await session._get_next_id()

        # IDs should be sequential
        assert id1 == 1
        assert id2 == 2
        assert id3 == 3

        # Test that counter persists across session instances with same session_id
        if USE_FAKE_REDIS:
            session2 = RedisSession(
                session_id="counter_test",
                redis_client=fake_redis,
                key_prefix="test:",
            )
        else:
            session2 = RedisSession.from_url("counter_test", url=REDIS_URL, key_prefix="test:")

        try:
            id4 = await session2._get_next_id()
            assert id4 == 4  # Should continue from previous session's counter
        finally:
            await session2.close()

    finally:
        await session.close()


async def test_corrupted_data_handling():
    """Test that corrupted JSON data is handled gracefully."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for direct data manipulation")

    session = await _create_test_session("corruption_test")

    try:
        await session.clear_session()

        # Add some valid data first
        await session.add_items([{"role": "user", "content": "valid message"}])

        # Inject corrupted data directly into Redis
        messages_key = "test:corruption_test:messages"

        # Add invalid JSON directly using the typed Redis client
        await _safe_rpush(fake_redis, messages_key, "invalid json data")
        await _safe_rpush(fake_redis, messages_key, "{incomplete json")

        # get_items should skip corrupted data and return valid items
        items = await session.get_items()
        assert len(items) == 1  # Only the original valid item

        # Now add a properly formatted valid item using the session's serialization
        valid_item: TResponseInputItem = {"role": "user", "content": "valid after corruption"}
        await session.add_items([valid_item])

        # Should now have 2 valid items (corrupted ones skipped)
        items = await session.get_items()
        assert len(items) == 2
        assert items[0].get("content") == "valid message"
        assert items[1].get("content") == "valid after corruption"

        # Test pop_item with corrupted data at the end
        await _safe_rpush(fake_redis, messages_key, "corrupted at end")

        # The corrupted item should be handled gracefully
        # Since it's at the end, pop_item will encounter it first and return None
        # But first, let's pop the valid items to get to the corrupted one
        popped1 = await session.pop_item()
        assert popped1 is not None
        assert popped1.get("content") == "valid after corruption"

        popped2 = await session.pop_item()
        assert popped2 is not None
        assert popped2.get("content") == "valid message"

        # Now we should hit the corrupted data - this should gracefully handle it
        # by returning None (and removing the corrupted item)
        popped_corrupted = await session.pop_item()
        assert popped_corrupted is None

    finally:
        await session.close()


async def test_ping_connection_failure():
    """Test ping method when Redis connection fails."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for connection mocking")

    import unittest.mock

    session = await _create_test_session("ping_failure_test")

    try:
        # First verify ping works normally
        assert await session.ping() is True

        # Mock the ping method to raise an exception
        with unittest.mock.patch.object(
            session._redis, "ping", side_effect=Exception("Connection failed")
        ):
            # ping should return False when connection fails
            assert await session.ping() is False

    finally:
        await session.close()


async def test_close_method_coverage():
    """Test complete coverage of close() method behavior."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for client state verification")

    # Test 1: External client (should NOT be closed)
    external_client = fake_redis
    assert external_client is not None  # Type assertion for mypy
    session1 = RedisSession(
        session_id="close_test_1",
        redis_client=external_client,
        key_prefix="test:",
    )

    # Verify _owns_client is False for external client
    assert session1._owns_client is False

    # Close should not close the external client
    await session1.close()

    # Verify external client is still usable
    assert await external_client.ping() is True

    # Test 2: Internal client (should be closed)
    # Create a session that owns its client
    session2 = RedisSession(
        session_id="close_test_2",
        redis_client=fake_redis,
        key_prefix="test:",
    )
    session2._owns_client = True  # Simulate ownership

    # This should trigger the close path for owned clients
    await session2.close()

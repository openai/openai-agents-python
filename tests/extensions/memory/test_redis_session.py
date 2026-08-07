from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable
from typing import Any, cast

import pytest

pytest.importorskip("redis")  # Skip tests if Redis is not installed

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory.redis_session import RedisSession
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Keep the fallback-to-real-Redis path isolated from xdist workers.
pytestmark = [pytest.mark.asyncio, pytest.mark.serial]


def _assert_cancel_message(exc: asyncio.CancelledError, expected: str) -> None:
    """Account for Python 3.10 dropping Task cancellation messages when re-awaited."""
    expected_args = (expected,) if sys.version_info >= (3, 11) else ()
    assert exc.args == expected_args


async def _release_after_detaching_pipeline_connection(delegate: Any) -> None:
    """Model Redis 8.1 clearing its pipeline reference before awaiting release."""
    connection = delegate.connection
    delegate.connection = None
    if connection is not None:
        await delegate.connection_pool.release(connection)


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
        session = RedisSession.from_url(session_id, url=REDIS_URL, key_prefix="test:")

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


@pytest.mark.parametrize("operation", ["pop", "clear"])
async def test_mutation_cancellation_waits_for_authoritative_outcome(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Cancellation must wait after Redis applies a destructive mutation."""
    session = await _create_test_session()
    item: TResponseInputItem = {"role": "user", "content": "once"}
    await session.add_items([item])
    mutation_applied = asyncio.Event()
    allow_return = asyncio.Event()

    if operation == "pop":
        original_rpop = session._redis.rpop

        async def controlled_rpop(*args: Any, **kwargs: Any) -> Any:
            result = await cast(Awaitable[Any], original_rpop(*args, **kwargs))
            mutation_applied.set()
            await allow_return.wait()
            return result

        monkeypatch.setattr(session._redis, "rpop", controlled_rpop)
        task: asyncio.Task[Any] = asyncio.create_task(session.pop_item())
    else:
        original_delete = session._redis.delete

        async def controlled_delete(*args: Any, **kwargs: Any) -> Any:
            result = await original_delete(*args, **kwargs)
            mutation_applied.set()
            await allow_return.wait()
            return result

        monkeypatch.setattr(session._redis, "delete", controlled_delete)
        task = asyncio.create_task(session.clear_session())

    try:
        await mutation_applied.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        allow_return.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        allow_return.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert await session.get_items() == []


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
        assert await shared_client.ping() is True  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context

        # Close the session
        await session.close()

        # Verify the shared client is still usable after session.close()
        # This would fail if we incorrectly closed the external client
        assert await shared_client.ping() is True  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context

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


async def test_add_items_preserves_created_at_metadata(monkeypatch: pytest.MonkeyPatch):
    """`created_at` must be set once and not overwritten by subsequent add_items calls."""
    session = await _create_test_session("created_at_test")
    current_time = 1_000
    monkeypatch.setattr("agents.extensions.memory.redis_session.time.time", lambda: current_time)

    try:
        await session.clear_session()
        await session.add_items([{"role": "user", "content": "first"}])
        first_meta = await session._redis.hgetall(session._session_key)  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context
        first_created = first_meta.get(b"created_at") or first_meta.get("created_at")
        assert first_created is not None

        # Advance the controlled clock so a regression would surface as a different value.
        current_time += 1

        await session.add_items([{"role": "user", "content": "second"}])
        second_meta = await session._redis.hgetall(session._session_key)  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context
        second_created = second_meta.get(b"created_at") or second_meta.get("created_at")
        second_updated = second_meta.get(b"updated_at") or second_meta.get("updated_at")

        assert second_created == first_created, "created_at must remain stable"
        assert second_updated != first_created, "updated_at must advance on writes"
    finally:
        await session.close()


async def test_get_items_limit_skips_corrupt_newest_messages():
    """limit counts valid items, expanding past corrupt newest messages."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for direct data manipulation")

    session = await _create_test_session("limit_corrupt_test")

    try:
        await session.clear_session()
        await session.add_items(
            [
                {"role": "user", "content": "valid 0"},
                {"role": "assistant", "content": "valid 1"},
                {"role": "user", "content": "valid 2"},
            ]
        )

        # Append a corrupt newest message.
        await _safe_rpush(fake_redis, session._messages_key, "not valid json {{{")

        limited = await session.get_items(limit=2)
        assert [item.get("content") for item in limited] == ["valid 1", "valid 2"]
    finally:
        await session.close()


async def test_get_items_limit_returns_fewer_when_history_exhausted():
    """Window expansion stops at the end of history instead of looping."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for direct data manipulation")

    session = await _create_test_session("limit_exhausted_test")

    try:
        await session.clear_session()
        await session.add_items([{"role": "user", "content": "only valid"}])

        retrieved = await session.get_items(limit=5)
        assert [item.get("content") for item in retrieved] == ["only valid"]
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
        messages_key = session._messages_key

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

        # Test pop_item with corrupted data at the end.
        await _safe_rpush(fake_redis, messages_key, "corrupted at end")

        # The corrupted item should be dropped and pop_item should keep looking
        # for the next valid item.
        popped1 = await session.pop_item()
        assert popped1 is not None
        assert popped1.get("content") == "valid after corruption"

        popped2 = await session.pop_item()
        assert popped2 is not None
        assert popped2.get("content") == "valid message"

        # All corrupt items were removed while looking for valid messages.
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
    assert await external_client.ping() is True  # type: ignore[misc]  # Redis library returns Union[Awaitable[T], T] in async context

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


# ============================================================================
# SessionSettings Tests
# ============================================================================


async def test_session_settings_default():
    """Test that session_settings defaults to empty SessionSettings."""
    from agents.memory import SessionSettings

    session = await _create_test_session()

    try:
        # Should have default SessionSettings
        assert isinstance(session.session_settings, SessionSettings)
        assert session.session_settings.limit is None
    finally:
        await session.close()


@pytest.mark.parametrize("use_dictionary", [False, True], ids=["class", "dictionary"])
async def test_session_settings_constructor(use_dictionary: bool):
    """Test passing session_settings via constructor."""
    from agents.memory import SessionSettings

    if USE_FAKE_REDIS:
        session = RedisSession(
            session_id="settings_test",
            redis_client=fake_redis,
            key_prefix="test:",
            session_settings={"limit": 5} if use_dictionary else SessionSettings(limit=5),
        )
    else:
        session = RedisSession.from_url(
            "settings_test",
            url=REDIS_URL,
            session_settings={"limit": 5} if use_dictionary else SessionSettings(limit=5),
        )

    try:
        assert isinstance(session.session_settings, SessionSettings)
        assert session.session_settings.limit == 5
    finally:
        await session.close()


async def test_session_settings_from_url():
    """Test passing session_settings via from_url."""
    if USE_FAKE_REDIS:
        pytest.skip("from_url test requires real Redis server")

    from agents.memory import SessionSettings

    session = RedisSession.from_url(
        "from_url_settings_test", url=REDIS_URL, session_settings=SessionSettings(limit=10)
    )

    try:
        if not await session.ping():
            pytest.skip("Redis server not available")
        assert session.session_settings is not None
        assert session.session_settings.limit == 10
    finally:
        await session.close()


async def test_get_items_uses_session_settings_limit():
    """Test that get_items uses session_settings.limit as default."""
    from agents.memory import SessionSettings

    if USE_FAKE_REDIS:
        session = RedisSession(
            session_id="uses_settings_limit_test",
            redis_client=fake_redis,
            key_prefix="test:",
            session_settings=SessionSettings(limit=3),
        )
    else:
        session = RedisSession.from_url(
            "uses_settings_limit_test", url=REDIS_URL, session_settings=SessionSettings(limit=3)
        )

    try:
        await session.clear_session()

        # Add 5 items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(5)
        ]
        await session.add_items(items)

        # get_items() with no limit should use session_settings.limit=3
        retrieved = await session.get_items()
        assert len(retrieved) == 3
        # Should get the last 3 items
        assert retrieved[0].get("content") == "Message 2"
        assert retrieved[1].get("content") == "Message 3"
        assert retrieved[2].get("content") == "Message 4"
    finally:
        await session.close()


async def test_get_items_explicit_limit_overrides_session_settings():
    """Test that explicit limit parameter overrides session_settings."""
    from agents.memory import SessionSettings

    if USE_FAKE_REDIS:
        session = RedisSession(
            session_id="explicit_override_test",
            redis_client=fake_redis,
            key_prefix="test:",
            session_settings=SessionSettings(limit=5),
        )
    else:
        session = RedisSession.from_url(
            "explicit_override_test", url=REDIS_URL, session_settings=SessionSettings(limit=5)
        )

    try:
        await session.clear_session()

        # Add 10 items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": f"Message {i}"} for i in range(10)
        ]
        await session.add_items(items)

        # Explicit limit=2 should override session_settings.limit=5
        retrieved = await session.get_items(limit=2)
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Message 8"
        assert retrieved[1].get("content") == "Message 9"
    finally:
        await session.close()


async def test_session_settings_resolve():
    """Test SessionSettings.resolve() method."""
    from agents.memory import SessionSettings

    base = SessionSettings(limit=100)
    override = SessionSettings(limit=50)

    final = base.resolve(override)

    assert final.limit == 50  # Override wins
    assert base.limit == 100  # Original unchanged

    # Resolving with None returns self
    final_none = base.resolve(None)
    assert final_none.limit == 100


async def test_runner_with_session_settings_override():
    """Test that RunConfig can override session's default settings."""
    from agents import Agent, RunConfig, Runner
    from agents.memory import SessionSettings
    from tests.fake_model import FakeModel
    from tests.test_responses import get_text_message

    if USE_FAKE_REDIS:
        session = RedisSession(
            session_id="runner_override_test",
            redis_client=fake_redis,
            key_prefix="test:",
            session_settings=SessionSettings(limit=100),
        )
    else:
        session = RedisSession.from_url(
            "runner_override_test", url=REDIS_URL, session_settings=SessionSettings(limit=100)
        )

    try:
        await session.clear_session()

        # Add some history
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
                session_settings=SessionSettings(limit=2)  # Override to 2
            ),
        )

        # Verify the agent received only the last 2 history items + new question
        last_input = model.last_turn_args["input"]
        # Filter out the new "New question" input
        history_items = [item for item in last_input if item.get("content") != "New question"]
        # Should have 2 history items (last two from the 10 we added)
        assert len(history_items) == 2
    finally:
        await session.close()


def _create_owned_redis_session(session_id: str) -> RedisSession:
    """Create a session that owns its client, mirroring the from_url path."""
    import unittest.mock

    import fakeredis.aioredis

    with unittest.mock.patch(
        "agents.extensions.memory.redis_session.redis.from_url",
        return_value=fakeredis.aioredis.FakeRedis(),
    ):
        return RedisSession.from_url(session_id, url="redis://localhost:6379/15")


async def test_redis_session_closed_operations_raise_runtime_error():
    """Operations on a closed owned-client session must fail instead of reconnecting."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for isolated close-state verification")

    session = _create_owned_redis_session("closed_state_test")
    assert session._owns_client is True
    await session.add_items([{"role": "user", "content": "before close"}])
    await session.close()

    with pytest.raises(RuntimeError, match="RedisSession is closed"):
        await session.get_items()

    with pytest.raises(RuntimeError, match="RedisSession is closed"):
        await session.add_items([{"role": "user", "content": "after close"}])

    with pytest.raises(RuntimeError, match="RedisSession is closed"):
        await session.pop_item()

    with pytest.raises(RuntimeError, match="RedisSession is closed"):
        await session.clear_session()


async def test_redis_session_closed_rejects_empty_add_items():
    """add_items([]) must not bypass the closed check through the empty-list fast path."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for isolated close-state verification")

    session = _create_owned_redis_session("closed_empty_add_test")
    await session.close()

    with pytest.raises(RuntimeError, match="RedisSession is closed"):
        await session.add_items([])


async def test_redis_session_close_is_idempotent():
    """Repeated and concurrent close() calls must remain safe no-ops."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for isolated close-state verification")

    import asyncio

    session = _create_owned_redis_session("close_idempotent_test")

    await asyncio.gather(session.close(), session.close())
    await session.close()

    with pytest.raises(RuntimeError, match="RedisSession is closed"):
        await session.get_items()


async def test_redis_session_failed_cleanup_is_terminal_and_retried():
    """A failed cleanup keeps the session terminal, and the next close() retries it."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for isolated close-state verification")

    import unittest.mock

    session = _create_owned_redis_session("failed_cleanup_test")
    attempts = 0
    real_aclose = session._redis.aclose

    async def failing_then_succeeding_aclose(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        # Mirror redis-py, which tears the pool down before surfacing the error.
        await real_aclose(*args, **kwargs)
        if attempts == 1:
            raise OSError("disconnect error surfaced after pool teardown")

    with unittest.mock.patch.object(session._redis, "aclose", failing_then_succeeding_aclose):
        with pytest.raises(OSError, match="disconnect error surfaced after pool teardown"):
            await session.close()

        assert session._closed is True
        for operation in (
            session.get_items(),
            session.add_items([{"role": "user", "content": "after failed cleanup"}]),
            session.pop_item(),
            session.clear_session(),
            session.ping(),
        ):
            with pytest.raises(RuntimeError, match="RedisSession is closed"):
                await operation

        await session.close()
        assert attempts == 2
        assert session._client_released is True

        await session.close()
        assert attempts == 2


async def test_redis_session_cancelled_cleanup_is_terminal_and_retried():
    """A cancelled cleanup keeps the session terminal, and the next close() retries it."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for isolated close-state verification")

    import asyncio
    import unittest.mock
    from contextlib import suppress

    timeout = 5.0
    session = _create_owned_redis_session("cancelled_cleanup_test")
    attempts = 0
    entered_cleanup = asyncio.Event()
    real_aclose = session._redis.aclose

    async def hanging_then_succeeding_aclose(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered_cleanup.set()
            await asyncio.Event().wait()  # Hang until cancelled.
        return await real_aclose(*args, **kwargs)

    close_task: asyncio.Task[None] | None = None
    try:
        with unittest.mock.patch.object(session._redis, "aclose", hanging_then_succeeding_aclose):
            close_task = asyncio.create_task(session.close())
            await asyncio.wait_for(entered_cleanup.wait(), timeout)
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            close_task = None

            assert session._closed is True
            with pytest.raises(RuntimeError, match="RedisSession is closed"):
                await session.get_items()

            await asyncio.wait_for(session.close(), timeout)
            assert attempts == 2
            assert session._client_released is True
    finally:
        if close_task is not None:
            close_task.cancel()
            with suppress(asyncio.CancelledError):
                await close_task


async def test_redis_session_ping_raises_after_close():
    """ping() must not reopen a connection on a client this session already closed."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for isolated close-state verification")

    session = _create_owned_redis_session("ping_after_close_test")
    assert await session.ping() is True
    await session.close()

    with pytest.raises(RuntimeError, match="RedisSession is closed"):
        await session.ping()


async def test_redis_session_close_is_noop_for_injected_client():
    """With an injected client, close() stays a no-op and the session remains usable."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for isolated close-state verification")

    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis()
    session = RedisSession(
        session_id="injected_client_noop_test",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    assert session._owns_client is False

    await session.add_items([{"role": "user", "content": "before close"}])
    await session.close()

    await session.add_items([{"role": "user", "content": "after close"}])
    items = await session.get_items()
    assert [item.get("content") for item in items] == ["before close", "after close"]

    assert await session.ping() is True

    other = RedisSession(
        session_id="injected_client_noop_test",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    assert len(await other.get_items()) == 2

    await session.clear_session()


async def test_add_items_applies_ttl_in_the_write_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL setup must not create a post-commit failure window for add_items."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for pipeline instrumentation")

    client = fakeredis.aioredis.FakeRedis()
    session = RedisSession(
        session_id="atomic_ttl",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=60,
    )
    real_pipeline = client.pipeline
    execute_calls = 0

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)

        class PipelineProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(delegate, name)

            def __setattr__(self, name: str, value: Any) -> None:
                setattr(delegate, name, value)

            async def execute(self) -> Any:
                nonlocal execute_calls
                execute_calls += 1
                result = await delegate.execute()
                if execute_calls == 2:
                    raise RuntimeError("post-commit TTL failure")
                return result

        return PipelineProxy()

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "once"}

    await session.add_items([item])

    assert execute_calls == 1
    assert await session.get_items() == [item]


async def test_add_items_rejects_unrepresentable_ttl_before_writing() -> None:
    """An invalid Redis TTL must not turn a failed write retry into duplicates."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis()
    session = RedisSession(
        session_id="invalid_ttl",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=2**63,
    )
    item: TResponseInputItem = {"role": "user", "content": "never committed"}

    for _ in range(2):
        with pytest.raises(ValueError, match="outside Redis's supported expiration range"):
            await session.add_items([item])

    assert await session.get_items() == []


async def test_add_items_rejects_wrong_metadata_key_type_before_writing() -> None:
    """A metadata type error must not commit history that a retry duplicates."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.exceptions import ResponseError

    client = fakeredis.aioredis.FakeRedis()
    session = RedisSession(
        session_id="wrong_metadata_type",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    await client.set(session._session_key, "not a hash")
    item: TResponseInputItem = {"role": "user", "content": "never committed"}

    for _ in range(2):
        with pytest.raises(ResponseError, match="metadata key must contain a hash"):
            await session.add_items([item])

    assert await session.get_items() == []


async def test_add_items_uses_server_absolute_expiration_after_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL validation must not become stale between serialization and Redis execution."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis()
    server_seconds = 1_750_000_000
    server_microseconds = 500_000
    server_time_ms = server_seconds * 1000 + server_microseconds // 1000
    ttl = (2**63 - 1 - server_time_ms) // 1000
    expected_expiration_ms = server_time_ms + ttl * 1000
    order: list[str] = []
    real_pipeline = client.pipeline

    session = RedisSession(
        session_id="absolute_ttl",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=ttl,
    )

    async def serialize(item: TResponseInputItem) -> str:
        order.append("serialize")
        return json.dumps(item)

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)

        class PipelineProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(delegate, name)

            async def time(self) -> tuple[int, int]:
                order.append("time")
                return server_seconds, server_microseconds

        return PipelineProxy()

    monkeypatch.setattr(session, "_serialize_item", serialize)
    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "once"}

    await session.add_items([item])

    assert order == ["serialize", "time"]
    assert -(2**63) <= expected_expiration_ms <= 2**63 - 1
    assert await session.get_items() == [item]


async def test_add_items_refreshes_server_timestamps_after_watch_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watched retry must derive metadata and expiration from its own attempt."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis()
    ttl = 60
    first_seconds = int(time.time())
    second_seconds = first_seconds + ttl * 2
    server_times = iter([(first_seconds, 0), (second_seconds, 0)])
    expirations: list[int] = []
    pipeline_attempt = 0
    real_pipeline = client.pipeline

    session = RedisSession(
        session_id="watch_retry_timestamps",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=ttl,
    )

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        nonlocal pipeline_attempt
        pipeline_attempt += 1
        attempt = pipeline_attempt
        delegate = real_pipeline(*args, **kwargs)

        class PipelineProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(delegate, name)

            def __setattr__(self, name: str, value: Any) -> None:
                setattr(delegate, name, value)

            def pexpireat(self, key: str, expiration_time_ms: int) -> Any:
                expirations.append(expiration_time_ms)
                return delegate.pexpireat(key, expiration_time_ms)

            async def time(self) -> tuple[int, int]:
                return next(server_times)

            async def execute(self) -> Any:
                if attempt == 1:
                    await client.hset(  # type: ignore[misc]
                        session._session_key, "concurrent_write", "1"
                    )
                return await delegate.execute()

        return PipelineProxy()

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "once"}

    await session.add_items([item])

    first_expiration = (first_seconds + ttl) * 1000
    second_expiration = (second_seconds + ttl) * 1000
    assert expirations == [first_expiration] * 3 + [second_expiration] * 3
    metadata = await client.hgetall(session._session_key)  # type: ignore[misc]
    updated_at = metadata.get(b"updated_at") or metadata.get("updated_at")
    assert updated_at in (str(second_seconds), str(second_seconds).encode())
    assert await session.get_items() == [item]


async def test_ambiguous_exec_watch_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-derived WatchError after a possible commit must remain ambiguous."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.exceptions import WatchError

    client = fakeredis.aioredis.FakeRedis()
    session = RedisSession(
        session_id="ambiguous_exec_watch_error",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline
    attempts = 0
    item: TResponseInputItem = {"role": "user", "content": "once"}
    serialized = json.dumps(item, separators=(",", ":"))

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        delegate = real_pipeline(*args, **kwargs)

        class PipelineProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(delegate, name)

            async def execute(self) -> Any:
                await client.rpush(session._messages_key, serialized)  # type: ignore[misc]
                raise WatchError("A ConnectionError occurred while watching one or more keys")

        return PipelineProxy()

    monkeypatch.setattr(client, "pipeline", pipeline)

    with pytest.raises(WatchError, match="ConnectionError"):
        await session.add_items([item])

    assert attempts == 1
    assert await session.get_items() == [item]


async def test_redis81_standard_pool_checkout_records_native_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained checkout path must preserve Redis 8.1 standard-pool metrics."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    import agents.extensions.memory.redis_session as redis_session_module

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id="redis81_standard_observability",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    counts: list[tuple[str, int]] = []
    create_times: list[float] = []
    real_release = pool.release

    class ConnectionState:
        IDLE = "idle"
        USED = "used"

    async def record_connection_count(
        *, pool_name: str, connection_state: str, counter: int
    ) -> None:
        assert pool_name == "test-pool"
        counts.append((connection_state, counter))

    async def record_connection_create_time(
        *, connection_pool: Any, duration_seconds: float
    ) -> None:
        assert connection_pool is pool
        create_times.append(duration_seconds)

    async def release(connection: Any) -> None:
        await real_release(connection)
        await record_connection_count(
            pool_name="test-pool", connection_state=ConnectionState.USED, counter=-1
        )
        await record_connection_count(
            pool_name="test-pool", connection_state=ConnectionState.IDLE, counter=1
        )

    connection_module = cast(Any, redis_session_module)._redis_connection_api
    has_native_release_metrics = hasattr(connection_module, "record_connection_count")
    monkeypatch.setattr(connection_module, "ConnectionState", ConnectionState, raising=False)
    monkeypatch.setattr(
        connection_module, "get_pool_name", lambda _pool: "test-pool", raising=False
    )
    monkeypatch.setattr(
        connection_module, "record_connection_count", record_connection_count, raising=False
    )
    monkeypatch.setattr(
        connection_module,
        "record_connection_create_time",
        record_connection_create_time,
        raising=False,
    )
    if not has_native_release_metrics:
        monkeypatch.setattr(pool, "release", release)

    await session.add_items([{"role": "user", "content": "once"}])

    assert sum(counter for state, counter in counts if state == ConnectionState.USED) == 0
    assert sum(counter for state, counter in counts if state == ConnectionState.IDLE) == 1
    assert len(create_times) == 1


@pytest.mark.parametrize("has_maintenance_lock", [False, True])
async def test_redis8_blocking_pool_checkout_preserves_native_timing(
    monkeypatch: pytest.MonkeyPatch,
    has_maintenance_lock: bool,
) -> None:
    """The retained checkout path must preserve Redis 8.0 and 8.1 timing hooks."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from contextlib import asynccontextmanager

    from redis.asyncio import BlockingConnectionPool

    import agents.extensions.memory.redis_session as redis_session_module

    seed_client = fakeredis.aioredis.FakeRedis()
    source_pool = seed_client.connection_pool
    pool = BlockingConnectionPool(
        max_connections=1,
        timeout=1,
        connection_class=source_pool.connection_class,
        **source_pool.connection_kwargs,
    )
    client = fakeredis.aioredis.FakeRedis(connection_pool=pool)
    session = RedisSession(
        session_id="redis81_blocking_observability",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    maintenance_entries = 0
    create_times: list[float] = []
    wait_times: list[float] = []

    @asynccontextmanager
    async def maybe_pool_lock() -> Any:
        nonlocal maintenance_entries
        maintenance_entries += 1
        yield

    async def record_connection_create_time(
        *, connection_pool: Any, duration_seconds: float
    ) -> None:
        assert connection_pool is pool
        create_times.append(duration_seconds)

    async def record_connection_wait_time(*, pool_name: str, duration_seconds: float) -> None:
        assert pool_name == "test-pool"
        wait_times.append(duration_seconds)

    connection_module = cast(Any, redis_session_module)._redis_connection_api
    if has_maintenance_lock:
        monkeypatch.setattr(pool, "_maybe_pool_lock", maybe_pool_lock, raising=False)
    monkeypatch.setattr(
        connection_module, "get_pool_name", lambda _pool: "test-pool", raising=False
    )
    monkeypatch.setattr(
        connection_module,
        "record_connection_create_time",
        record_connection_create_time,
        raising=False,
    )
    monkeypatch.setattr(
        connection_module,
        "record_connection_wait_time",
        record_connection_wait_time,
        raising=False,
    )

    await session.add_items([{"role": "user", "content": "once"}])

    assert maintenance_entries == int(has_maintenance_lock)
    assert len(create_times) == 1
    assert len(wait_times) == 1


async def test_add_items_with_ttl_supports_single_connection_pool() -> None:
    """WATCH and server time must share one caller-managed Redis connection."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="single_connection_ttl",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=60,
    )
    item: TResponseInputItem = {"role": "user", "content": "once"}

    await session.add_items([item])

    assert await session.get_items() == [item]


async def test_cancelled_watch_releases_single_connection_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation before an immediate command must discard the watched connection."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="cancelled_watch_cleanup",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=60,
    )
    real_pipeline = client.pipeline
    pool = client.connection_pool
    time_started = asyncio.Event()
    dirty_connection: Any = None

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)

        async def controlled_time() -> tuple[int, int]:
            nonlocal dirty_connection
            dirty_connection = delegate.connection
            assert dirty_connection is not None
            time_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(delegate, "time", controlled_time)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    add_task = asyncio.create_task(session.add_items([{"role": "user", "content": "cancelled"}]))
    try:
        await time_started.wait()
        add_task.cancel("first-pre-exec-cancel")
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await add_task
    finally:
        if not add_task.done():
            add_task.cancel()
            await asyncio.gather(add_task, return_exceptions=True)

    _assert_cancel_message(exc_info.value, "first-pre-exec-cancel")
    assert dirty_connection not in pool._in_use_connections
    assert dirty_connection not in pool._available_connections
    assert await session.get_items() == []
    assert await client.ping() is True  # type: ignore[misc]


async def test_cancelled_in_flight_watch_command_reconnects_before_pool_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation with an unread reply must discard the dirty connection."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="cancelled_in_flight_watch_command",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline
    pool = client.connection_pool
    real_release = pool.release
    response_read_started = asyncio.Event()
    dirty_connection: Any = None
    close_calls = 0
    released_connections: list[Any] = []

    async def track_release(connection: Any) -> None:
        released_connections.append(connection)
        await real_release(connection)

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_type = delegate.type

        async def controlled_type(key: str) -> Any:
            nonlocal close_calls, dirty_connection
            connection = delegate.connection
            assert connection is not None
            dirty_connection = connection
            real_read_response = connection.read_response
            real_close = connection._close
            read_calls = 0

            async def block_first_read(*args: Any, **kwargs: Any) -> Any:
                nonlocal read_calls
                read_calls += 1
                if read_calls == 1:
                    response_read_started.set()
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable")
                return await real_read_response(*args, **kwargs)

            def track_close() -> None:
                nonlocal close_calls
                close_calls += 1
                real_close()

            monkeypatch.setattr(connection, "read_response", block_first_read)
            monkeypatch.setattr(connection, "_close", track_close)
            return await real_type(key)

        monkeypatch.setattr(delegate, "type", controlled_type)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    monkeypatch.setattr(pool, "release", track_release)
    add_task = asyncio.create_task(session.add_items([{"role": "user", "content": "cancelled"}]))
    try:
        await response_read_started.wait()
        add_task.cancel("cancel-during-response-read")
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await add_task
    finally:
        if not add_task.done():
            add_task.cancel()
            await asyncio.gather(add_task, return_exceptions=True)

    _assert_cancel_message(exc_info.value, "cancel-during-response-read")
    assert dirty_connection is not None
    assert close_calls == 1
    assert released_connections == []
    assert dirty_connection not in pool._in_use_connections
    assert dirty_connection not in pool._available_connections
    assert await client.ping() is True  # type: ignore[misc]
    assert len(released_connections) == 1
    assert released_connections[0] is not dirty_connection
    assert await session.get_items() == []


@pytest.mark.parametrize("blocking_pool", [False, True])
@pytest.mark.parametrize("validation_fails", [False, True])
async def test_cancelled_connection_acquisition_is_discarded_before_release(
    monkeypatch: pytest.MonkeyPatch,
    blocking_pool: bool,
    validation_fails: bool,
) -> None:
    """Cancellation during pool validation must retain and discard the acquired identity."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.asyncio import BlockingConnectionPool
    from redis.event import AsyncAfterConnectionReleasedEvent

    pool: Any
    if blocking_pool:
        seed_client = fakeredis.aioredis.FakeRedis()
        source_pool = seed_client.connection_pool
        pool = BlockingConnectionPool(
            max_connections=1,
            timeout=1,
            connection_class=source_pool.connection_class,
            **source_pool.connection_kwargs,
        )
        client = fakeredis.aioredis.FakeRedis(connection_pool=pool)
    else:
        client = fakeredis.aioredis.FakeRedis(max_connections=1)
        pool = client.connection_pool

    session = RedisSession(
        session_id=f"cancelled_connection_acquisition_{blocking_pool}",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    dispatcher = pool._event_dispatcher
    assert dispatcher is not None
    listeners = dispatcher._event_listeners_mapping[AsyncAfterConnectionReleasedEvent]
    released_connections: list[Any] = []
    ensure_started = asyncio.Event()
    allow_ensure = asyncio.Event()
    dirty_connection: Any = None
    real_ensure_connection = pool.ensure_connection
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, Any]] = []

    class RecordingReleaseListener:
        async def listen(self, event: Any) -> None:
            released_connections.append(event.connection)

    async def controlled_ensure_connection(connection: Any) -> None:
        nonlocal dirty_connection
        if dirty_connection is None:
            dirty_connection = connection
            ensure_started.set()
            await allow_ensure.wait()
            if validation_fails:
                raise RuntimeError("connection validation failed")
        await real_ensure_connection(connection)

    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        [*listeners, RecordingReleaseListener()],
    )
    monkeypatch.setattr(pool, "ensure_connection", controlled_ensure_connection)
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    add_task = asyncio.create_task(session.add_items([{"role": "user", "content": "cancelled"}]))
    waiter: asyncio.Task[Any] | None = None
    try:
        await ensure_started.wait()
        add_task.cancel("cancel-during-acquisition")
        if blocking_pool:

            async def ping() -> Any:
                return await client.ping()  # type: ignore[misc]

            waiter = asyncio.create_task(ping())
            await asyncio.sleep(0)
        allow_ensure.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await add_task
        _assert_cancel_message(exc_info.value, "cancel-during-acquisition")

        assert dirty_connection is not None
        assert dirty_connection not in pool._in_use_connections
        assert dirty_connection not in pool._available_connections
        assert dirty_connection not in released_connections
        if waiter is not None:
            assert await waiter is True
        else:
            assert await client.ping() is True  # type: ignore[misc]
        assert released_connections
        assert all(connection is not dirty_connection for connection in released_connections)
        assert await session.get_items() == []
        await asyncio.sleep(0)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_exception_handler)
        allow_ensure.set()
        pending = [task for task in (add_task, waiter) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize(
    "cleanup_mode",
    ["internal_reset", "failing_internal_reset"],
)
async def test_post_commit_cancellation_propagates_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_mode: str,
) -> None:
    """Cancellation after EXEC must propagate once driver cleanup settles."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id=f"post_commit_cancellation_{cleanup_mode}",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline
    internal_reset_started = asyncio.Event()
    allow_internal_reset = asyncio.Event()

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset
        reset_calls = 0

        async def controlled_reset() -> None:
            nonlocal reset_calls
            reset_calls += 1
            if reset_calls == 1:
                internal_reset_started.set()
                await allow_internal_reset.wait()
            await real_reset()
            if cleanup_mode == "failing_internal_reset" and reset_calls == 1:
                raise RuntimeError("post-commit reset failed")

        monkeypatch.setattr(delegate, "reset", controlled_reset)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}
    add_task = asyncio.create_task(session.add_items([item]))
    try:
        await internal_reset_started.wait()
        add_task.cancel("first-post-commit-cancel")
        await asyncio.sleep(0)
        add_task.cancel("second-during-cleanup")
        await asyncio.sleep(0)
        allow_internal_reset.set()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await add_task
    finally:
        allow_internal_reset.set()
        if not add_task.done():
            add_task.cancel()
            await asyncio.gather(add_task, return_exceptions=True)

    assert await session.get_items() == [item]
    _assert_cancel_message(exc_info.value, "first-post-commit-cancel")
    assert add_task.cancelled()
    assert await client.ping() is True  # type: ignore[misc]


async def test_post_commit_response_callback_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response callback failure after EXEC must not duplicate the committed batch."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="post_commit_callback_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )

    def fail_rpush_callback(response: Any, **kwargs: Any) -> Any:
        raise RuntimeError("response callback failed")

    client.set_response_callback("RPUSH", fail_rpush_callback)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    monkeypatch.delitem(client.response_callbacks, "RPUSH")
    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_successful_batch_response_with_sibling_error_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful RPUSH remains committed when a sibling EXEC response fails."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="successful_batch_with_sibling_error",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_hset = delegate.hset
        hset_calls = 0

        def replace_updated_at_with_wrong_type(*args: Any, **kwargs: Any) -> Any:
            nonlocal hset_calls
            hset_calls += 1
            if hset_calls == 2:
                return delegate.incr(session._session_key)
            return real_hset(*args, **kwargs)

        monkeypatch.setattr(delegate, "hset", replace_updated_at_with_wrong_type)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_post_commit_reset_self_cancellation_does_not_cancel_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset child cancellation after EXEC must not impersonate caller cancellation."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="post_commit_reset_self_cancel",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset
        reset_calls = 0

        async def controlled_reset() -> None:
            nonlocal reset_calls
            reset_calls += 1
            if reset_calls == 1:
                raise asyncio.CancelledError("reset self-cancelled")
            await real_reset()

        monkeypatch.setattr(delegate, "reset", controlled_reset)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_post_commit_reset_failure_detaches_without_release_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset failure must detach rather than release through a reborrowing listener."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.event import AsyncAfterConnectionReleasedEvent

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id="post_commit_repeated_reset_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline
    dispatcher = pool._event_dispatcher
    assert dispatcher is not None
    listeners = dispatcher._event_listeners_mapping[AsyncAfterConnectionReleasedEvent]
    listener_calls = 0
    borrowed_connection: Any = None

    class ReborrowingFailingReleaseListener:
        async def listen(self, event: Any) -> None:
            nonlocal borrowed_connection, listener_calls
            listener_calls += 1
            borrowed_connection = await pool.get_connection()
            raise RuntimeError("release listener failed after reborrow")

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset
        reset_calls = 0

        async def fail_two_resets_before_release() -> None:
            nonlocal reset_calls
            reset_calls += 1
            if reset_calls <= 2:
                raise RuntimeError("reset failed before release")
            await real_reset()

        monkeypatch.setattr(delegate, "reset", fail_two_resets_before_release)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        [*listeners, ReborrowingFailingReleaseListener()],
    )
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert listener_calls == 0
    assert borrowed_connection is None
    assert not pool._in_use_connections
    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        listeners,
    )
    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_reconnect_required_release_detaches_without_listener_reborrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect-required identity must never enter shared-pool release."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.event import AsyncAfterConnectionReleasedEvent

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id="reconnect_required_release",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline
    dispatcher = pool._event_dispatcher
    assert dispatcher is not None
    listeners = dispatcher._event_listeners_mapping[AsyncAfterConnectionReleasedEvent]
    listener_calls = 0
    borrowed_connection: Any = None

    class ReborrowingFailingReleaseListener:
        async def listen(self, event: Any) -> None:
            nonlocal borrowed_connection, listener_calls
            listener_calls += 1
            borrowed_connection = await pool.get_connection()
            raise RuntimeError("release listener failed after reborrow")

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset

        async def mark_for_reconnect_before_reset() -> None:
            connection = delegate.connection
            assert connection is not None
            connection.mark_for_reconnect()
            await real_reset()

        monkeypatch.setattr(delegate, "reset", mark_for_reconnect_before_reset)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        [*listeners, ReborrowingFailingReleaseListener()],
    )
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert listener_calls == 0
    assert borrowed_connection is None
    assert not pool._in_use_connections
    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        listeners,
    )
    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_reconnect_marked_inside_release_failure_is_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect race before native transfer must retain cleanup ownership."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id="reconnect_during_release",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_release = pool.release
    real_pipeline = client.pipeline
    close_calls = 0
    detached_connection: Any = None

    async def redis81_raced_release(connection: Any) -> None:
        nonlocal close_calls, detached_connection
        detached_connection = connection
        real_close = connection._close

        def tracked_close() -> None:
            nonlocal close_calls
            close_calls += 1
            real_close()

        monkeypatch.setattr(connection, "_close", tracked_close)
        connection.mark_for_reconnect()
        pool._in_use_connections.remove(connection)
        raise RuntimeError("disconnect failed before pool transfer")

    monkeypatch.setattr(pool, "release", redis81_raced_release)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert detached_connection is not None
    assert close_calls == 1
    assert detached_connection not in pool._in_use_connections
    assert detached_connection not in pool._available_connections
    monkeypatch.setattr(pool, "release", real_release)
    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_post_commit_release_listener_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listener failure after pool release must not make a committed batch retryable."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.event import AsyncAfterConnectionReleasedEvent

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="post_commit_release_listener_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    dispatcher = client.connection_pool._event_dispatcher
    assert dispatcher is not None
    listeners = dispatcher._event_listeners_mapping[AsyncAfterConnectionReleasedEvent]

    class FailingReleaseListener:
        async def listen(self, event: Any) -> None:
            raise RuntimeError("release listener failed after pool return")

    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        [*listeners, FailingReleaseListener()],
    )
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        listeners,
    )
    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_pre_exec_close_failure_is_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed dirty-connection close must remain owned for close retry."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="pre_exec_disconnect_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=60,
    )
    real_pipeline = client.pipeline
    time_started = asyncio.Event()
    dirty_connection: Any = None
    real_close: Any = None

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)

        async def controlled_time() -> tuple[int, int]:
            nonlocal dirty_connection, real_close
            dirty_connection = delegate.connection
            assert dirty_connection is not None
            real_close = dirty_connection._close

            def fail_close() -> None:
                raise RuntimeError("close failed")

            monkeypatch.setattr(dirty_connection, "_close", fail_close)
            time_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(delegate, "time", controlled_time)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    add_task = asyncio.create_task(session.add_items([{"role": "user", "content": "cancelled"}]))
    await time_started.wait()
    add_task.cancel("caller-cancel")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await add_task

    _assert_cancel_message(exc_info.value, "caller-cancel")
    assert session._detached_connections == {dirty_connection}
    assert dirty_connection not in client.connection_pool._in_use_connections
    assert dirty_connection not in client.connection_pool._available_connections
    monkeypatch.setattr(dirty_connection, "_close", real_close)
    await session.close()
    assert session._detached_connections == set()
    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await client.ping() is True  # type: ignore[misc]
    assert await session.get_items() == []


async def test_pre_exec_cancellation_skips_pipeline_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A protocol-dirty cancellation must not expose the connection through reset."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="pre_exec_cancel_released_reset_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=60,
    )
    real_pipeline = client.pipeline
    time_started = asyncio.Event()

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)

        async def controlled_time() -> tuple[int, int]:
            time_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def fail_if_reset() -> None:
            raise AssertionError("dirty connection reached pipeline reset")

        monkeypatch.setattr(delegate, "time", controlled_time)
        monkeypatch.setattr(delegate, "reset", fail_if_reset)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    add_task = asyncio.create_task(session.add_items([{"role": "user", "content": "cancelled"}]))
    await time_started.wait()
    add_task.cancel("first-caller-cancel")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await add_task

    _assert_cancel_message(exc_info.value, "first-caller-cancel")
    assert await client.ping() is True  # type: ignore[misc]
    assert await session.get_items() == []


async def test_blocking_pool_cancellation_completion_owns_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation must not interrupt blocking-pool cleanup after it starts."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.asyncio import BlockingConnectionPool

    seed_client = fakeredis.aioredis.FakeRedis()
    source_pool = seed_client.connection_pool
    pool = BlockingConnectionPool(
        max_connections=1,
        timeout=1,
        connection_class=source_pool.connection_class,
        **source_pool.connection_kwargs,
    )
    client = fakeredis.aioredis.FakeRedis(connection_pool=pool)
    session = RedisSession(
        session_id="blocking_pool_cancelled_release",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=60,
    )
    real_pipeline = client.pipeline
    time_started = asyncio.Event()
    fail_time = asyncio.Event()
    reset_started = asyncio.Event()

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset

        async def controlled_time() -> tuple[int, int]:
            time_started.set()
            await fail_time.wait()
            raise RuntimeError("pre-execute failure")

        async def observed_reset() -> None:
            reset_started.set()
            await real_reset()

        monkeypatch.setattr(delegate, "time", controlled_time)
        monkeypatch.setattr(delegate, "reset", observed_reset)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    add_task = asyncio.create_task(session.add_items([{"role": "user", "content": "failed"}]))
    await time_started.wait()
    await pool._condition.acquire()
    try:
        fail_time.set()
        await reset_started.wait()
        add_task.cancel("caller-cancel")
        await asyncio.sleep(0)
    finally:
        pool._condition.release()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await add_task

    _assert_cancel_message(exc_info.value, "caller-cancel")
    assert not pool._in_use_connections
    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await client.ping() is True  # type: ignore[misc]
    assert await session.get_items() == []


async def test_ordinary_pool_cancellation_completion_owns_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation must not interrupt ordinary-pool cleanup after it starts."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id="ordinary_pool_cancelled_release",
        redis_client=cast("Redis", client),
        key_prefix="test:",
        ttl=60,
    )
    real_pipeline = client.pipeline
    time_started = asyncio.Event()
    fail_time = asyncio.Event()
    reset_started = asyncio.Event()
    allow_reset = asyncio.Event()

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset

        async def controlled_time() -> tuple[int, int]:
            time_started.set()
            await fail_time.wait()
            raise RuntimeError("pre-execute failure")

        async def controlled_reset() -> None:
            reset_started.set()
            await allow_reset.wait()
            await real_reset()

        monkeypatch.setattr(delegate, "time", controlled_time)
        monkeypatch.setattr(delegate, "reset", controlled_reset)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    add_task = asyncio.create_task(session.add_items([{"role": "user", "content": "failed"}]))
    try:
        await time_started.wait()
        fail_time.set()
        await reset_started.wait()
        add_task.cancel("caller-cancel")
        await asyncio.sleep(0)
        allow_reset.set()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await add_task
    finally:
        allow_reset.set()
        if not add_task.done():
            add_task.cancel()
            await asyncio.gather(add_task, return_exceptions=True)

    _assert_cancel_message(exc_info.value, "caller-cancel")
    assert not pool._in_use_connections
    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await client.ping() is True  # type: ignore[misc]
    assert await session.get_items() == []


async def test_transparent_overridden_pool_release_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom release that preserves pool ownership semantics remains supported."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id="overridden_pool_release",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_release = pool.release
    release_calls = 0

    async def transparent_release(connection: Any) -> None:
        nonlocal release_calls
        release_calls += 1
        await real_release(connection)

    monkeypatch.setattr(pool, "release", transparent_release)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert release_calls > 0
    assert not pool._in_use_connections
    monkeypatch.setattr(pool, "release", real_release)
    assert await session.get_items() == [item]


async def test_release_failure_after_reborrow_preserves_new_borrower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release-then-reborrow failure must not reclaim the new borrower's connection."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id="ambiguous_pool_release",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_release = pool.release
    real_pipeline = client.pipeline
    release_calls = 0
    borrowed_connection: Any = None

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        monkeypatch.setattr(
            delegate,
            "reset",
            lambda: _release_after_detaching_pipeline_connection(delegate),
        )
        return delegate

    async def transfer_reborrow_then_fail(connection: Any) -> None:
        nonlocal borrowed_connection, release_calls
        release_calls += 1
        await real_release(connection)
        if release_calls == 1:
            borrowed_connection = await pool.get_connection()
            assert borrowed_connection is connection
            raise RuntimeError("release failed after reborrow")

    monkeypatch.setattr(pool, "release", transfer_reborrow_then_fail)
    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert release_calls == 1
    assert borrowed_connection is not None
    assert borrowed_connection in pool._in_use_connections
    assert borrowed_connection not in pool._available_connections
    monkeypatch.setattr(pool, "release", real_release)
    monkeypatch.setattr(client, "pipeline", real_pipeline)
    await real_release(borrowed_connection)
    replacement_connection = await pool.get_connection()
    assert replacement_connection is borrowed_connection
    await real_release(replacement_connection)
    assert await session.get_items() == [item]


async def test_post_commit_disconnect_failure_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached failed-disconnect connection must not make a committed batch retryable."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="post_commit_disconnect_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        reset_calls = 0

        async def fail_disconnect_after_commit() -> None:
            nonlocal reset_calls
            reset_calls += 1
            if reset_calls == 1:
                connection = delegate.connection
                assert connection is not None
                connection.mark_for_reconnect()

                async def fail_disconnect(nowait: bool = False) -> None:
                    raise RuntimeError("disconnect failed")

                monkeypatch.setattr(connection, "disconnect", fail_disconnect)
            await _release_after_detaching_pipeline_connection(delegate)

        monkeypatch.setattr(delegate, "reset", fail_disconnect_after_commit)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


@pytest.mark.parametrize("cancelled", [False, True])
async def test_post_commit_detached_close_failure_is_quarantined(
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    """A non-reusable connection close failure must not make a commit retryable."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    pool = client.connection_pool
    session = RedisSession(
        session_id=f"post_commit_detached_close_failure_{cancelled}",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline
    reset_started = asyncio.Event()
    allow_reset = asyncio.Event()
    detached_connection: Any = None
    real_close: Any = None

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)

        async def fail_disconnect_and_close_after_commit() -> None:
            nonlocal detached_connection, real_close
            reset_started.set()
            await allow_reset.wait()
            if detached_connection is None:
                detached_connection = delegate.connection
                assert detached_connection is not None
                real_close = detached_connection._close
                detached_connection.mark_for_reconnect()

                async def fail_disconnect(nowait: bool = False) -> None:
                    raise RuntimeError("disconnect failed")

                def fail_close() -> None:
                    raise RuntimeError("close failed")

                monkeypatch.setattr(detached_connection, "disconnect", fail_disconnect)
                monkeypatch.setattr(detached_connection, "_close", fail_close)
            await _release_after_detaching_pipeline_connection(delegate)

        monkeypatch.setattr(delegate, "reset", fail_disconnect_and_close_after_commit)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}
    add_task = asyncio.create_task(session.add_items([item]))
    try:
        await reset_started.wait()
        if cancelled:
            add_task.cancel("caller-cancel")
            await asyncio.sleep(0)
        allow_reset.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await add_task
            _assert_cancel_message(exc_info.value, "caller-cancel")
        else:
            await add_task

        assert not pool._in_use_connections
        assert session._detached_connections == {detached_connection}
        monkeypatch.setattr(detached_connection, "_close", real_close)
        await session.close()
        assert session._detached_connections == set()
        assert await session.get_items() == [item]
        assert await client.ping() is True  # type: ignore[misc]
    finally:
        allow_reset.set()
        if not add_task.done():
            add_task.cancel()
            await asyncio.gather(add_task, return_exceptions=True)
        if detached_connection is not None and real_close is not None:
            monkeypatch.setattr(detached_connection, "_close", real_close)
        await session.close()


async def test_blocking_pool_listener_failure_notifies_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed blocking-pool release must notify a waiter despite listener failure."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.asyncio import BlockingConnectionPool
    from redis.event import AsyncAfterConnectionReleasedEvent

    seed_client = fakeredis.aioredis.FakeRedis()
    source_pool = seed_client.connection_pool
    pool = BlockingConnectionPool(
        max_connections=1,
        timeout=1,
        connection_class=source_pool.connection_class,
        **source_pool.connection_kwargs,
    )
    client = fakeredis.aioredis.FakeRedis(connection_pool=pool)
    session = RedisSession(
        session_id="blocking_pool_listener_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    dispatcher = pool._event_dispatcher
    assert dispatcher is not None
    listeners = dispatcher._event_listeners_mapping[AsyncAfterConnectionReleasedEvent]
    real_pipeline = client.pipeline
    reset_started = asyncio.Event()
    allow_reset = asyncio.Event()

    class FailFirstReleaseListener:
        def __init__(self) -> None:
            self.calls = 0

        async def listen(self, event: Any) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("release listener failed")

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset

        async def controlled_reset() -> None:
            reset_started.set()
            await allow_reset.wait()
            await real_reset()

        monkeypatch.setattr(delegate, "reset", controlled_reset)
        return delegate

    monkeypatch.setitem(
        dispatcher._event_listeners_mapping,
        AsyncAfterConnectionReleasedEvent,
        [*listeners, FailFirstReleaseListener()],
    )
    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}
    add_task = asyncio.create_task(session.add_items([item]))
    ping_task: asyncio.Task[Any] | None = None

    async def ping() -> Any:
        return await client.ping()  # type: ignore[misc]

    try:
        await reset_started.wait()
        ping_task = asyncio.create_task(ping())
        await asyncio.sleep(0)
        allow_reset.set()
        await add_task
        assert await ping_task is True
    finally:
        allow_reset.set()
        pending = [task for task in (add_task, ping_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await session.get_items() == [item]


async def test_blocking_pool_detached_disconnect_notifies_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing a detached blocking-pool connection must notify an existing waiter."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    from redis.asyncio import BlockingConnectionPool

    seed_client = fakeredis.aioredis.FakeRedis()
    source_pool = seed_client.connection_pool
    pool = BlockingConnectionPool(
        max_connections=1,
        timeout=1,
        connection_class=source_pool.connection_class,
        **source_pool.connection_kwargs,
    )
    client = fakeredis.aioredis.FakeRedis(connection_pool=pool)
    session = RedisSession(
        session_id="blocking_pool_detached_disconnect",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline
    reset_started = asyncio.Event()
    allow_reset = asyncio.Event()

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)

        async def fail_disconnect_after_commit() -> None:
            reset_started.set()
            await allow_reset.wait()
            connection = delegate.connection
            assert connection is not None
            connection.mark_for_reconnect()

            async def fail_disconnect(nowait: bool = False) -> None:
                raise RuntimeError("disconnect failed")

            monkeypatch.setattr(connection, "disconnect", fail_disconnect)
            await _release_after_detaching_pipeline_connection(delegate)

        monkeypatch.setattr(delegate, "reset", fail_disconnect_after_commit)
        return delegate

    async def ping() -> Any:
        return await client.ping()  # type: ignore[misc]

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}
    add_task = asyncio.create_task(session.add_items([item]))
    ping_task: asyncio.Task[Any] | None = None
    try:
        await reset_started.wait()
        ping_task = asyncio.create_task(ping())
        await asyncio.sleep(0)
        allow_reset.set()
        await add_task
        assert await ping_task is True
    finally:
        allow_reset.set()
        pending = [task for task in (add_task, ping_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    monkeypatch.setattr(client, "pipeline", real_pipeline)
    assert await session.get_items() == [item]


async def test_post_commit_internal_reset_failure_does_not_report_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure after a successful EXEC must not invite a duplicate retry."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis")

    client = fakeredis.aioredis.FakeRedis(max_connections=1)
    session = RedisSession(
        session_id="post_commit_reset_failure",
        redis_client=cast("Redis", client),
        key_prefix="test:",
    )
    real_pipeline = client.pipeline

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        delegate = real_pipeline(*args, **kwargs)
        real_reset = delegate.reset
        reset_calls = 0

        async def fail_first_reset_after_release() -> None:
            nonlocal reset_calls
            reset_calls += 1
            await real_reset()
            if reset_calls == 1:
                raise RuntimeError("post-commit reset failed")

        monkeypatch.setattr(delegate, "reset", fail_first_reset_after_release)
        return delegate

    monkeypatch.setattr(client, "pipeline", pipeline)
    item: TResponseInputItem = {"role": "user", "content": "committed"}

    await session.add_items([item])

    assert await session.get_items() == [item]
    assert await client.ping() is True  # type: ignore[misc]


async def test_redis_session_operation_waiting_behind_close_raises():
    """An operation queued behind close() must fail rather than run after shutdown completes."""
    if not USE_FAKE_REDIS:
        pytest.skip("This test requires fakeredis for controlled close interleaving")

    import asyncio
    import unittest.mock
    from contextlib import suppress

    import fakeredis.aioredis

    from agents.memory.session_settings import resolve_session_limit

    timeout = 5.0
    blocked_probe = 0.1
    fake_client = fakeredis.aioredis.FakeRedis()
    close_holds_lock = asyncio.Event()  # Set once close() is inside the session lock.
    release_close = asyncio.Event()  # Test-owned gate that lets the paused close() finish.
    op_entered = asyncio.Event()  # Set once the operation is past method entry.
    real_aclose = fake_client.aclose
    real_resolve = resolve_session_limit

    async def paused_aclose(*args: Any, **kwargs: Any) -> Any:
        close_holds_lock.set()
        await asyncio.wait_for(release_close.wait(), timeout)
        return await real_aclose(*args, **kwargs)

    def entry_signalling_resolve(*args: Any, **kwargs: Any) -> Any:
        # This runs immediately before the operation reaches the session lock.
        op_entered.set()
        return real_resolve(*args, **kwargs)

    with unittest.mock.patch(
        "agents.extensions.memory.redis_session.redis.from_url", return_value=fake_client
    ):
        session = RedisSession.from_url("close_interleave_test", url="redis://localhost:6379/15")
    assert session._owns_client is True

    close_task: asyncio.Task[None] | None = None
    op_task: asyncio.Task[list[TResponseInputItem]] | None = None
    try:
        with unittest.mock.patch.object(fake_client, "aclose", paused_aclose):
            close_task = asyncio.create_task(session.close())
            await asyncio.wait_for(close_holds_lock.wait(), timeout)

            # Start the operation while close() still holds the lock so it must queue behind it.
            with unittest.mock.patch(
                "agents.extensions.memory.redis_session.resolve_session_limit",
                entry_signalling_resolve,
            ):
                op_task = asyncio.create_task(session.get_items())
                await asyncio.wait_for(op_entered.wait(), timeout)

            # The operation is past method entry, so it can only be blocked on the session lock.
            _, still_blocked = await asyncio.wait({op_task}, timeout=blocked_probe)
            assert op_task in still_blocked

            release_close.set()
            await asyncio.wait_for(close_task, timeout)
            close_task = None

            with pytest.raises(RuntimeError, match="RedisSession is closed"):
                await asyncio.wait_for(op_task, timeout)
            op_task = None
    finally:
        release_close.set()
        for task in (close_task, op_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, RuntimeError):
                    await task

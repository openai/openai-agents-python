from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

pytest.importorskip("dapr")  # Skip tests if Dapr is not installed

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory import (
    DAPR_CONSISTENCY_EVENTUAL,
    DAPR_CONSISTENCY_STRONG,
    DaprSession,
)
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio


class FakeDaprClient:
    """Fake Dapr client for testing without real Dapr sidecar."""

    def __init__(self):
        self._state: dict[str, bytes] = {}
        self._etags: dict[str, str] = {}
        self._etag_counter = 0
        self._closed = False

    async def get_state(
        self,
        store_name: str,
        key: str,
        state_metadata: Any = None,
        state_options: Any = None,
    ) -> Mock:
        """Get state from in-memory store."""
        response = Mock()
        response.data = self._state.get(key, b"")
        # The Dapr SDK reports a missing etag as an empty string rather than None.
        response.etag = self._etags.get(key, "")
        return response

    async def save_state(
        self,
        store_name: str,
        key: str,
        value: str | bytes,
        state_metadata: dict[str, str] | None = None,
        options: Any = None,
        etag: str | None = None,
    ) -> None:
        """Save state to in-memory store."""
        concurrency = getattr(options, "concurrency", None)
        current_etag = self._etags.get(key)

        expects_match = False
        if concurrency is not None:
            concurrency_name = getattr(concurrency, "name", str(concurrency))
            expects_match = concurrency_name == "first_write"

        if expects_match:
            if current_etag is None:
                if etag not in (None, ""):
                    raise RuntimeError("etag mismatch: key does not exist")
            elif etag != current_etag:
                raise RuntimeError("etag mismatch: stale data")

        if isinstance(value, str):
            self._state[key] = value.encode("utf-8")
        else:
            self._state[key] = value

        self._etag_counter += 1
        self._etags[key] = str(self._etag_counter)

    async def delete_state(
        self,
        store_name: str,
        key: str,
        state_metadata: Any = None,
        options: Any = None,
    ) -> None:
        """Delete state from in-memory store."""
        if key in self._state:
            del self._state[key]
            self._etags.pop(key, None)

    async def close(self) -> None:
        """Mark client as closed."""
        self._closed = True


@pytest.fixture
def fake_dapr_client() -> FakeDaprClient:
    """Fixture for fake Dapr client."""
    return FakeDaprClient()


class ConflictFakeDaprClient(FakeDaprClient):
    """Fake client that simulates optimistic concurrency conflicts once per key."""

    def __init__(self):
        super().__init__()
        self._conflicted_keys: set[str] = set()

    def _simulate_concurrent_update(self, key: str) -> None:
        raw_payload = self._state.get(key, b"[]")
        try:
            decoded = json.loads(raw_payload.decode("utf-8"))
            if not isinstance(decoded, list):
                decoded = []
        except (json.JSONDecodeError, UnicodeDecodeError):
            decoded = []

        competitor_item = json.dumps(
            {"role": "assistant", "content": "from-concurrent-writer"},
            separators=(",", ":"),
        )
        decoded.append(competitor_item)
        self._state[key] = json.dumps(decoded, separators=(",", ":")).encode("utf-8")
        self._etag_counter += 1
        self._etags[key] = str(self._etag_counter)

    async def save_state(
        self,
        store_name: str,
        key: str,
        value: str | bytes,
        state_metadata: dict[str, str] | None = None,
        options: Any = None,
        etag: str | None = None,
    ) -> None:
        concurrency = getattr(options, "concurrency", None)
        concurrency_name = getattr(concurrency, "name", str(concurrency))
        current_etag = self._etags.get(key)

        if (
            concurrency_name == "first_write"
            and key.endswith(":messages")
            and current_etag is not None
            and key not in self._conflicted_keys
        ):
            self._conflicted_keys.add(key)
            self._simulate_concurrent_update(key)
            raise RuntimeError("etag mismatch: concurrent writer")

        await super().save_state(
            store_name=store_name,
            key=key,
            value=value,
            state_metadata=state_metadata,
            options=options,
            etag=etag,
        )


@pytest.fixture
def conflict_dapr_client() -> ConflictFakeDaprClient:
    """Fixture for fake client that forces concurrency conflicts."""
    return ConflictFakeDaprClient()


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


async def _create_test_session(
    fake_dapr_client: FakeDaprClient,
    session_id: str | None = None,
) -> DaprSession:
    """Helper to create a test session with cleanup."""
    import uuid

    if session_id is None:
        session_id = f"test_session_{uuid.uuid4().hex[:8]}"

    session = DaprSession(
        session_id=session_id,
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )

    # Clean up any existing data
    await session.clear_session()

    return session


async def test_dapr_session_direct_ops(fake_dapr_client: FakeDaprClient):
    """Test direct database operations of DaprSession."""
    session = await _create_test_session(fake_dapr_client)

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


async def test_runner_integration(agent: Agent, fake_dapr_client: FakeDaprClient):
    """Test that DaprSession works correctly with the agent Runner."""
    session = await _create_test_session(fake_dapr_client)

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


async def test_session_isolation(fake_dapr_client: FakeDaprClient):
    """Test that different session IDs result in isolated conversation histories."""
    session1 = DaprSession(
        session_id="session_1",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )
    session2 = DaprSession(
        session_id="session_2",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )

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


async def test_add_items_retries_on_concurrency(conflict_dapr_client: ConflictFakeDaprClient):
    """Ensure add_items retries after a simulated optimistic concurrency failure."""
    session = await _create_test_session(conflict_dapr_client, "concurrency_add")

    try:
        await session.add_items(
            [
                {"role": "user", "content": "seed"},
            ]
        )

        await session.add_items(
            [
                {"role": "assistant", "content": "new message"},
            ]
        )

        contents = [item.get("content") for item in await session.get_items()]
        assert contents == ["seed", "from-concurrent-writer", "new message"]
        assert session._messages_key in conflict_dapr_client._conflicted_keys
    finally:
        await session.close()


async def test_pop_item_retries_on_concurrency(conflict_dapr_client: ConflictFakeDaprClient):
    """Ensure pop_item retries after a simulated optimistic concurrency failure."""
    session = await _create_test_session(conflict_dapr_client, "concurrency_pop")

    try:
        await session.add_items(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]
        )

        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("content") == "from-concurrent-writer"

        contents = [item.get("content") for item in await session.get_items()]
        assert contents == ["first", "second"]
        assert session._messages_key in conflict_dapr_client._conflicted_keys
    finally:
        await session.close()


async def test_get_items_with_limit(fake_dapr_client: FakeDaprClient):
    """Test the limit parameter in get_items."""
    session = await _create_test_session(fake_dapr_client)

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


async def test_pop_from_empty_session(fake_dapr_client: FakeDaprClient):
    """Test that pop_item returns None on an empty session."""
    session = DaprSession(
        session_id="empty_session",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )
    try:
        await session.clear_session()
        popped = await session.pop_item()
        assert popped is None
    finally:
        await session.close()


async def test_get_items_limit_skips_corrupt_newest_entries(fake_dapr_client: FakeDaprClient):
    """limit counts valid items, expanding past corrupt newest entries."""
    session = await _create_test_session(fake_dapr_client, "limit_corrupt")

    try:
        serialized = [
            await session._serialize_item({"role": "user", "content": "valid 0"}),
            await session._serialize_item({"role": "assistant", "content": "valid 1"}),
            await session._serialize_item({"role": "user", "content": "valid 2"}),
            "not valid json {{{",
        ]
        fake_dapr_client._state[session._messages_key] = json.dumps(
            serialized, separators=(",", ":")
        ).encode("utf-8")

        limited = await session.get_items(limit=2)
        assert [item.get("content") for item in limited] == ["valid 1", "valid 2"]
    finally:
        await session.close()


async def test_get_items_limit_returns_fewer_when_history_exhausted(
    fake_dapr_client: FakeDaprClient,
):
    """A limit larger than the stored history returns what exists."""
    session = await _create_test_session(fake_dapr_client, "limit_exhausted")

    try:
        await session.add_items([{"role": "user", "content": "only valid"}])

        retrieved = await session.get_items(limit=5)
        assert [item.get("content") for item in retrieved] == ["only valid"]
    finally:
        await session.close()


async def test_pop_item_skips_corrupt_most_recent(fake_dapr_client: FakeDaprClient):
    """pop_item skips corrupt newest entries and returns the next valid item."""
    session = await _create_test_session(fake_dapr_client, "pop_corrupt")

    try:
        valid_item: TResponseInputItem = {"role": "user", "content": "valid"}
        fake_dapr_client._state[session._messages_key] = json.dumps(
            [await session._serialize_item(valid_item), "not valid json {{{"],
            separators=(",", ":"),
        ).encode("utf-8")

        assert await session.pop_item() == valid_item
        assert await session.get_items() == []
    finally:
        await session.close()


async def test_pop_item_returns_none_after_dropping_only_corrupt_entries(
    fake_dapr_client: FakeDaprClient,
):
    """pop_item removes corrupt entries and returns None when no valid items remain."""
    session = await _create_test_session(fake_dapr_client, "pop_only_corrupt")

    try:
        fake_dapr_client._state[session._messages_key] = json.dumps(
            ["not valid json {{{"],
            separators=(",", ":"),
        ).encode("utf-8")

        assert await session.pop_item() is None
        assert await session.get_items() == []
    finally:
        await session.close()


async def test_add_empty_items_list(fake_dapr_client: FakeDaprClient):
    """Test that adding an empty list of items is a no-op."""
    session = await _create_test_session(fake_dapr_client)

    try:
        initial_items = await session.get_items()
        assert len(initial_items) == 0

        await session.add_items([])

        items_after_add = await session.get_items()
        assert len(items_after_add) == 0

    finally:
        await session.close()


async def test_unicode_content(fake_dapr_client: FakeDaprClient):
    """Test that session correctly stores and retrieves unicode/non-ASCII content."""
    session = await _create_test_session(fake_dapr_client)

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


async def test_special_characters_and_json_safety(fake_dapr_client: FakeDaprClient):
    """Test that session safely stores and retrieves items with special characters."""
    session = await _create_test_session(fake_dapr_client)

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


async def test_data_integrity_with_problematic_strings(fake_dapr_client: FakeDaprClient):
    """Test that session preserves data integrity with strings that could break parsers."""
    session = await _create_test_session(fake_dapr_client)

    try:
        # Add items with various problematic string patterns
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

        # Retrieve all items and verify they are stored exactly as provided
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


async def test_concurrent_access(fake_dapr_client: FakeDaprClient):
    """Test concurrent access to the same session to verify data integrity."""
    import asyncio

    session = await _create_test_session(fake_dapr_client, "concurrent_test")

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

        # Check that all expected messages are present
        for expected in expected_messages:
            assert expected in contents

    finally:
        await session.close()


async def test_dapr_connectivity(fake_dapr_client: FakeDaprClient):
    """Test Dapr connectivity methods."""
    session = DaprSession(
        session_id="connectivity_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )
    try:
        # Test ping
        is_connected = await session.ping()
        assert is_connected is True
    finally:
        await session.close()


async def test_ttl_functionality(fake_dapr_client: FakeDaprClient):
    """Test TTL (time-to-live) functionality."""
    session = DaprSession(
        session_id="ttl_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
        ttl=3600,  # 1 hour TTL
    )

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

    finally:
        try:
            await session.clear_session()
        except Exception:
            pass  # Ignore cleanup errors
        await session.close()


async def test_consistency_levels(fake_dapr_client: FakeDaprClient):
    """Test different consistency levels."""
    # Test eventual consistency (default)
    session_eventual = DaprSession(
        session_id="eventual_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
        consistency=DAPR_CONSISTENCY_EVENTUAL,
    )

    # Test strong consistency
    session_strong = DaprSession(
        session_id="strong_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
        consistency=DAPR_CONSISTENCY_STRONG,
    )

    try:
        # Both should work the same way with fake client
        items: list[TResponseInputItem] = [{"role": "user", "content": "Test"}]

        await session_eventual.add_items(items)
        retrieved_eventual = await session_eventual.get_items()
        assert len(retrieved_eventual) == 1

        await session_strong.add_items(items)
        retrieved_strong = await session_strong.get_items()
        assert len(retrieved_strong) == 1

    finally:
        await session_eventual.close()
        await session_strong.close()


async def test_external_client_not_closed(fake_dapr_client: FakeDaprClient):
    """Test that external Dapr clients are not closed when session.close() is called."""
    # Create session with external client
    session = DaprSession(
        session_id="external_client_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )

    try:
        # Add some data to verify the client is working
        await session.add_items([{"role": "user", "content": "test message"}])
        items = await session.get_items()
        assert len(items) == 1

        # Close the session
        await session.close()

        # Verify the shared client is still usable after session.close()
        assert fake_dapr_client._closed is False

    finally:
        # Clean up
        try:
            await session.clear_session()
        except Exception:
            pass


async def test_internal_client_ownership(fake_dapr_client: FakeDaprClient):
    """Test that clients created via from_address are properly managed."""
    # Create a session that owns its client
    session = DaprSession(
        session_id="internal_client_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )
    session._owns_client = True  # Simulate ownership

    try:
        # Add some data
        await session.add_items([{"role": "user", "content": "test message"}])
        items = await session.get_items()
        assert len(items) == 1

        # Verify ownership flag
        assert session._owns_client is True

    finally:
        # This should close the internal client
        await session.close()
        assert fake_dapr_client._closed is True


@pytest.mark.parametrize(
    "raw_state",
    [
        b"invalid json data",
        b"\xff",
        json.dumps({"some": "object"}).encode("utf-8"),
    ],
)
async def test_add_items_rejects_corrupted_aggregate_state(
    fake_dapr_client: FakeDaprClient,
    raw_state: bytes,
):
    """Test that corrupted aggregate state is not overwritten by add_items."""
    session = await _create_test_session(fake_dapr_client, "corruption_test")

    try:
        await session.clear_session()

        # Add some valid data first.
        await session.add_items([{"role": "user", "content": "valid message"}])

        # Inject corrupted data directly into state store.
        messages_key = "corruption_test:messages"
        fake_dapr_client._state[messages_key] = raw_state

        # get_items should handle corrupted data gracefully.
        items = await session.get_items()
        assert len(items) == 0  # Corrupted data returns empty list

        # add_items should not overwrite the corrupted aggregate state.
        valid_item: TResponseInputItem = {"role": "user", "content": "valid after corruption"}
        with pytest.raises(ValueError, match="stored Dapr session messages"):
            await session.add_items([valid_item])
        assert fake_dapr_client._state[messages_key] == raw_state

    finally:
        await session.close()


async def test_ping_connection_failure(fake_dapr_client: FakeDaprClient):
    """Test ping method when Dapr connection fails."""
    session = await _create_test_session(fake_dapr_client, "ping_failure_test")

    try:
        # First verify ping works normally
        assert await session.ping() is True

        # Mock the get_state method to raise an exception
        original_get_state = fake_dapr_client.get_state

        def failing_get_state(*args, **kwargs):
            raise Exception("Connection failed")

        fake_dapr_client.get_state = failing_get_state  # type: ignore[method-assign]

        # ping should return False when connection fails
        assert await session.ping() is False

        # Restore original method
        fake_dapr_client.get_state = original_get_state  # type: ignore[method-assign]

    finally:
        await session.close()


async def test_close_method_coverage(fake_dapr_client: FakeDaprClient):
    """Test complete coverage of close() method behavior."""
    # Test 1: External client (should NOT be closed)
    session1 = DaprSession(
        session_id="close_test_1",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )

    # Verify _owns_client is False for external client
    assert session1._owns_client is False

    # Close should not close the external client
    await session1.close()

    # Verify external client is still usable
    assert fake_dapr_client._closed is False

    # Test 2: Internal client (should be closed)
    fake_dapr_client2 = FakeDaprClient()
    session2 = DaprSession(
        session_id="close_test_2",
        state_store_name="statestore",
        dapr_client=fake_dapr_client2,  # type: ignore[arg-type]
    )
    session2._owns_client = True  # Simulate ownership

    # This should trigger the close path for owned clients
    await session2.close()
    assert fake_dapr_client2._closed is True


async def test_messages_not_list_handling(fake_dapr_client: FakeDaprClient):
    """Test that non-list messages data is handled gracefully."""
    session = await _create_test_session(fake_dapr_client, "not_list_test")

    # Manually corrupt the state with non-list data
    corrupt_data = json.dumps({"some": "object"})
    fake_dapr_client._state[session._messages_key] = corrupt_data.encode("utf-8")

    # Should return empty list for corrupted data
    items = await session.get_items()
    assert len(items) == 0

    await session.close()


async def test_already_deserialized_messages(fake_dapr_client: FakeDaprClient):
    """Test handling of messages that are already dict objects."""
    session = await _create_test_session(fake_dapr_client, "deserialized_test")

    # Store messages as a list of dict objects (not JSON strings)
    messages_list = [
        {"role": "user", "content": "First message"},
        {"role": "assistant", "content": "Second message"},
    ]
    messages_json = json.dumps(messages_list)
    fake_dapr_client._state[session._messages_key] = messages_json.encode("utf-8")

    # Should handle both string and dict messages
    items = await session.get_items()
    assert len(items) == 2
    assert items[0]["content"] == "First message"  # type: ignore[typeddict-item]
    assert items[1]["content"] == "Second message"  # type: ignore[typeddict-item]

    await session.close()


async def test_context_manager(fake_dapr_client: FakeDaprClient):
    """Test that DaprSession works as an async context manager."""
    # Test that the context manager enters and exits properly
    async with DaprSession(
        "test_cm_session",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    ) as session:
        # Verify we got the session object back
        assert session.session_id == "test_cm_session"

        # Add some data
        await session.add_items([{"role": "user", "content": "Test message"}])
        items = await session.get_items()
        assert len(items) == 1
        assert items[0]["content"] == "Test message"  # type: ignore[typeddict-item]

    # After exiting context manager, close should have been called
    # Verify we can still check the state (fake client doesn't truly disconnect)
    assert fake_dapr_client._closed is False  # External client not closed

    # Test with owned client scenario (simulating from_address behavior)
    owned_session = DaprSession(
        "test_cm_owned",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
    )
    # Manually set ownership to simulate from_address behavior
    owned_session._owns_client = True

    async with owned_session:
        await owned_session.add_items([{"role": "user", "content": "Owned client test"}])
        items = await owned_session.get_items()
        assert len(items) == 1

    # Close should have been called automatically (though fake client doesn't track this)


# ============================================================================
# SessionSettings Tests
# ============================================================================


async def test_session_settings_default(fake_dapr_client: FakeDaprClient):
    """Test that session_settings defaults to empty SessionSettings."""
    from agents.memory import SessionSettings

    session = await _create_test_session(fake_dapr_client)

    try:
        # Should have default SessionSettings
        assert isinstance(session.session_settings, SessionSettings)
        assert session.session_settings.limit is None
    finally:
        await session.close()


@pytest.mark.parametrize("use_dictionary", [False, True], ids=["class", "dictionary"])
async def test_session_settings_constructor(fake_dapr_client: FakeDaprClient, use_dictionary: bool):
    """Test passing session_settings via constructor."""
    from agents.memory import SessionSettings

    session = DaprSession(
        session_id="settings_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
        session_settings={"limit": 5} if use_dictionary else SessionSettings(limit=5),
    )

    try:
        assert isinstance(session.session_settings, SessionSettings)
        assert session.session_settings.limit == 5
    finally:
        await session.close()


async def test_get_items_uses_session_settings_limit(fake_dapr_client: FakeDaprClient):
    """Test that get_items uses session_settings.limit as default."""
    from agents.memory import SessionSettings

    session = DaprSession(
        session_id="uses_settings_limit_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
        session_settings=SessionSettings(limit=3),
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


async def test_get_items_explicit_limit_overrides_session_settings(
    fake_dapr_client: FakeDaprClient,
):
    """Test that explicit limit parameter overrides session_settings."""
    from agents.memory import SessionSettings

    session = DaprSession(
        session_id="explicit_override_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
        session_settings=SessionSettings(limit=5),
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


async def test_runner_with_session_settings_override(fake_dapr_client: FakeDaprClient):
    """Test that RunConfig can override session's default settings."""
    from agents import Agent, RunConfig, Runner
    from agents.memory import SessionSettings
    from tests.fake_model import FakeModel
    from tests.test_responses import get_text_message

    session = DaprSession(
        session_id="runner_override_test",
        state_store_name="statestore",
        dapr_client=fake_dapr_client,  # type: ignore[arg-type]
        session_settings=SessionSettings(limit=100),
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


def _create_owned_dapr_session(session_id: str) -> DaprSession:
    """Create a session that owns its client, mirroring the from_address path."""
    import unittest.mock

    with unittest.mock.patch(
        "agents.extensions.memory.dapr_session.DaprClient",
        return_value=FakeDaprClient(),
    ):
        return DaprSession.from_address(session_id, state_store_name="statestore")


async def test_dapr_session_closed_operations_raise_runtime_error():
    """Operations on a closed owned-client session must fail instead of using a closed client."""
    session = _create_owned_dapr_session("closed_state_test")
    assert session._owns_client is True
    await session.add_items([{"role": "user", "content": "before close"}])
    await session.close()

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.get_items()

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.add_items([{"role": "user", "content": "after close"}])

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.pop_item()

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.clear_session()


async def test_dapr_session_closed_rejects_empty_add_items():
    """add_items([]) must not bypass the closed check through the empty-list fast path."""
    session = _create_owned_dapr_session("closed_empty_add_test")
    await session.close()

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.add_items([])


async def test_dapr_session_closed_after_context_manager_exit():
    """Leaving the async context manager closes an owned session, so later use must fail."""
    session = _create_owned_dapr_session("closed_context_manager_test")

    async with session:
        await session.add_items([{"role": "user", "content": "inside context"}])

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.get_items()


async def test_dapr_session_close_is_idempotent():
    """Repeated and concurrent close() calls must remain safe no-ops."""
    import asyncio

    session = _create_owned_dapr_session("close_idempotent_test")

    await asyncio.gather(session.close(), session.close())
    await session.close()

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.get_items()


async def test_dapr_session_failed_cleanup_is_terminal_and_retried():
    """A failed cleanup keeps the session terminal, and the next close() retries it."""
    import unittest.mock

    session = _create_owned_dapr_session("failed_cleanup_test")
    attempts = 0
    real_close = session._dapr_client.close

    async def failing_then_succeeding_close(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        # Mirror a real client, which tears the channel down before surfacing the error.
        await real_close(*args, **kwargs)
        if attempts == 1:
            raise OSError("channel shutdown error surfaced after teardown")

    with unittest.mock.patch.object(session._dapr_client, "close", failing_then_succeeding_close):
        with pytest.raises(OSError, match="channel shutdown error surfaced after teardown"):
            await session.close()

        assert session._closed is True
        for operation in (
            session.get_items(),
            session.add_items([{"role": "user", "content": "after failed cleanup"}]),
            session.pop_item(),
            session.clear_session(),
            session.ping(),
        ):
            with pytest.raises(RuntimeError, match="DaprSession is closed"):
                await operation

        await session.close()
        assert attempts == 2
        assert session._client_released is True

        await session.close()
        assert attempts == 2


async def test_dapr_session_cancelled_cleanup_is_terminal_and_retried():
    """A cancelled cleanup keeps the session terminal, and the next close() retries it."""
    import asyncio
    import unittest.mock
    from contextlib import suppress

    timeout = 5.0
    session = _create_owned_dapr_session("cancelled_cleanup_test")
    attempts = 0
    entered_cleanup = asyncio.Event()
    real_close = session._dapr_client.close

    async def hanging_then_succeeding_close(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered_cleanup.set()
            await asyncio.Event().wait()  # Hang until cancelled.
        return await real_close(*args, **kwargs)

    close_task: asyncio.Task[None] | None = None
    try:
        with unittest.mock.patch.object(
            session._dapr_client, "close", hanging_then_succeeding_close
        ):
            close_task = asyncio.create_task(session.close())
            await asyncio.wait_for(entered_cleanup.wait(), timeout)
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            close_task = None

            assert session._closed is True
            with pytest.raises(RuntimeError, match="DaprSession is closed"):
                await session.get_items()

            await asyncio.wait_for(session.close(), timeout)
            assert attempts == 2
            assert session._client_released is True
    finally:
        if close_task is not None:
            close_task.cancel()
            with suppress(asyncio.CancelledError):
                await close_task


async def test_dapr_session_ping_raises_after_close():
    """ping() must not read or write through a client this session already closed."""
    session = _create_owned_dapr_session("ping_after_close_test")
    assert await session.ping() is True
    await session.close()

    with pytest.raises(RuntimeError, match="DaprSession is closed"):
        await session.ping()


async def test_dapr_session_close_is_noop_for_injected_client():
    """With an injected client, close() stays a no-op and the session remains usable."""
    client = FakeDaprClient()
    session = DaprSession(
        session_id="injected_client_noop_test",
        state_store_name="statestore",
        dapr_client=client,  # type: ignore[arg-type]
    )
    assert session._owns_client is False

    await session.add_items([{"role": "user", "content": "before close"}])
    await session.close()

    assert client._closed is False
    await session.add_items([{"role": "user", "content": "after close"}])
    items = await session.get_items()
    assert [item.get("content") for item in items] == ["before close", "after close"]

    assert await session.ping() is True

    other = DaprSession(
        session_id="injected_client_noop_test",
        state_store_name="statestore",
        dapr_client=client,  # type: ignore[arg-type]
    )
    assert len(await other.get_items()) == 2


async def test_dapr_session_operation_waiting_behind_close_raises():
    """An operation queued behind close() must fail rather than run after shutdown completes."""
    import asyncio
    import unittest.mock
    from contextlib import suppress

    from agents.memory.session_settings import resolve_session_limit

    timeout = 5.0
    blocked_probe = 0.1
    fake_client = FakeDaprClient()
    close_holds_lock = asyncio.Event()  # Set once close() is inside the session lock.
    release_close = asyncio.Event()  # Test-owned gate that lets the paused close() finish.
    op_entered = asyncio.Event()  # Set once the operation is past method entry.
    real_close = fake_client.close
    real_resolve = resolve_session_limit

    async def paused_close(*args: Any, **kwargs: Any) -> Any:
        close_holds_lock.set()
        await asyncio.wait_for(release_close.wait(), timeout)
        return await real_close(*args, **kwargs)

    def entry_signalling_resolve(*args: Any, **kwargs: Any) -> Any:
        # This runs immediately before the operation reaches the session lock.
        op_entered.set()
        return real_resolve(*args, **kwargs)

    with unittest.mock.patch(
        "agents.extensions.memory.dapr_session.DaprClient", return_value=fake_client
    ):
        session = DaprSession.from_address("close_interleave_test", state_store_name="statestore")
    assert session._owns_client is True

    close_task: asyncio.Task[None] | None = None
    op_task: asyncio.Task[list[TResponseInputItem]] | None = None
    try:
        with unittest.mock.patch.object(fake_client, "close", paused_close):
            close_task = asyncio.create_task(session.close())
            await asyncio.wait_for(close_holds_lock.wait(), timeout)

            # Start the operation while close() still holds the lock so it must queue behind it.
            with unittest.mock.patch(
                "agents.extensions.memory.dapr_session.resolve_session_limit",
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

            with pytest.raises(RuntimeError, match="DaprSession is closed"):
                await asyncio.wait_for(op_task, timeout)
            op_task = None
    finally:
        release_close.set()
        for task in (close_task, op_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, RuntimeError):
                    await task


async def test_add_items_preserves_created_at_metadata(
    fake_dapr_client: FakeDaprClient, monkeypatch: pytest.MonkeyPatch
):
    """`created_at` must be set once and not overwritten by subsequent add_items calls."""
    session = await _create_test_session(fake_dapr_client)

    try:
        monkeypatch.setattr("agents.extensions.memory.dapr_session.time.time", lambda: 1000.0)
        await session.add_items([{"role": "user", "content": "first"}])
        first = json.loads(fake_dapr_client._state[session._metadata_key].decode("utf-8"))
        assert first["created_at"] == "1000"
        assert first["updated_at"] == "1000"

        monkeypatch.setattr("agents.extensions.memory.dapr_session.time.time", lambda: 2000.0)
        await session.add_items([{"role": "user", "content": "second"}])
        second = json.loads(fake_dapr_client._state[session._metadata_key].decode("utf-8"))
        assert second["created_at"] == "1000"
        assert second["updated_at"] == "2000"
    finally:
        await session.close()


async def test_metadata_creation_does_not_request_first_write(
    fake_dapr_client: FakeDaprClient, monkeypatch: pytest.MonkeyPatch
):
    """Creating the metadata key must not ask for first-write concurrency.

    Dapr treats a write with no etag as last-write-wins even when first-write is requested,
    so asking for it on the create would imply a guarantee the store does not provide. The
    SDK reports a missing etag as an empty string, so this also pins that the empty value is
    not mistaken for a real one.
    """
    session = await _create_test_session(fake_dapr_client, "metadata_create_concurrency")

    try:
        real_save = fake_dapr_client.save_state
        seen: list[tuple[str | None, Any]] = []

        async def record_metadata_saves(
            store_name: str,
            key: str,
            value: str | bytes,
            **kwargs: Any,
        ) -> None:
            if key == session._metadata_key:
                seen.append(
                    (kwargs.get("etag"), getattr(kwargs.get("options"), "concurrency", None))
                )
            await real_save(store_name, key, value, **kwargs)

        monkeypatch.setattr(fake_dapr_client, "save_state", record_metadata_saves)

        await session.add_items([{"role": "user", "content": "first"}])
        assert len(seen) == 1
        create_etag, create_concurrency = seen[0]
        # No real etag existed, so no etag is sent and concurrency is left unspecified
        # rather than first-write, which Dapr would ignore here anyway.
        assert create_etag is None
        assert getattr(create_concurrency, "name", None) == "unspecified"

        await session.add_items([{"role": "user", "content": "second"}])
        assert len(seen) == 2
        update_etag, update_concurrency = seen[1]
        # Metadata now exists, so the update is guarded by the etag that backed it.
        assert update_etag is not None
        assert getattr(update_concurrency, "name", None) == "first_write"
    finally:
        await session.close()


async def test_stale_metadata_etag_retries_and_keeps_created_at(
    fake_dapr_client: FakeDaprClient, monkeypatch: pytest.MonkeyPatch
):
    """A metadata save guarded by a stale etag must retry and keep the stored `created_at`.

    Once the metadata key exists, every later append reads a real etag and saves against it.
    A writer holding an etag that another append has already superseded is rejected, so it
    re-reads and adopts the stored `created_at` rather than replacing it with its own `now`.

    Dapr treats a write with no etag as last-write-wins even when first-write concurrency is
    requested, so the create is deliberately not covered here. This is the guarantee the
    store actually provides.
    """
    session_id = "shared_session_created_at_stale_etag"
    first = await _create_test_session(fake_dapr_client, session_id=session_id)
    second = await _create_test_session(fake_dapr_client, session_id=session_id)

    try:
        monkeypatch.setattr("agents.extensions.memory.dapr_session.time.time", lambda: 1000.0)
        await first.add_items([{"role": "user", "content": "first"}])
        created = json.loads(fake_dapr_client._state[first._metadata_key].decode("utf-8"))
        assert created["created_at"] == "1000"
        stale_etag = fake_dapr_client._etags[first._metadata_key]

        # A second append supersedes that etag, so the value captured above is now stale but
        # still non-null, which is the situation the guard is actually for.
        monkeypatch.setattr("agents.extensions.memory.dapr_session.time.time", lambda: 1500.0)
        await first.add_items([{"role": "user", "content": "second"}])
        assert fake_dapr_client._etags[first._metadata_key] != stale_etag

        real_read = second._read_created_at
        calls = 0

        async def read_stale_once() -> tuple[str | None, str | None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Pretend this writer read the metadata before the second append landed.
                return "1000", stale_etag
            return await real_read()

        monkeypatch.setattr(second, "_read_created_at", read_stale_once)
        monkeypatch.setattr("agents.extensions.memory.dapr_session.time.time", lambda: 2000.0)
        await second.add_items([{"role": "user", "content": "third"}])

        final = json.loads(fake_dapr_client._state[second._metadata_key].decode("utf-8"))
        # The stale save is rejected, so the retry reads the stored value and keeps it.
        assert final["created_at"] == "1000"
        assert final["updated_at"] == "2000"
        # Two reads means the conditional save actually rejected the stale one and retried.
        assert calls == 2
    finally:
        await first.close()
        await second.close()


async def test_add_items_survives_metadata_write_giving_up(
    fake_dapr_client: FakeDaprClient, monkeypatch: pytest.MonkeyPatch, caplog: Any
):
    """A metadata write that exhausts its retries must not fail an append that already landed.

    The messages key is saved before the metadata key, so raising here would report failure for
    items that are already in the session, and the natural retry of `add_items` would store the
    same batch a second time.
    """
    import logging

    session = await _create_test_session(fake_dapr_client, "metadata_gives_up")

    try:
        real_save = fake_dapr_client.save_state

        async def fail_only_metadata_saves(
            store_name: str,
            key: str,
            value: str | bytes,
            **kwargs: Any,
        ) -> None:
            if key == session._metadata_key:
                raise RuntimeError("etag mismatch")
            await real_save(store_name, key, value, **kwargs)

        monkeypatch.setattr(fake_dapr_client, "save_state", fail_only_metadata_saves)
        monkeypatch.setattr(session, "_calculate_retry_delay", lambda attempt: 0.0)

        with caplog.at_level(logging.WARNING):
            await session.add_items([{"role": "user", "content": "kept"}])

        items = await session.get_items()
        assert [item.get("content") for item in items] == ["kept"]

        warnings = [
            record for record in caplog.records if "could not update" in record.getMessage()
        ]
        assert warnings
        # Data logging is off by default, so neither the caller-supplied session id nor the
        # provider error text may reach the record.
        for record in warnings:
            assert "metadata_gives_up" not in record.getMessage()
            assert "etag mismatch" not in record.getMessage()
    finally:
        await session.close()


@pytest.mark.parametrize(
    ("dont_log_model_data", "dont_log_tool_data", "redacted"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
    ids=["model-redacted", "tool-redacted", "fully-diagnostic"],
)
async def test_metadata_write_warning_respects_data_policies(
    fake_dapr_client: FakeDaprClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: Any,
    dont_log_model_data: bool,
    dont_log_tool_data: bool,
    redacted: bool,
):
    """The give-up warning must obey both data policies, and never lose the committed items.

    `log_model_and_tool_action_warning` redacts when either policy is on, so only the mode with
    both off may carry the session id or the provider error. This inspects the whole LogRecord
    rather than just the rendered message, because the session id travels in `extra` and the
    exception travels in `exc_info`, neither of which shows up in `getMessage()`.
    """
    import logging

    import agents._debug as _debug

    session_id = f"metadata_policy_{dont_log_model_data}_{dont_log_tool_data}"
    session = await _create_test_session(fake_dapr_client, session_id)

    try:
        monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", dont_log_model_data)
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", dont_log_tool_data)

        real_save = fake_dapr_client.save_state
        error_text = "etag mismatch"

        async def fail_only_metadata_saves(
            store_name: str,
            key: str,
            value: str | bytes,
            **kwargs: Any,
        ) -> None:
            if key == session._metadata_key:
                raise RuntimeError(error_text)
            await real_save(store_name, key, value, **kwargs)

        monkeypatch.setattr(fake_dapr_client, "save_state", fail_only_metadata_saves)
        monkeypatch.setattr(session, "_calculate_retry_delay", lambda attempt: 0.0)

        with caplog.at_level(logging.WARNING):
            await session.add_items([{"role": "user", "content": "kept"}])

        # The append landed regardless of how the failure was logged.
        items = await session.get_items()
        assert [item.get("content") for item in items] == ["kept"]

        records = [record for record in caplog.records if "could not update" in record.getMessage()]
        assert len(records) == 1
        record = records[0]
        rendered = logging.Formatter().format(record)

        if redacted:
            # Redacted form is the bare message with no exception and no diagnostic context.
            assert record.msg == "%s"
            assert record.exc_info is None
            assert record.exc_text is None
            assert not hasattr(record, "openai_agents_diagnostic_context")
            assert session_id not in rendered
            assert error_text not in rendered
            assert all(
                session_id not in str(value) and error_text not in str(value)
                for value in record.__dict__.values()
            )
        else:
            # Diagnostic form carries the exception and the session id, by design.
            assert record.msg == "%s: %s"
            assert record.exc_info is not None
            assert isinstance(record.exc_info[1], RuntimeError)
            assert error_text in rendered
            assert record.openai_agents_diagnostic_context == {"session_id": session_id}
    finally:
        await session.close()

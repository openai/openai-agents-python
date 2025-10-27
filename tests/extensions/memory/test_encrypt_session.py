from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")  # Skip tests if cryptography is not installed

from cryptography.fernet import Fernet

from agents import Agent, Runner, SQLiteSession, TResponseInputItem
from agents.extensions.memory.encrypt_session import EncryptedSession
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


@pytest.fixture
def encryption_key() -> str:
    """Fixture for a valid Fernet encryption key."""
    return str(Fernet.generate_key().decode("utf-8"))


@pytest.fixture
def underlying_session():
    """Fixture for an underlying SQLite session."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_encrypt.db"
    return SQLiteSession("test_session", db_path)


async def test_encrypted_session_basic_functionality(
    agent: Agent, encryption_key: str, underlying_session: SQLiteSession
):
    """Test basic encryption/decryption functionality."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=600,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert len(retrieved) == 2
    assert retrieved[0].get("content") == "Hello"
    assert retrieved[1].get("content") == "Hi there!"

    encrypted_items = await underlying_session.get_items()
    assert encrypted_items[0].get("__enc__") == 1
    assert "payload" in encrypted_items[0]
    assert encrypted_items[0].get("content") != "Hello"

    underlying_session.close()


async def test_encrypted_session_with_runner(
    agent: Agent, encryption_key: str, underlying_session: SQLiteSession
):
    """Test that EncryptedSession works with Runner."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    assert isinstance(agent.model, FakeModel)
    agent.model.set_next_output([get_text_message("San Francisco")])
    result1 = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    assert result1.final_output == "San Francisco"

    agent.model.set_next_output([get_text_message("California")])
    result2 = await Runner.run(agent, "What state is it in?", session=session)
    assert result2.final_output == "California"

    last_input = agent.model.last_turn_args["input"]
    assert len(last_input) > 1
    assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)

    underlying_session.close()


async def test_encrypted_session_pop_item(encryption_key: str, underlying_session: SQLiteSession):
    """Test pop_item functionality."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Second"},
    ]
    await session.add_items(items)

    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "Second"

    remaining = await session.get_items()
    assert len(remaining) == 1
    assert remaining[0].get("content") == "First"

    underlying_session.close()


async def test_encrypted_session_clear(encryption_key: str, underlying_session: SQLiteSession):
    """Test clear_session functionality."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    await session.add_items([{"role": "user", "content": "Test"}])
    await session.clear_session()

    items = await session.get_items()
    assert len(items) == 0

    underlying_session.close()


async def test_encrypted_session_ttl_expiration(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test TTL expiration - expired items are silently skipped."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=1,  # 1 second TTL
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]
    await session.add_items(items)

    time.sleep(2)

    retrieved = await session.get_items()
    assert len(retrieved) == 0

    underlying_items = await underlying_session.get_items()
    assert len(underlying_items) == 2

    underlying_session.close()


async def test_encrypted_session_pop_expired(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test pop_item with expired data."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=1,
    )

    await session.add_items([{"role": "user", "content": "Test"}])
    time.sleep(2)

    popped = await session.pop_item()
    assert popped is None

    underlying_session.close()


async def test_encrypted_session_pop_mixed_expired_valid(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test pop_item auto-retry with mixed expired and valid items."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=2,  # 2 second TTL
    )

    await session.add_items(
        [
            {"role": "user", "content": "Old message 1"},
            {"role": "assistant", "content": "Old response 1"},
        ]
    )

    time.sleep(3)

    await session.add_items(
        [
            {"role": "user", "content": "New message"},
            {"role": "assistant", "content": "New response"},
        ]
    )

    popped = await session.pop_item()
    assert popped is not None
    assert popped.get("content") == "New response"

    popped2 = await session.pop_item()
    assert popped2 is not None
    assert popped2.get("content") == "New message"

    popped3 = await session.pop_item()
    assert popped3 is None

    underlying_session.close()


async def test_encrypted_session_raw_string_key(underlying_session: SQLiteSession):
    """Test using raw string as encryption key (not base64)."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key="my-secret-password",  # Raw string, not Fernet key
    )

    await session.add_items([{"role": "user", "content": "Test"}])
    items = await session.get_items()
    assert len(items) == 1
    assert items[0].get("content") == "Test"

    underlying_session.close()


async def test_encrypted_session_get_items_limit(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test get_items with limit parameter."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": f"Message {i}"} for i in range(5)
    ]
    await session.add_items(items)

    limited = await session.get_items(limit=2)
    assert len(limited) == 2
    assert limited[0].get("content") == "Message 3"  # Latest 2
    assert limited[1].get("content") == "Message 4"

    underlying_session.close()


async def test_encrypted_session_unicode_content(
    encryption_key: str, underlying_session: SQLiteSession
):
    """Test encryption of international text content."""
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello world"},
        {"role": "assistant", "content": "Special chars: áéíóú"},
        {"role": "user", "content": "Numbers and symbols: 123!@#"},
    ]
    await session.add_items(items)

    retrieved = await session.get_items()
    assert retrieved[0].get("content") == "Hello world"
    assert retrieved[1].get("content") == "Special chars: áéíóú"
    assert retrieved[2].get("content") == "Numbers and symbols: 123!@#"

    underlying_session.close()


class CustomSession(SQLiteSession):
    """Mock custom session with additional methods for testing delegation."""

    def get_stats(self) -> dict[str, int]:
        """Custom method that should be accessible through delegation."""
        return {"custom_method_calls": 42, "test_value": 123}

    async def custom_async_method(self) -> str:
        """Custom async method for testing delegation."""
        return "custom_async_result"


async def test_encrypted_session_delegation():
    """Test that custom methods on underlying session are accessible through delegation."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_delegation.db"
    underlying_session = CustomSession("test_session", db_path)

    encryption_key = str(Fernet.generate_key().decode("utf-8"))
    session = EncryptedSession(
        session_id="test_session",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
    )

    stats = session.get_stats()
    assert stats == {"custom_method_calls": 42, "test_value": 123}

    result = await session.custom_async_method()
    assert result == "custom_async_result"

    await session.add_items([{"role": "user", "content": "Test delegation"}])
    items = await session.get_items()
    assert len(items) == 1
    assert items[0].get("content") == "Test delegation"

    underlying_session.close()

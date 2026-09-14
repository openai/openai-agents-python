from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from agents import SQLiteSession
from agents.extensions.memory.encrypt_session import EncryptedSession

pytestmark = pytest.mark.asyncio


async def test_wrong_key_pop_preserves_recoverable_ciphertext(tmp_path) -> None:
    store = SQLiteSession("conversation", tmp_path / "history.db")
    try:
        correct = EncryptedSession(
            session_id="conversation", underlying_session=store,
            encryption_key="example-correct-key", ttl=3600,
        )
        wrong = EncryptedSession(
            session_id="conversation", underlying_session=store,
            encryption_key="example-wrong-key", ttl=3600,
        )
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "follow-up"},
        ]
        await correct.add_items(messages)
        stored_before = await store.get_items()

        assert await wrong.pop_item() is None
        assert await store.get_items() == stored_before
        assert await correct.get_items() == messages
    finally:
        store.close()

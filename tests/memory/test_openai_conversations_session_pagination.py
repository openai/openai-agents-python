from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from agents.memory.openai_conversations_session import OpenAIConversationsSession


class _ConversationItem:
    def __init__(self, index: int) -> None:
        self.index = index

    def model_dump(self, *, exclude_unset: bool) -> dict[str, Any]:
        assert exclude_unset is True
        return {"id": f"item-{self.index}", "role": "user", "content": str(self.index)}


@pytest.mark.asyncio
async def test_get_items_keeps_large_limit_out_of_provider_page_size() -> None:
    client = MagicMock()
    captured: dict[str, Any] = {}

    def list_items(**kwargs: Any):
        captured.update(kwargs)

        async def iterate():
            for index in range(199, -1, -1):
                yield _ConversationItem(index)

        return iterate()

    client.conversations.items.list = MagicMock(side_effect=list_items)
    session = OpenAIConversationsSession(conversation_id="conv-1", openai_client=client)

    items = await session.get_items(limit=150)

    assert captured == {"conversation_id": "conv-1", "order": "desc"}
    assert [cast(dict[str, Any], item)["id"] for item in items] == [
        f"item-{index}" for index in range(50, 200)
    ]

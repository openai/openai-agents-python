from __future__ import annotations

from openai import AsyncOpenAI

from agents.models._openai_shared import get_default_openai_client

from ..items import TResponseInputItem
from .session import SessionABC


async def start_openai_conversations_session(openai_client: AsyncOpenAI | None = None) -> str:
    _maybe_openai_client = openai_client
    if openai_client is None:
        _maybe_openai_client = get_default_openai_client() or AsyncOpenAI()
    # this never be None here
    _openai_client: AsyncOpenAI = _maybe_openai_client  # type: ignore [assignment]

    response = await _openai_client.conversations.create(items=[])
    return response.id


_EMPTY_SESSION_ID = ""


class OpenAIConversationsSession(SessionABC):
    def __init__(
        self,
        *,
        session_id: str | None = None,
        openai_client: AsyncOpenAI | None = None,
    ):
        # this implementation allows to set this value later
        self.session_id = session_id or _EMPTY_SESSION_ID
        _openai_client = openai_client
        if _openai_client is None:
            _openai_client = get_default_openai_client() or AsyncOpenAI()
        # this never be None here
        self.openai_client: AsyncOpenAI = _openai_client

    async def _ensure_session_id(self) -> None:
        if self.session_id == _EMPTY_SESSION_ID:
            self.session_id = await start_openai_conversations_session(self.openai_client)

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        await self._ensure_session_id()

        all_items = []
        if limit is None:
            async for item in self.openai_client.conversations.items.list(
                conversation_id=self.session_id,
                order="asc",
            ):
                # calling model_dump() to make this serializable
                all_items.append(item.model_dump())
        else:
            async for item in self.openai_client.conversations.items.list(
                conversation_id=self.session_id,
                limit=limit,
                order="desc",
            ):
                # calling model_dump() to make this serializable
                all_items.append(item.model_dump())
                if limit is not None and len(all_items) >= limit:
                    break
            all_items.reverse()

        return all_items  # type: ignore

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        await self._ensure_session_id()
        await self.openai_client.conversations.items.create(
            conversation_id=self.session_id,
            items=items,
        )

    async def pop_item(self) -> TResponseInputItem | None:
        await self._ensure_session_id()
        items = await self.get_items(limit=1)
        if not items:
            return None
        item_id: str = str(items[0]["id"])  # type: ignore [typeddict-item]
        await self.openai_client.conversations.items.delete(
            conversation_id=self.session_id, item_id=item_id
        )
        return items[0]

    async def clear_session(self) -> None:
        await self._ensure_session_id()
        await self.openai_client.conversations.delete(
            conversation_id=self.session_id,
        )
        self.session_id = _EMPTY_SESSION_ID

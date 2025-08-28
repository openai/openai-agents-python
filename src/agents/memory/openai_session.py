from typing import Optional

from openai import AsyncOpenAI

from agents.models._openai_shared import get_default_openai_client

from ..items import TResponseInputItem
from .session import SessionABC


async def start_openai_session(openai_client: Optional[AsyncOpenAI] = None) -> str:
    _openai_client = openai_client
    if openai_client is None:
        _openai_client = get_default_openai_client() or AsyncOpenAI()

    response = await _openai_client.conversations.create(items=[])  # type: ignore [union-attr]
    return response.id


class OpenAISession(SessionABC):
    def __init__(
        self,
        session_id: Optional[str] = None,
        openai_client: Optional[AsyncOpenAI] = None,
    ):
        # this implementation allows to set this value later
        self.session_id = session_id  # type: ignore
        self.openai_client = openai_client
        if self.openai_client is None:
            self.openai_client = get_default_openai_client() or AsyncOpenAI()

    async def _ensure_session_id(self) -> None:
        if self.session_id is None:
            self.session_id = await start_openai_session(self.openai_client)

    async def get_items(self, limit: Optional[int] = None) -> list[TResponseInputItem]:
        await self._ensure_session_id()

        all_items = []
        if limit is None:
            async for item in self.openai_client.conversations.items.list(  # type: ignore [union-attr]
                conversation_id=self.session_id,
                order="asc",
            ):
                # calling model_dump() to make this serializable
                all_items.append(item.model_dump())
        else:
            async for item in self.openai_client.conversations.items.list(  # type: ignore [union-attr]
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
        await self.openai_client.conversations.items.create(  # type: ignore [union-attr]
            conversation_id=self.session_id,
            items=items,
        )

    async def pop_item(self) -> TResponseInputItem | None:
        await self._ensure_session_id()
        items = await self.get_items(limit=1)
        if not items:
            return None
        await self.openai_client.conversations.items.delete(  # type: ignore [union-attr]
            conversation_id=self.session_id,
            item_id=str(items[0]["id"]),  # type: ignore
        )
        return items[0]

    async def clear_session(self) -> None:
        await self._ensure_session_id()
        await self.openai_client.conversations.delete(  # type: ignore [union-attr]
            conversation_id=self.session_id,
        )
        self.session_id = None  # type: ignore

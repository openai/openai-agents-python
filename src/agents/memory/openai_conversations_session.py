from __future__ import annotations

from openai import AsyncOpenAI

from agents.models._openai_shared import get_default_openai_client

from ..items import TResponseInputItem
from .session import SessionABC
from .session_settings import SessionSettings, resolve_session_limit


async def start_openai_conversations_session(openai_client: AsyncOpenAI | None = None) -> str:
    _maybe_openai_client = openai_client
    if openai_client is None:
        _maybe_openai_client = get_default_openai_client() or AsyncOpenAI()
    # this never be None here
    _openai_client: AsyncOpenAI = _maybe_openai_client  # type: ignore [assignment]

    response = await _openai_client.conversations.create(items=[])
    return response.id


class OpenAIConversationsSession(SessionABC):
    def __init__(
        self,
        *,
        conversation_id: str | None = None,
        openai_client: AsyncOpenAI | None = None,
        session_settings: SessionSettings | None = None,
    ):
        self._session_id: str | None = conversation_id
        self.session_settings = session_settings or SessionSettings()
        _openai_client = openai_client
        if _openai_client is None:
            _openai_client = get_default_openai_client() or AsyncOpenAI()
        # this never be None here
        self._openai_client: AsyncOpenAI = _openai_client

    @property
    def session_id(self) -> str:
        """Get the session ID (conversation ID).

        Returns:
            The conversation ID for this session.

        Raises:
            ValueError: If the session has not been initialized yet.
                Call any session method (get_items, add_items, etc.) first
                to trigger lazy initialization.
        """
        if self._session_id is None:
            raise ValueError(
                "Session ID not yet available. The session is lazily initialized "
                "on first API call. Call get_items(), add_items(), or similar first."
            )
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        """Set the session ID (conversation ID)."""
        self._session_id = value

    async def _get_session_id(self) -> str:
        if self._session_id is None:
            self._session_id = await start_openai_conversations_session(self._openai_client)
        return self._session_id

    async def _clear_session_id(self) -> None:
        self._session_id = None

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        session_id = await self._get_session_id()

        session_limit = resolve_session_limit(limit, self.session_settings)

        all_items = []
        if session_limit is None:
            async for item in self._openai_client.conversations.items.list(
                conversation_id=session_id,
                order="asc",
            ):
                # calling model_dump() to make this serializable
                all_items.append(item.model_dump(exclude_unset=True))
        else:
            async for item in self._openai_client.conversations.items.list(
                conversation_id=session_id,
                limit=session_limit,
                order="desc",
            ):
                # calling model_dump() to make this serializable
                all_items.append(item.model_dump(exclude_unset=True))
                if session_limit is not None and len(all_items) >= session_limit:
                    break
            all_items.reverse()

        return all_items  # type: ignore

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        session_id = await self._get_session_id()
        if not items:
            return

        await self._openai_client.conversations.items.create(
            conversation_id=session_id,
            items=items,
        )

    async def pop_item(self) -> TResponseInputItem | None:
        session_id = await self._get_session_id()
        items = await self.get_items(limit=1)
        if not items:
            return None
        item_id: str = str(items[0]["id"])  # type: ignore [typeddict-item]
        await self._openai_client.conversations.items.delete(
            conversation_id=session_id, item_id=item_id
        )
        return items[0]

    async def clear_session(self) -> None:
        session_id = await self._get_session_id()
        await self._openai_client.conversations.delete(
            conversation_id=session_id,
        )
        await self._clear_session_id()

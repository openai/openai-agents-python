from __future__ import annotations

import asyncio
from typing import Any

from openai import AsyncOpenAI

from agents.models._openai_shared import get_default_openai_client

from ..items import TResponseInputItem
from .session import SessionABC, _await_mutation
from .session_settings import SessionSettings, coerce_session_settings, resolve_session_limit

_MAX_ITEMS_PER_REQUEST = 20


async def start_openai_conversations_session(openai_client: AsyncOpenAI | None = None) -> str:
    _maybe_openai_client = openai_client
    if openai_client is None:
        default_client = get_default_openai_client()
        _maybe_openai_client = default_client if default_client is not None else AsyncOpenAI()
    # this never be None here
    _openai_client: AsyncOpenAI = _maybe_openai_client  # type: ignore [assignment]

    response = await _openai_client.conversations.create(items=[])
    return response.id


class OpenAIConversationsSession(SessionABC):
    session_settings: SessionSettings | None = None

    def __init__(
        self,
        *,
        conversation_id: str | None = None,
        openai_client: AsyncOpenAI | None = None,
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ):
        self._session_id: str | None = conversation_id
        self._session_id_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self.session_settings = (
            coerce_session_settings(session_settings)
            if session_settings is not None
            else SessionSettings()
        )
        _openai_client = openai_client
        if _openai_client is None:
            default_client = get_default_openai_client()
            _openai_client = default_client if default_client is not None else AsyncOpenAI()
        # this never be None here
        self._openai_client: AsyncOpenAI = _openai_client

    @property
    def session_id(self) -> str:
        """Get the session ID (conversation ID).

        Returns:
            The conversation ID for this session.

        Raises:
            ValueError: If the session has not been initialized yet.
                Call a session method that accesses the remote conversation, such as
                get_items() or add_items() with a non-empty list, to initialize it.
        """
        if self._session_id is None:
            raise ValueError(
                "Session ID not yet available. The session is lazily initialized "
                "on first API call. Call get_items(), add_items() with a non-empty list, "
                "or a similar method first."
            )
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        """Set the session ID (conversation ID)."""
        self._session_id = value

    async def _get_session_id(self) -> str:
        async with self._session_id_lock:
            if self._session_id is None:
                self._session_id = await start_openai_conversations_session(self._openai_client)
            return self._session_id

    async def _clear_session_id(self) -> None:
        self._session_id = None

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        async with self._mutation_lock:
            return await self._get_items(limit)

    async def _get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Read history while the caller holds the instance's mutation lock."""
        session_id = await self._get_session_id()

        session_limit = resolve_session_limit(limit, self.session_settings)
        if session_limit == 0:
            return []

        all_items = []
        if session_limit is None:
            async for item in self._openai_client.conversations.items.list(
                conversation_id=session_id,
                order="asc",
            ):
                # calling model_dump() to make this serializable
                all_items.append(item.model_dump(exclude_unset=True))
        else:
            # Omit provider page size. Apply the session cutoff locally after pagination.
            async for item in self._openai_client.conversations.items.list(
                conversation_id=session_id,
                order="desc",
            ):
                # calling model_dump() to make this serializable
                all_items.append(item.model_dump(exclude_unset=True))
                if session_limit is not None and len(all_items) >= session_limit:
                    break
            all_items.reverse()

        return all_items  # type: ignore

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Append items in order, in requests of at most 20 items each.

        Reads and mutations through this session instance are serialized. Separate instances
        or external writers require application-level coordination.

        Writes spanning multiple requests are not atomic. If a request fails or
        is cancelled, earlier batches remain saved and later batches are not
        sent. The original exception propagates. Before retrying, callers must
        reconcile the remote history; retrying the entire list can duplicate
        items that were already saved, including an unacknowledged request.
        """
        if not items:
            return

        async with self._mutation_lock:
            session_id = await self._get_session_id()
            # The Conversations items-create endpoint accepts up to 20 items per request.
            for offset in range(0, len(items), _MAX_ITEMS_PER_REQUEST):
                await self._openai_client.conversations.items.create(
                    conversation_id=session_id,
                    items=items[offset : offset + _MAX_ITEMS_PER_REQUEST],
                )

    async def pop_item(self) -> TResponseInputItem | None:
        async with self._mutation_lock:
            session_id = await self._get_session_id()
            items = await self._get_items(limit=1)
            if not items:
                return None
            item_id: str = str(items[0]["id"])  # type: ignore [typeddict-item]
            await self._openai_client.conversations.items.delete(
                conversation_id=session_id, item_id=item_id
            )
            return items[0]

    async def clear_session(self) -> None:
        async with self._mutation_lock, self._session_id_lock:
            if self._session_id is None:
                return

            session_id = self._session_id

            async def delete_and_clear_session_id() -> None:
                await self._openai_client.conversations.delete(
                    conversation_id=session_id,
                )
                if self._session_id == session_id:
                    self._session_id = None

            await _await_mutation(delete_and_clear_session_id())

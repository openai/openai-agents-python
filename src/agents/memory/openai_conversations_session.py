from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any

from openai import AsyncOpenAI

from agents.models._openai_shared import get_default_openai_client

from ..items import TResponseInputItem
from .session import SessionABC
from .session_settings import SessionSettings, coerce_session_settings, resolve_session_limit

# Conversations items.create accepts at most 20 items per request.
# See https://developers.openai.com/api/reference/resources/conversations/subresources/items/.
_MAX_ITEMS_PER_CONVERSATION_CREATE = 20


def _created_conversation_item_ids(result: object) -> list[str]:
    data = getattr(result, "data", None)
    if data is None:
        return []
    ids: list[str] = []
    for item in data:
        item_id = getattr(item, "id", None)
        if isinstance(item_id, str) and item_id:
            ids.append(item_id)
    return ids


async def _await_despite_cancellation(awaitable: Awaitable[None]) -> None:
    """Finish rollback even if the caller keeps cancelling the current task."""
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        _ = task.exception() if not task.cancelled() else None
        raise


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
        self._session_lock = asyncio.Lock()
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
        async with self._session_lock:
            return await self._get_items_unlocked(limit)

    async def _get_items_unlocked(self, limit: int | None = None) -> list[TResponseInputItem]:
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
        if not items:
            return

        async with self._session_lock:
            session_id = await self._get_session_id()
            created_ids: list[str] = []
            try:
                for offset in range(0, len(items), _MAX_ITEMS_PER_CONVERSATION_CREATE):
                    created = await self._openai_client.conversations.items.create(
                        conversation_id=session_id,
                        items=items[offset : offset + _MAX_ITEMS_PER_CONVERSATION_CREATE],
                    )
                    created_ids.extend(_created_conversation_item_ids(created))
            except (Exception, asyncio.CancelledError):
                await _await_despite_cancellation(
                    self._delete_created_items(session_id, created_ids)
                )
                raise

    async def _delete_created_items(self, session_id: str, created_ids: list[str]) -> None:
        for item_id in reversed(created_ids):
            with contextlib.suppress(Exception):
                await self._openai_client.conversations.items.delete(
                    conversation_id=session_id, item_id=item_id
                )

    async def pop_item(self) -> TResponseInputItem | None:
        async with self._session_lock:
            session_id = await self._get_session_id()
            items = await self._get_items_unlocked(limit=1)
            if not items:
                return None
            item_id: str = str(items[0]["id"])  # type: ignore [typeddict-item]
            await self._openai_client.conversations.items.delete(
                conversation_id=session_id, item_id=item_id
            )
            return items[0]

    async def clear_session(self) -> None:
        async with self._session_lock:
            async with self._session_id_lock:
                if self._session_id is None:
                    return

                await self._openai_client.conversations.delete(
                    conversation_id=self._session_id,
                )
                self._session_id = None

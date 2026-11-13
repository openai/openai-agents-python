"""Provider-item history for native Session runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ._lifecycle import _close_after_error, _join
from .input import _native_input, _response_content

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from .agent import Agent
    from .input import UserInput
    from .session import Completed, Session

OutputT = TypeVar("OutputT")
_MAX_RUN_ITEMS = 4096
_MAX_RUN_BYTES = 8 * 1024 * 1024


class _Operation:
    def __init__(self, owner: object) -> None:
        self.owner: object | None = owner


_operations: ContextVar[tuple[_Operation, ...]] = ContextVar(
    "conversations_history_operations", default=()
)


class ConversationsWriteError(RuntimeError):
    """An uncertain append, with public items retained for reconciliation."""

    def __init__(
        self, conversation_id: str, items: tuple[dict[str, Any], ...], acknowledged: int
    ) -> None:
        super().__init__(
            "Conversation append failed; reconcile remote history before retrying "
            f"({acknowledged} items acknowledged)"
        )
        self.conversation_id = conversation_id
        self.items = items
        self.acknowledged = acknowledged
        self.result: Completed[Any] | None = None


class ConversationsHistory:
    """OpenAI Conversations items and an owned native run.

    The supplied client remains application-owned. Reads and writes through one
    instance are serialized; separate instances and external writers require
    application coordination. Multi-request writes are not atomic. Failed or
    cancelled writes may have saved the current request; reconcile before retry.
    Native checkpoints, permissions and callback bindings stay outside messages.
    """

    def __init__(self, client: AsyncOpenAI, *, conversation_id: str | None = None) -> None:
        self._client = client
        self._conversation_id = conversation_id
        self._lock = asyncio.Lock()
        self._active_session: Session[Any, Any] | None = None

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    def _check_callback(self) -> None:
        if self._active_session is not None:
            self._active_session._check_callback_operation("access its own Conversations history")
        if any(scope.owner is self for scope in _operations.get()):
            raise RuntimeError("Cannot access Conversations history from its owning operation")

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        self._check_callback()
        async with self._lock:
            scope = _Operation(self)
            active = tuple(scope for scope in _operations.get() if scope.owner is not None)
            token = _operations.set((*active, scope))
            try:
                yield
            finally:
                scope.owner = None
                _operations.reset(token)

    async def _id(self) -> str:
        if self._conversation_id is None:

            async def create() -> None:
                response = await self._client.with_options(max_retries=0).conversations.create(
                    items=[]
                )
                self._conversation_id = response.id

            await _join(asyncio.create_task(create()))
        assert self._conversation_id is not None
        return self._conversation_id

    async def get_conversation_id(self) -> str:
        async with self._operation():
            return await self._id()

    async def _get_items(self, limit: int | None, *, for_run: bool = False) -> list[dict[str, Any]]:
        conversation_id = await self._id()
        if limit == 0:
            return []
        items: list[dict[str, Any]] = []
        size = 2  # The JSON list's brackets, including an empty history.
        async for item in self._client.conversations.items.list(
            conversation_id=conversation_id,
            order="asc" if limit is None else "desc",
            include=[
                "reasoning.encrypted_content",
                "message.input_image.image_url",
                "computer_call_output.output.image_url",
            ],
        ):
            if for_run and len(items) >= _MAX_RUN_ITEMS:
                raise ValueError("Conversations run history exceeds 4096 items; compact it first")
            value = item.model_dump(exclude_unset=True)
            if for_run:
                size += len(
                    json.dumps(
                        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode("utf-8")
                ) + bool(items)
                if size > _MAX_RUN_BYTES:
                    raise ValueError("Conversations run history exceeds 8 MiB; compact it first")
            items.append(value)
            if limit is not None and len(items) == limit:
                break
        if limit is not None:
            items.reverse()
        return items

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None and (isinstance(limit, bool) or limit < 0):
            raise ValueError("limit must be a nonnegative integer or None")
        async with self._operation():
            return await self._get_items(limit)

    @staticmethod
    def _capture(items: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(json.dumps(dict(item), allow_nan=False)) for item in items)

    async def _add_items(self, items: tuple[dict[str, Any], ...]) -> None:
        if not items:
            return
        conversation_id = await self._id()
        client = self._client.with_options(max_retries=0)
        acknowledged = 0
        try:
            for offset in range(0, len(items), 20):
                if offset:
                    await asyncio.sleep(0)

                async def append(offset: int = offset) -> None:
                    nonlocal acknowledged
                    await client.conversations.items.create(
                        conversation_id=conversation_id,
                        items=list(items[offset : offset + 20]),  # type: ignore[arg-type]
                    )
                    acknowledged = offset + len(items[offset : offset + 20])

                await _join(asyncio.create_task(append()))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ConversationsWriteError(conversation_id, items, acknowledged) from error

    async def add_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        captured = self._capture(items)
        async with self._operation():
            await self._add_items(captured)

    async def pop_item(self) -> dict[str, Any] | None:
        async with self._operation():
            items = await self._get_items(1)
            if not items:
                return None
            conversation_id = await self._id()
            await _join(
                asyncio.create_task(
                    self._client.with_options(max_retries=0).conversations.items.delete(
                        item_id=items[0]["id"], conversation_id=conversation_id
                    )
                )
            )
            return items[0]

    async def clear(self) -> None:
        async with self._operation():
            if self._conversation_id is None:
                return
            conversation_id = self._conversation_id

            async def clear() -> None:
                await self._client.with_options(max_retries=0).conversations.delete(conversation_id)
                self._conversation_id = None

            await _join(asyncio.create_task(clear()))

    async def _append_native_suffix(self, session: Session[Any, Any], start: int) -> None:
        final = await session.history_snapshot()
        await self._add_items(
            tuple(
                item
                for item in final.items[start:]
                if item.get("type") != "message" or item.get("role") not in ("developer", "system")
            )
        )

    async def run(
        self, agent: Agent[Any, OutputT], input: UserInput, **session_options: Any
    ) -> Completed[OutputT]:
        """One native Session, with remote history import and accepted public output append."""
        if any(
            session_options.get(key) is not None
            for key in ("state", "resume", "storage", "initial_messages", "provider_history")
        ):
            raise ValueError("Conversations history requires a fresh native Session history owner")
        parts = _native_input(input)
        if not isinstance(input, str):
            input = cast("UserInput", {"role": "user", "content": _response_content(parts)})
        async with self._operation():
            try:
                async with agent.session(**session_options) as session:
                    self._active_session = session
                    session._fail_unhandled_questions = session._interaction_handler is None
                    items = await self._get_items(None, for_run=True)
                    before = await session.history_snapshot()
                    if items:
                        await session.transact_history(
                            expected_revision=before.revision, keep=0, items=items
                        )
                    loaded = await session.history_snapshot()
                    try:
                        turn = await session.submit(input)
                        result = await turn.get_result()
                    except asyncio.CancelledError as cancelled:

                        async def retire_and_save() -> None:
                            await session.cancel_all()
                            await self._append_native_suffix(session, loaded.total)

                        await _close_after_error(cancelled, retire_and_save)
                        raise
                    except Exception as failure:
                        await _close_after_error(
                            failure, lambda: self._append_native_suffix(session, loaded.total)
                        )
                        raise

                    async def save_result() -> None:
                        try:
                            await self._append_native_suffix(session, loaded.total)
                        except ConversationsWriteError as error:
                            error.result = result
                            raise

                    await _join(asyncio.create_task(save_result()))
                    return cast("Completed[OutputT]", result)
            finally:
                self._active_session = None

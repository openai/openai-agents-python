"""Optional resources attached to one existing native run."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .models import ModelRunOwner


class _ModelRun:
    def __init__(self, session_id: str, turn_id: str) -> None:
        self.owner = ModelRunOwner(session_id, turn_id)
        self._models: dict[int, tuple[Any, Callable[[ModelRunOwner], Any]]] = {}
        self._closing: asyncio.Task[list[BaseException]] | None = None

    def admit(self, model: Any, cleanup: Callable[[ModelRunOwner], Any]) -> ModelRunOwner:
        if self._closing is not None:
            raise RuntimeError("Model run cleanup has already started")
        self._models.setdefault(id(model), (model, cleanup))
        return self.owner

    def close(
        self, invoke: Callable[[Callable[[ModelRunOwner], Any], ModelRunOwner], Awaitable[None]]
    ) -> asyncio.Task[list[BaseException]]:
        if self._closing is None:

            async def finish() -> list[BaseException]:
                models, self._models = self._models, {}
                settled = await asyncio.gather(
                    *(invoke(cleanup, self.owner) for _, cleanup in models.values()),
                    return_exceptions=True,
                )
                return [error for error in settled if isinstance(error, BaseException)]

            self._closing = asyncio.create_task(finish())
        return self._closing

"""Keep detached sandbox resource owners alive on their original event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

_T = TypeVar("_T")
_DEFAULT_CANCEL_GRACE_S = 5.0
_MIN_CANCEL_GRACE_S = 0.1


class _CleanupOwnerTask(asyncio.Task[_T]):
    """Keep cleanup alive briefly, then let loop shutdown cancel a stuck operation."""

    def __init__(
        self,
        coroutine: Any,
        *,
        loop: asyncio.AbstractEventLoop,
        name: str,
        cancel_grace_s: float,
    ) -> None:
        super().__init__(coroutine, loop=loop, name=name)
        self._cancel_grace_s = max(cancel_grace_s, _MIN_CANCEL_GRACE_S)
        self._forced_cancel_handle: asyncio.TimerHandle | None = None

    def cancel(self, msg: object = None) -> bool:
        if self.done():
            return super().cancel(msg)
        if self._forced_cancel_handle is None:
            self._forced_cancel_handle = self.get_loop().call_later(
                self._cancel_grace_s,
                self._force_cancel,
                msg,
            )
        return False

    def _force_cancel(self, msg: object) -> None:
        self._forced_cancel_handle = None
        if not self.done():
            asyncio.Task.cancel(self, msg)

    def _clear_forced_cancel(self, _done: asyncio.Future[_T]) -> None:
        handle = self._forced_cancel_handle
        self._forced_cancel_handle = None
        if handle is not None:
            handle.cancel()


def create_cleanup_owner(
    awaitable: Awaitable[_T], *, name: str, cancel_grace_s: float = _DEFAULT_CANCEL_GRACE_S
) -> asyncio.Task[_T]:
    """Create an owner that gets a bounded grace period during loop shutdown."""

    async def run() -> _T:
        return await awaitable

    task: _CleanupOwnerTask[_T] = _CleanupOwnerTask(
        run(),
        loop=asyncio.get_running_loop(),
        name=name,
        cancel_grace_s=cancel_grace_s,
    )
    task.add_done_callback(task._clear_forced_cancel)
    return task

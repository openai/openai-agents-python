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
        self._force_cancelling = False

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
            self._force_cancelling = True
            asyncio.Task.cancel(self, msg)

    def _clear_forced_cancel(self, _done: asyncio.Future[_T]) -> None:
        handle = self._forced_cancel_handle
        self._forced_cancel_handle = None
        if handle is not None:
            handle.cancel()


class _CleanupOwnerForcedShutdown(asyncio.CancelledError):
    """Propagate forced shutdown from a nested cleanup owner."""


def cleanup_owner_is_force_cancelling(
    error: BaseException | None = None,
) -> bool:
    """Return whether cleanup orchestration must stop for bounded loop shutdown."""

    if isinstance(error, _CleanupOwnerForcedShutdown):
        return True
    task = asyncio.current_task()
    return isinstance(task, _CleanupOwnerTask) and task._force_cancelling


def raise_if_cleanup_owner_force_cancelling(
    error: BaseException | None = None,
    *,
    nested_tasks: tuple[asyncio.Future[Any], ...] = (),
) -> None:
    """Stop orchestration when this owner or a nested owner was force-cancelled."""

    nested_force_cancelled = any(
        isinstance(task, _CleanupOwnerTask) and task._force_cancelling for task in nested_tasks
    )
    if cleanup_owner_is_force_cancelling(error) or nested_force_cancelled:
        raise _CleanupOwnerForcedShutdown() from None


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

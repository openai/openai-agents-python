"""Check immediate cancellation before an SDK-managed plugin side effect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from weakref import ReferenceType

    from ..agent import Agent
    from ..result import RunResultStreaming

T = TypeVar("T")
_cancelled: ContextVar[Callable[[], bool] | None] = ContextVar("plugin_cancelled", default=None)
streams: ContextVar[tuple[ReferenceType[RunResultStreaming], ...]] = ContextVar(
    "plugin_streams", default=()
)


def may_run_plugins(agent: Agent[Any]) -> bool:
    """Include parents that can delegate to a separately configured agent."""
    return bool(agent.plugins or agent.handoffs) or any(
        getattr(tool, "_is_agent_tool", False) for tool in agent.tools
    )


async def run_guarded(work: Coroutine[Any, Any, T]) -> T:
    # Batches run in shielded descendants. Checking the invoking task alone
    # misses immediate cancellation of a parent.
    parent = _cancelled.get()
    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    initial = cancelling() if cancelling is not None else 0
    waiter: asyncio.Future[T] | None = None
    stopped = False

    def cancelled() -> bool:
        return (
            stopped
            or (cancelling is not None and cancelling() > initial)
            or (waiter is not None and waiter.cancelled())
            or (parent is not None and parent())
        )

    token = _cancelled.set(cancelled)
    try:
        if cancelling is not None:
            return await work
        # Python 3.10 has no Task.cancelling(). The waiter's future receives
        # cancellation at once, before descendant tasks start to unwind.
        task = asyncio.create_task(work)
        waiter = asyncio.shield(task)
        try:
            return await waiter
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    finally:
        stopped = bool(cancelled())
        waiter = cancelling = None
        _cancelled.reset(token)


def cancelled() -> bool:
    owner = _cancelled.get()
    return (owner is not None and owner()) or any(
        (run := ref()) is not None and run._cancel_mode == "immediate" for ref in streams.get()
    )


async def check_cancelled() -> None:
    # Deliver a cancellation requested by the current task itself, also on
    # Python 3.10, where Task.cancelling() is unavailable.
    await asyncio.sleep(0)
    if cancelled():
        raise asyncio.CancelledError

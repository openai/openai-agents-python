from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar, overload

from ..logger import logger


@dataclass
class _SiblingCancellationScope:
    """Records whether `gather_with_cancel` is the one doing the cancelling."""

    cancelling: bool = False


# Holds a mutable scope rather than a bool so the flag stays observable from the
# child tasks: each task copies the context at creation time, and the copy keeps a
# reference to this same object.
_sibling_cancellation_scope: ContextVar[_SiblingCancellationScope | None] = ContextVar(
    "agents_sibling_cancellation_scope", default=None
)


def cancelled_by_failing_sibling() -> bool:
    """True when the current task is being cancelled because a sibling arm raised.

    Concurrent arms use this to tell "a peer failed, so finish unwinding and let the
    caller observe a drained arm" apart from "an ancestor is being torn down, so get
    out of the way immediately".
    """
    scope = _sibling_cancellation_scope.get()
    return scope is not None and scope.cancelling


# Long enough for an arm to run its own bounded teardown (function tools drain for
# _FUNCTION_TOOL_TEARDOWN_DRAIN_SECONDS), short enough that one executor which
# resists cancellation cannot hold the run open.
_ARM_DRAIN_SECONDS = 1.0


def _report_unfinished_arm(task: asyncio.Task[Any]) -> None:
    """Report an arm that outlived the drain window, at the loop level."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("Cancelled concurrent task failed after teardown: %s", error)


async def _cancel_and_drain(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    # Bounded: an executor that catches CancelledError and keeps cleaning up must
    # not stop the original failure from propagating. Anything still running is
    # reported through a done callback instead of being waited on.
    done, pending = await asyncio.wait(tasks, timeout=_ARM_DRAIN_SECONDS)
    for task in done:
        if not task.cancelled():
            # Retrieved so a failure during teardown is not reported as an
            # exception that was never retrieved.
            task.exception()
    for task in pending:
        task.add_done_callback(_report_unfinished_arm)


T = TypeVar("T")
T1 = TypeVar("T1")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")
T5 = TypeVar("T5")
T6 = TypeVar("T6")


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    /,
) -> tuple[T1, T2]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    /,
) -> tuple[T1, T2, T3]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    awaitable_4: Awaitable[T4],
    /,
) -> tuple[T1, T2, T3, T4]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    awaitable_4: Awaitable[T4],
    awaitable_5: Awaitable[T5],
    /,
) -> tuple[T1, T2, T3, T4, T5]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    awaitable_4: Awaitable[T4],
    awaitable_5: Awaitable[T5],
    awaitable_6: Awaitable[T6],
    /,
) -> tuple[T1, T2, T3, T4, T5, T6]: ...


@overload
async def gather_with_cancel(*awaitables: Awaitable[T]) -> tuple[T, ...]: ...


async def gather_with_cancel(*awaitables: Awaitable[Any]) -> tuple[Any, ...]:
    """Gather awaitables, cancelling and draining siblings when one raises."""
    if not awaitables:
        return ()

    scope = _SiblingCancellationScope()
    token = _sibling_cancellation_scope.set(scope)
    try:
        tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
        try:
            # Waiting on completion rather than `asyncio.gather` is what makes the
            # two cancellation causes distinguishable. An arm that ends badly comes
            # back as a finished task here, so this frame is demonstrably still
            # alive and the cancellation that follows originates with us. An
            # ancestor cancelling us instead raises out of the wait itself, leaving
            # `scope.cancelling` false so arms unwind promptly. Note that the arm may
            # end badly by *being cancelled* rather than raising, which is why this
            # cannot use FIRST_EXCEPTION: cancellation does not satisfy it.
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                # Ordered by original position so which failure wins is deterministic
                # when several arms finish in the same pass.
                for task in sorted(done, key=tasks.index):
                    if task.cancelled() or task.exception() is not None:
                        scope.cancelling = True
                        await _cancel_and_drain(tasks)
                        task.result()  # re-raises the arm's exception
        except BaseException:
            await _cancel_and_drain(tasks)
            raise
        return tuple(task.result() for task in tasks)
    finally:
        _sibling_cancellation_scope.reset(token)

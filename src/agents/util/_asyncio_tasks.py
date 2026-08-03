from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar, overload


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
    scope = _SiblingCancellationScope()
    token = _sibling_cancellation_scope.set(scope)
    try:
        tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException as error:
            # Only a genuine sibling failure flips the flag. If the gather itself was
            # cancelled, an ancestor is going away and arms should unwind promptly
            # rather than drain.
            scope.cancelling = not isinstance(error, asyncio.CancelledError)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    finally:
        _sibling_cancellation_scope.reset(token)

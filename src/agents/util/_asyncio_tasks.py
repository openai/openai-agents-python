from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, overload

T = TypeVar("T")
T1 = TypeVar("T1")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")
T5 = TypeVar("T5")
T6 = TypeVar("T6")


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """Retrieve a completed future's exception without changing its result semantics."""
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    /,
    *,
    on_child_failure: Callable[[], None] | None = None,
) -> tuple[T1, T2]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    /,
    *,
    on_child_failure: Callable[[], None] | None = None,
) -> tuple[T1, T2, T3]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    awaitable_4: Awaitable[T4],
    /,
    *,
    on_child_failure: Callable[[], None] | None = None,
) -> tuple[T1, T2, T3, T4]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    awaitable_4: Awaitable[T4],
    awaitable_5: Awaitable[T5],
    /,
    *,
    on_child_failure: Callable[[], None] | None = None,
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
    *,
    on_child_failure: Callable[[], None] | None = None,
) -> tuple[T1, T2, T3, T4, T5, T6]: ...


@overload
async def gather_with_cancel(
    *awaitables: Awaitable[T],
    on_child_failure: Callable[[], None] | None = None,
) -> tuple[T, ...]: ...


async def gather_with_cancel(
    *awaitables: Awaitable[Any],
    on_child_failure: Callable[[], None] | None = None,
) -> tuple[Any, ...]:
    """Gather awaitables, cancelling and draining siblings when one raises."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    gather_future = asyncio.gather(*tasks)
    gather_future.add_done_callback(_consume_future_exception)
    try:
        await asyncio.wait((gather_future,))
        try:
            return tuple(gather_future.result())
        except BaseException:
            if on_child_failure is not None:
                on_child_failure()
            raise
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

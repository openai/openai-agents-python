from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar, overload

T = TypeVar("T")
T1 = TypeVar("T1")
T2 = TypeVar("T2")
T3 = TypeVar("T3")


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
async def gather_with_cancel(*awaitables: Awaitable[T]) -> tuple[T, ...]: ...


async def gather_with_cancel(*awaitables: Awaitable[Any]) -> tuple[Any, ...]:
    """Gather awaitables, cancelling and draining siblings when one raises."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

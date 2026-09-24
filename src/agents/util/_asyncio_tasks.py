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
TProducer = TypeVar("TProducer")
TConsumer = TypeVar("TConsumer")


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
    except GeneratorExit:
        # Coroutine closure cannot suspend; the owner must tear down child tasks.
        raise
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def run_producer_consumer(
    producer: Awaitable[TProducer],
    consumer: Awaitable[TConsumer],
    /,
    *,
    fail_fast_exceptions: tuple[type[BaseException], ...] = (),
    on_failure: Callable[[], None] | None = None,
) -> tuple[TProducer, TConsumer]:
    """Run a producer and consumer with asymmetric failure handling.

    The producer must signal completion to the consumer in a ``finally`` block. A producer
    failure waits for the consumer to drain before propagating, while a consumer failure or
    parent cancellation cancels and drains the sibling task. Producer failures matching
    ``fail_fast_exceptions`` also cancel the consumer without waiting for it to drain;
    the producer must not wait to signal completion in those cases. Before awaiting cancelled
    tasks, ``on_failure`` runs synchronously so callers can stop upstream work during cleanup.
    """
    producer_task = asyncio.ensure_future(producer)
    consumer_task = asyncio.ensure_future(consumer)
    tasks = (producer_task, consumer_task)

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if consumer_task in done:
            consumer_result = consumer_task.result()
            producer_result = await producer_task
            return producer_result, consumer_result

        try:
            producer_result = producer_task.result()
        except BaseException as exc:
            if isinstance(exc, fail_fast_exceptions):
                raise
            await consumer_task
            raise

        consumer_result = await consumer_task
        return producer_result, consumer_result
    except GeneratorExit:
        # Coroutine closure cannot suspend; the owner must tear down child tasks.
        raise
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        if on_failure is not None:
            on_failure()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

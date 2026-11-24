from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast, overload

T = TypeVar("T")
T1 = TypeVar("T1")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")
T5 = TypeVar("T5")
T6 = TypeVar("T6")
TProducer = TypeVar("TProducer")
TConsumer = TypeVar("TConsumer")

_FUNCTION_TOOL_CANCELLED_DRAIN_SECONDS = 0.25


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
    on_child_failure: Callable[[BaseException], None] | None = None,
    cancelled_children: set[asyncio.Task[Any]] | None = None,
    cancelled_children_timeout: float = 0.0,
) -> tuple[T1, T2]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    /,
    *,
    on_child_failure: Callable[[BaseException], None] | None = None,
    cancelled_children: set[asyncio.Task[Any]] | None = None,
    cancelled_children_timeout: float = 0.0,
) -> tuple[T1, T2, T3]: ...


@overload
async def gather_with_cancel(
    awaitable_1: Awaitable[T1],
    awaitable_2: Awaitable[T2],
    awaitable_3: Awaitable[T3],
    awaitable_4: Awaitable[T4],
    /,
    *,
    on_child_failure: Callable[[BaseException], None] | None = None,
    cancelled_children: set[asyncio.Task[Any]] | None = None,
    cancelled_children_timeout: float = 0.0,
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
    on_child_failure: Callable[[BaseException], None] | None = None,
    cancelled_children: set[asyncio.Task[Any]] | None = None,
    cancelled_children_timeout: float = 0.0,
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
    on_child_failure: Callable[[BaseException], None] | None = None,
    cancelled_children: set[asyncio.Task[Any]] | None = None,
    cancelled_children_timeout: float = 0.0,
) -> tuple[T1, T2, T3, T4, T5, T6]: ...


@overload
async def gather_with_cancel(
    *awaitables: Awaitable[T],
    on_child_failure: Callable[[BaseException], None] | None = None,
    cancelled_children: set[asyncio.Task[Any]] | None = None,
    cancelled_children_timeout: float = 0.0,
) -> tuple[T, ...]: ...


async def gather_with_cancel(
    *awaitables: Awaitable[Any],
    on_child_failure: Callable[[BaseException], None] | None = None,
    cancelled_children: set[asyncio.Task[Any]] | None = None,
    cancelled_children_timeout: float = 0.0,
) -> tuple[Any, ...]:
    """Gather awaitables, cancelling and draining siblings when one raises.

    ``cancelled_children`` records children whose cancellation cleanup is already
    owned by the caller. A sibling failure gives them ``cancelled_children_timeout``
    seconds to finish before cancelling again. Parent cancellation always cancels
    all unfinished children without waiting for that grace period.
    """
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    gather_future = asyncio.gather(*tasks)
    gather_future.add_done_callback(_consume_future_exception)
    child_failed = False
    try:
        await asyncio.wait((gather_future,))
        try:
            return tuple(gather_future.result())
        except BaseException as error:
            child_failed = True
            if on_child_failure is not None:
                on_child_failure(error)
            raise
    except GeneratorExit:
        # Coroutine closure cannot suspend; the owner must tear down child tasks.
        raise
    except BaseException:
        owned_pending: list[asyncio.Future[Any]] = []
        for task in tasks:
            if task.done():
                continue
            if child_failed and cancelled_children is not None and task in cancelled_children:
                owned_pending.append(task)
            else:
                task.cancel()
        if owned_pending:
            try:
                _, pending = await asyncio.wait(owned_pending, timeout=cancelled_children_timeout)
            except BaseException:
                # Unlike gather, wait does not forward parent cancellation to children.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for task in cast(set[asyncio.Task[Any]], pending):
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _join_cleanup(*tasks: Awaitable[Any]) -> None:
    """Wait one grace period without forwarding further cancellation into child cleanup."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _FUNCTION_TOOL_CANCELLED_DRAIN_SECONDS
    pending = {asyncio.ensure_future(task) for task in tasks}
    for task in pending:
        task.add_done_callback(_consume_future_exception)
    cancellation: asyncio.CancelledError | None = None
    while pending and (remaining := deadline - loop.time()) > 0:
        try:
            _, pending = await asyncio.wait(pending, timeout=remaining)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        raise cancellation


async def run_producer_consumer(
    producer: Awaitable[TProducer],
    consumer: Awaitable[TConsumer],
    /,
    *,
    fail_fast_exceptions: tuple[type[BaseException], ...] = (),
    on_failure: Callable[[], None] | None = None,
    close_producer: Callable[[], Awaitable[None]] | None = None,
) -> tuple[TProducer, TConsumer]:
    """Run a producer and consumer with asymmetric failure handling.

    The producer must signal completion to the consumer in a ``finally`` block. A producer
    failure waits for the consumer to drain before propagating, while a consumer failure or
    parent cancellation cancels and drains the sibling task. Producer failures matching
    ``fail_fast_exceptions`` also cancel the consumer without waiting for it to drain;
    the producer must not wait to signal completion in those cases. Before awaiting cancelled
    tasks, ``on_failure`` runs synchronously so callers can stop upstream work. The optional
    ``close_producer`` runs after the producer stops, even if cleanup exceeds the grace period.
    """
    producer_task = asyncio.ensure_future(producer)
    consumer_task = asyncio.ensure_future(consumer)
    tasks = (producer_task, consumer_task)

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if consumer_task in done:
            consumer_result = consumer_task.result()
            producer_result = await asyncio.shield(producer_task)
            return producer_result, consumer_result

        try:
            producer_result = producer_task.result()
        except BaseException as exc:
            if isinstance(exc, fail_fast_exceptions):
                raise
            await asyncio.shield(consumer_task)
            raise

        consumer_result = await asyncio.shield(consumer_task)
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
        if close_producer is None:
            # The caller may own the iterator in an enclosing aclosing block.
            await asyncio.gather(*tasks, return_exceptions=True)
        else:

            async def finish_producer() -> None:
                await asyncio.gather(producer_task, return_exceptions=True)
                await close_producer()

            await _join_cleanup(finish_producer(), consumer_task)
        raise

from __future__ import annotations

import asyncio

import pytest

from agents.util._asyncio_tasks import gather_with_cancel, run_producer_consumer


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_gather_with_cancel_reports_child_failure_before_cancelling_siblings(
    error_type: type[BaseException],
) -> None:
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    child_failure_reported = asyncio.Event()

    async def sibling() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    async def fail_after_sibling_starts() -> None:
        await sibling_started.wait()
        raise error_type("child failed")

    with pytest.raises(error_type):
        await gather_with_cancel(
            sibling(),
            fail_after_sibling_starts(),
            on_child_failure=child_failure_reported.set,
        )

    assert child_failure_reported.is_set()
    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_gather_with_cancel_does_not_report_parent_cancellation_as_child_failure() -> None:
    children_started = 0
    all_children_started = asyncio.Event()
    child_failure_reported = asyncio.Event()
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()

    async def child() -> None:
        nonlocal children_started
        children_started += 1
        if children_started == 2:
            all_children_started.set()
        await asyncio.Event().wait()

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        task = asyncio.create_task(
            gather_with_cancel(
                child(),
                child(),
                on_child_failure=child_failure_reported.set,
            )
        )
        await all_children_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    assert not child_failure_reported.is_set()
    assert loop_errors == []


@pytest.mark.asyncio
async def test_run_producer_consumer_drains_consumer_before_producer_failure() -> None:
    class ProducerError(Exception):
        pass

    item_ready = asyncio.Event()
    allow_consumer_to_finish = asyncio.Event()
    consumer_finished = asyncio.Event()

    async def producer() -> None:
        item_ready.set()
        raise ProducerError("producer failed")

    async def consumer() -> None:
        await item_ready.wait()
        await allow_consumer_to_finish.wait()
        consumer_finished.set()

    task = asyncio.create_task(run_producer_consumer(producer(), consumer()))
    await item_ready.wait()
    await asyncio.sleep(0)

    assert not task.done()
    allow_consumer_to_finish.set()

    with pytest.raises(ProducerError, match="producer failed"):
        await task
    assert consumer_finished.is_set()


@pytest.mark.asyncio
async def test_run_producer_consumer_cancels_producer_after_consumer_failure() -> None:
    class ConsumerError(BaseException):
        pass

    producer_started = asyncio.Event()
    producer_cancelled = asyncio.Event()

    async def producer() -> None:
        producer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            producer_cancelled.set()

    async def consumer() -> None:
        await producer_started.wait()
        raise ConsumerError("consumer failed")

    with pytest.raises(ConsumerError, match="consumer failed"):
        await run_producer_consumer(producer(), consumer())
    assert producer_cancelled.is_set()

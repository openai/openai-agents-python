from __future__ import annotations

import asyncio

import pytest

from agents.util._asyncio_tasks import run_producer_consumer


@pytest.mark.asyncio
async def test_run_producer_consumer_preserves_producer_failure_when_consumer_also_fails() -> None:
    class ProducerError(Exception):
        pass

    class ConsumerError(Exception):
        pass

    producer_failed = asyncio.Event()
    allow_consumer_failure = asyncio.Event()
    consumer_failed = asyncio.Event()

    async def producer() -> None:
        producer_failed.set()
        raise ProducerError("producer failed")

    async def consumer() -> None:
        await allow_consumer_failure.wait()
        consumer_failed.set()
        raise ConsumerError("consumer failed while draining")

    task = asyncio.create_task(run_producer_consumer(producer(), consumer()))
    await producer_failed.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done()

    allow_consumer_failure.set()
    with pytest.raises(ProducerError, match="producer failed"):
        await task

    assert consumer_failed.is_set()

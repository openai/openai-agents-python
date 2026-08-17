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
    consumer_failed = asyncio.Event()

    async def producer() -> None:
        producer_failed.set()
        raise ProducerError("producer failed")

    async def consumer() -> None:
        await producer_failed.wait()
        consumer_failed.set()
        raise ConsumerError("consumer failed while draining")

    with pytest.raises(ProducerError, match="producer failed"):
        await run_producer_consumer(producer(), consumer())

    assert consumer_failed.is_set()

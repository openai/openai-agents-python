"""Event consumers terminate independently of transport cleanup."""

import asyncio

import pytest

from agents.realtime.agent import RealtimeAgent
from agents.realtime.model_events import RealtimeModelOtherEvent
from agents.realtime.session import RealtimeSession
from agents.realtime.testing import ScriptedRealtimeModel


async def _collect(session: RealtimeSession) -> list[str]:
    return [event.type async for event in session]


async def _wait_for_readers(session: RealtimeSession, count: int) -> None:
    async def wait() -> None:
        while session._event_iterator_waiters != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("transport close failed"), asyncio.CancelledError()]
)
async def test_readers_finish_before_transport_cleanup(failure: BaseException | None):
    class PausedCloseModel(ScriptedRealtimeModel):
        def __init__(self) -> None:
            super().__init__(strict=False)
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.attempts = 0

        async def close(self) -> None:
            self.attempts += 1
            self.started.set()
            await self.release.wait()
            if self.attempts == 1 and failure is not None:
                raise failure
            await super().close()

    model = PausedCloseModel()
    session = RealtimeSession(model, RealtimeAgent(name="test"), None)
    await session.enter()
    readers = [asyncio.create_task(_collect(session)) for _ in range(2)]
    closer: asyncio.Task[None] | None = None
    try:
        await _wait_for_readers(session, 2)
        closer = asyncio.create_task(session.close())
        await asyncio.wait_for(model.started.wait(), timeout=1)

        results = await asyncio.wait_for(asyncio.gather(*readers), timeout=1)
        assert sorted(results) == [[], ["history_updated"]]
        assert not closer.done()
        assert not session._closed
        assert model.listeners == ()

        model.release.set()
        if failure is None:
            await closer
        else:
            with pytest.raises(type(failure)):
                await closer
            assert not session._closed
            await session.close()

        assert session._closed
        assert model.attempts == (1 if failure is None else 2)
    finally:
        model.release.set()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        if closer is not None:
            await asyncio.gather(closer, return_exceptions=True)
        await session.close()


@pytest.mark.asyncio
async def test_reader_started_after_failed_close_receives_buffered_events():
    model = ScriptedRealtimeModel(close_error=RuntimeError("transport close failed"), strict=False)
    session = RealtimeSession(model, RealtimeAgent(name="test"), None)
    await session.enter()
    await model.emit(RealtimeModelOtherEvent(data={"test": "buffered"}))
    try:
        with pytest.raises(RuntimeError, match="transport close failed"):
            await session.close()

        assert await asyncio.wait_for(_collect(session), timeout=1) == [
            "history_updated",
            "raw_model_event",
        ]
        assert await asyncio.wait_for(_collect(session), timeout=1) == []
        assert not session._closed
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_processing_reader_cannot_strand_another_reader_during_close():
    model = ScriptedRealtimeModel(close_error=RuntimeError("transport close failed"), strict=False)
    session = RealtimeSession(model, RealtimeAgent(name="test"), None)
    await session.enter()
    processing = asyncio.Event()
    resume = asyncio.Event()

    async def process() -> list[str]:
        events = []
        async for event in session:
            events.append(event.type)
            processing.set()
            await resume.wait()
        return events

    active = asyncio.create_task(process())
    parked: asyncio.Task[list[str]] | None = None
    try:
        await asyncio.wait_for(processing.wait(), timeout=1)
        parked = asyncio.create_task(_collect(session))
        await _wait_for_readers(session, 1)

        # Schedule the processing reader before close wakes the queue's reader.
        resume.set()
        with pytest.raises(RuntimeError, match="transport close failed"):
            await session.close()

        assert await asyncio.wait_for(asyncio.gather(active, parked), timeout=1) == [
            ["history_updated"],
            [],
        ]
    finally:
        active.cancel()
        if parked is not None:
            parked.cancel()
            await asyncio.gather(parked, return_exceptions=True)
        await asyncio.gather(active, return_exceptions=True)
        await session.close()

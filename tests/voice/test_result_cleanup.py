"""A consumer that stops early must still get the producer tasks cancelled."""

from __future__ import annotations

import asyncio

import pytest

from agents.voice.result import StreamedAudioResult


def _make_result() -> StreamedAudioResult:
    result = StreamedAudioResult.__new__(StreamedAudioResult)
    result._queue = asyncio.Queue()  # type: ignore[attr-defined]
    result._tasks = []  # type: ignore[attr-defined]
    result._dispatcher_task = None  # type: ignore[attr-defined]
    result.text_generation_task = None  # type: ignore[attr-defined]
    result._stored_exception = None  # type: ignore[attr-defined]
    result._done_processing = False  # type: ignore[attr-defined]
    result._buffer = []  # type: ignore[attr-defined]
    result._playing = False  # type: ignore[attr-defined]
    result._tracing_span = None  # type: ignore[attr-defined]
    result._ordered_done = False  # type: ignore[attr-defined]
    return result


@pytest.mark.asyncio
async def test_early_break_cancels_producer_tasks() -> None:
    """`break` out of the stream leaves nothing running."""
    result = _make_result()
    started = asyncio.Event()

    async def producer() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(producer())
    result._tasks.append(task)  # type: ignore[attr-defined]
    await started.wait()

    from agents.voice.events import VoiceStreamEventLifecycle

    await result._queue.put(VoiceStreamEventLifecycle(event="turn_started"))  # type: ignore[attr-defined]

    stream = result.stream()
    async for _event in stream:
        break
    await stream.aclose()

    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_normal_exhaustion_still_cleans_up() -> None:
    """The existing path is unchanged: `None` ends the stream and cleanup runs."""
    result = _make_result()

    async def producer() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(producer())
    result._tasks.append(task)  # type: ignore[attr-defined]

    await result._queue.put(None)  # type: ignore[attr-defined]

    events = [event async for event in result.stream()]

    assert events == []
    assert task.cancelled() or task.done()

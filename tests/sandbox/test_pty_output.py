from __future__ import annotations

import asyncio
from collections import deque

import pytest

from agents.sandbox.session.pty_output import collect_pty_output


@pytest.mark.asyncio
async def test_collect_pty_output_waits_for_notification() -> None:
    output_chunks: deque[bytes] = deque()
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()
    done = False

    async def produce_output() -> None:
        nonlocal done
        await asyncio.sleep(0)
        async with output_lock:
            output_chunks.append(b"notified output")
        done = True
        output_notify.set()

    producer_task = asyncio.create_task(produce_output())
    output, original_token_count = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=500,
        max_output_tokens=None,
    )
    await producer_task

    assert output == b"notified output"
    assert original_token_count is None


@pytest.mark.asyncio
async def test_collect_pty_output_drains_chunks_added_when_done() -> None:
    output_chunks = deque([b"before done"])

    def mark_done() -> bool:
        output_chunks.append(b" after done")
        return True

    output, original_token_count = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=mark_done,
        yield_time_ms=500,
        max_output_tokens=None,
    )

    assert output == b"before done after done"
    assert original_token_count is None


async def _one_window(
    chunks: deque[bytes],
    lock: asyncio.Lock,
    notify: asyncio.Event,
    done: dict[str, bool],
) -> bytes:
    notify.set()
    collected, _ = await collect_pty_output(
        output_chunks=chunks,
        output_lock=lock,
        output_notify=notify,
        is_done=lambda: done["value"],
        yield_time_ms=1,
        max_output_tokens=None,
    )
    return collected


# one, two, three and four byte characters, so every sequence width is split
SPLIT_TEXT = "aé☃\U0001d11eb"


@pytest.mark.asyncio
@pytest.mark.parametrize("split", range(1, len(SPLIT_TEXT.encode("utf-8"))))
async def test_collect_pty_output_keeps_a_character_split_across_windows(split: int) -> None:
    text = SPLIT_TEXT
    raw = text.encode("utf-8")

    chunks: deque[bytes] = deque()
    lock = asyncio.Lock()
    notify = asyncio.Event()
    done = {"value": False}

    chunks.append(raw[:split])
    first = await _one_window(chunks, lock, notify, done)
    chunks.append(raw[split:])
    done["value"] = True
    second = await _one_window(chunks, lock, notify, done)

    assert (first + second).decode("utf-8") == text


@pytest.mark.asyncio
async def test_collect_pty_output_replaces_a_truncated_character_once_done() -> None:
    # the stream ends mid character, so there is no later window to complete it
    chunks: deque[bytes] = deque([b"hi " + "é".encode()[:1]])

    collected, _ = await collect_pty_output(
        output_chunks=chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: True,
        yield_time_ms=1,
        max_output_tokens=None,
    )

    assert collected.decode("utf-8") == "hi �"
    assert not chunks


@pytest.mark.asyncio
async def test_collect_pty_output_leaves_complete_multibyte_output_alone() -> None:
    chunks: deque[bytes] = deque(["héllo".encode()])

    collected, _ = await collect_pty_output(
        output_chunks=chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: True,
        yield_time_ms=1,
        max_output_tokens=None,
    )

    assert collected.decode("utf-8") == "héllo"

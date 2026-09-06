from __future__ import annotations

import asyncio
from collections import deque

import pytest

from agents.sandbox.session.pty_output import (
    collect_pty_output,
    incomplete_utf8_tail_length,
)


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


@pytest.mark.asyncio
async def test_collect_pty_output_holds_back_a_split_utf8_sequence_until_it_completes() -> None:
    output_chunks: deque[bytes] = deque([b"prefix \xe4\xb8"])
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()

    first_output, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: False,
        yield_time_ms=10,
        max_output_tokens=None,
    )
    assert first_output == b"prefix "
    assert list(output_chunks) == [b"\xe4\xb8"]

    output_chunks.append(b"\xad\n")
    second_output, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: False,
        yield_time_ms=10,
        max_output_tokens=None,
    )
    assert second_output == "中\n".encode()
    assert not output_chunks


@pytest.mark.asyncio
async def test_collect_pty_output_flushes_an_incomplete_sequence_when_done() -> None:
    output_chunks: deque[bytes] = deque([b"tail \xe4\xb8"])

    output, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: True,
        yield_time_ms=10,
        max_output_tokens=None,
    )
    assert output == "tail \ufffd".encode()
    assert not output_chunks


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", 0),
        (b"ascii", 0),
        ("中".encode(), 0),
        (b"\xe4", 1),
        (b"\xe4\xb8", 2),
        (b"\xf0\x9f\x98", 3),
        (b"\xc3", 1),
        (b"ok \xf0\x9f", 2),
        (b"\xb8\xad", 0),  # stray continuation bytes are invalid, not incomplete
        (b"\xff", 0),  # invalid lead byte
        (b"\xe4\xb8\xad\xe4", 1),
    ],
)
def test_incomplete_utf8_tail_length(data: bytes, expected: int) -> None:
    assert incomplete_utf8_tail_length(data) == expected

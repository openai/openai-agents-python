from __future__ import annotations

import asyncio
from collections import deque

import pytest

from agents.sandbox.session.pty_output import (
    _incomplete_utf8_suffix_length,
    collect_pty_output,
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
async def test_collect_pty_output_holds_split_utf8_character_for_the_next_window() -> None:
    """A character split across two collection windows must survive, not become U+FFFD."""
    text = "h\u00e9llo w\u00f6rld \u2705"
    raw = text.encode("utf-8")
    split = 2  # after "h" and the first byte of the two-byte "e" with acute

    output_chunks: deque[bytes] = deque()
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()
    producer_done = False

    async def window() -> bytes:
        output_notify.set()
        collected, _ = await collect_pty_output(
            output_chunks=output_chunks,
            output_lock=output_lock,
            output_notify=output_notify,
            is_done=lambda: producer_done,
            yield_time_ms=1,
            max_output_tokens=None,
        )
        return collected

    output_chunks.append(raw[:split])
    first = await window()
    # The partial sequence is withheld rather than decoded, so the window ends on "h".
    assert first == b"h"

    output_chunks.append(raw[split:])
    producer_done = True
    second = await window()

    assert (first + second).decode("utf-8") == text


@pytest.mark.asyncio
async def test_collect_pty_output_replaces_partial_utf8_once_the_producer_is_done() -> None:
    """Nothing can complete a truncated sequence after the producer closes, so replace it."""
    output_chunks: deque[bytes] = deque([b"ok\xc3"])
    output_notify = asyncio.Event()
    output_notify.set()

    output, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=output_notify,
        is_done=lambda: True,
        yield_time_ms=1,
        max_output_tokens=None,
    )

    assert output.decode("utf-8") == "ok\ufffd"
    assert not output_chunks


@pytest.mark.parametrize("lead", [b"\xc0", b"\xc1", b"\xf5", b"\xff"])
@pytest.mark.asyncio
async def test_collect_pty_output_replaces_bytes_that_cannot_start_a_character(
    lead: bytes,
) -> None:
    """Only real lead bytes may be carried; others can never be completed by later output."""
    output_chunks: deque[bytes] = deque([b"prompt" + lead])
    output_notify = asyncio.Event()
    output_notify.set()

    output, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=output_notify,
        is_done=lambda: False,
        yield_time_ms=1,
        max_output_tokens=None,
    )

    # Replaced in this window rather than withheld from an interactive prompt forever.
    assert output.decode("utf-8") == "prompt\ufffd"
    assert not output_chunks


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"abc", 0),
        (b"a\xc3", 1),
        (b"a\xc3\xa9", 0),
        (b"a\xe2", 1),
        (b"a\xe2\x82", 2),
        (b"a\xe2\x82\xac", 0),
        (b"a\xf0", 1),
        (b"a\xf0\x9f", 2),
        (b"a\xf0\x9f\x98", 3),
        (b"a\xf0\x9f\x98\x80", 0),
        (b"a\xc0", 0),
        (b"a\xc1", 0),
        (b"a\xf5", 0),
        (b"a\xff", 0),
        (b"a\x80", 0),
        (b"\x80\x80\x80\x80", 0),
    ],
)
def test_incomplete_utf8_suffix_length(data: bytes, expected: int) -> None:
    """Every split position of every sequence length, plus bytes that can never complete."""
    assert _incomplete_utf8_suffix_length(data) == expected


@pytest.mark.parametrize("split", [1, 2])
@pytest.mark.asyncio
async def test_collect_pty_output_holds_three_byte_character_split_at_any_point(
    split: int,
) -> None:
    """A three-byte character must survive a window boundary after one or two bytes."""
    text = "a\u20acb"
    raw = text.encode("utf-8")
    boundary = 1 + split  # after "a" plus part of the euro sign

    output_chunks: deque[bytes] = deque([raw[:boundary]])
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()
    producer_done = False

    async def window() -> bytes:
        output_notify.set()
        collected, _ = await collect_pty_output(
            output_chunks=output_chunks,
            output_lock=output_lock,
            output_notify=output_notify,
            is_done=lambda: producer_done,
            yield_time_ms=1,
            max_output_tokens=None,
        )
        return collected

    first = await window()
    assert first == b"a"

    output_chunks.append(raw[boundary:])
    producer_done = True
    second = await window()

    assert (first + second).decode("utf-8") == text

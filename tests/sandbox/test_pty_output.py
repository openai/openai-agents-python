from __future__ import annotations

import asyncio
from collections import deque

import pytest

from agents.sandbox.session.pty_output import collect_pty_output, decode_pty_window


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
async def test_collect_pty_output_preserves_char_split_across_windows() -> None:
    # A multi-byte character split across two collection windows over the same
    # persistent deque must not be corrupted into U+FFFD. Regression for #4744.
    text = "héllo wörld"
    raw = text.encode("utf-8")
    split = raw.index(b"\xb6")  # inside the "ö": its trailing continuation byte

    output_chunks: deque[bytes] = deque([raw[:split]])
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()

    # First, non-final window: returns on the deadline and must hold back the
    # incomplete tail for the next window instead of decoding it to U+FFFD.
    first, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: False,
        yield_time_ms=10,
        max_output_tokens=None,
    )

    # Second, final window: the rest of the character arrives.
    output_chunks.append(raw[split:])
    second, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: True,
        yield_time_ms=10,
        max_output_tokens=None,
    )

    combined = (first + second).decode("utf-8")
    assert combined == text
    assert "�" not in combined


@pytest.mark.asyncio
async def test_collect_pty_output_replaces_invalid_byte_without_withholding() -> None:
    # An invalid UTF-8 lead byte (0xC0) must decode to U+FFFD immediately in a
    # non-final window, not be buffered as if it were an incomplete sequence.
    output_chunks: deque[bytes] = deque([b"ok\xc0"])

    out, _ = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: False,
        yield_time_ms=10,
        max_output_tokens=None,
    )

    assert out.decode("utf-8") == "ok�"
    assert not output_chunks  # nothing was withheld


def test_decode_pty_window_holds_incomplete_and_replaces_invalid() -> None:
    raw = "wörld".encode()  # b"w\xc3\xb6rld"

    # A genuinely incomplete trailing sequence is returned as leftover.
    text, leftover = decode_pty_window(raw[:2], final=False)  # b"w\xc3"
    assert leftover == b"\xc3"
    text2, leftover2 = decode_pty_window(leftover + raw[2:], final=False)
    assert text + text2 == "wörld"
    assert leftover2 == b""

    # An invalid byte becomes U+FFFD immediately, nothing held back.
    text3, leftover3 = decode_pty_window(b"ok\xc0", final=False)
    assert text3 == "ok�"
    assert leftover3 == b""

    # final=True flushes any pending bytes as U+FFFD.
    text4, leftover4 = decode_pty_window(b"w\xc3", final=True)
    assert text4 == "w�"
    assert leftover4 == b""

from __future__ import annotations

import asyncio
import codecs
import time
from collections import deque
from collections.abc import Callable

from .pty_types import truncate_text_by_tokens


async def collect_pty_output(
    *,
    output_chunks: deque[bytes],
    output_lock: asyncio.Lock,
    output_notify: asyncio.Event,
    is_done: Callable[[], bool],
    yield_time_ms: int,
    max_output_tokens: int | None,
) -> tuple[bytes, int | None]:
    """Collect and truncate PTY output until the deadline or provider completion."""
    deadline = time.monotonic() + (yield_time_ms / 1000)
    output = bytearray()

    while True:
        async with output_lock:
            while output_chunks:
                output.extend(output_chunks.popleft())

        if time.monotonic() >= deadline:
            break

        if is_done():
            async with output_lock:
                while output_chunks:
                    output.extend(output_chunks.popleft())
            break

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            break

        try:
            await asyncio.wait_for(output_notify.wait(), timeout=remaining_s)
        except asyncio.TimeoutError:
            break
        output_notify.clear()

    if not is_done():
        # A multibyte UTF-8 sequence can straddle two yield windows (the producer wrote
        # part of it before the deadline). Hold the incomplete tail back for the next
        # collection instead of emitting replacement characters on both sides.
        tail_length = incomplete_utf8_tail_length(output)
        if tail_length:
            async with output_lock:
                output_chunks.appendleft(bytes(output[-tail_length:]))
            del output[-tail_length:]
    text = output.decode("utf-8", errors="replace")
    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count


def incomplete_utf8_tail_length(data: bytes | bytearray) -> int:
    """Return how many trailing bytes form a valid but not yet complete UTF-8 sequence.

    Python's incremental decoder decides what counts as a valid prefix, so invalid
    leaders (``0xC0``, ``0xC1``, ``0xF5`` and up) and ill-formed second bytes (overlong
    forms, surrogates, code points past U+10FFFF) are not held back: they decode to
    replacement characters immediately, as before. A pending sequence is at most three
    bytes long, so only the tail needs to be inspected.
    """
    tail = bytes(data[-3:])
    if not tail:
        return 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder.decode(tail, final=False)
    pending, _ = decoder.getstate()
    return len(pending)


async def drain_pty_output_chunks(output_chunks: deque[bytes], output_lock: asyncio.Lock) -> bytes:
    """Take every queued chunk, including bytes a collection held back."""
    output = bytearray()
    async with output_lock:
        while output_chunks:
            output.extend(output_chunks.popleft())
    return bytes(output)

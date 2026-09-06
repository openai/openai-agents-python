from __future__ import annotations

import asyncio
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
    """Return how many trailing bytes start a UTF-8 sequence that is not yet complete.

    Only a well-formed prefix counts: a lead byte followed by fewer continuation bytes
    than it announces. Invalid bytes are left alone so they decode as replacement
    characters immediately rather than being held forever.
    """
    limit = min(len(data), 3)
    for offset in range(1, limit + 1):
        byte = data[-offset]
        if byte & 0xC0 == 0x80:
            continue  # continuation byte; keep looking for the lead byte
        if byte & 0xE0 == 0xC0:
            expected = 2
        elif byte & 0xF0 == 0xE0:
            expected = 3
        elif byte & 0xF8 == 0xF0:
            expected = 4
        else:
            return 0  # ASCII or an invalid lead byte
        return offset if offset < expected else 0
    return 0

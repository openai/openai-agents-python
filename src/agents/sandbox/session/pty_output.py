from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable

from .pty_types import truncate_text_by_tokens


def _incomplete_utf8_tail_len(buf: bytes | bytearray) -> int:
    """Return the number of trailing bytes that begin an incomplete UTF-8 sequence.

    Returns 0 when ``buf`` ends on a character boundary or with bytes that cannot
    start a longer sequence, so decoding the whole buffer is safe.
    """
    for i in range(1, min(4, len(buf)) + 1):
        byte = buf[-i]
        if byte & 0xC0 == 0x80:
            # Continuation byte; keep scanning back for its lead byte.
            continue
        if byte & 0x80 == 0x00:
            seq_len = 1
        elif byte & 0xE0 == 0xC0:
            seq_len = 2
        elif byte & 0xF0 == 0xE0:
            seq_len = 3
        elif byte & 0xF8 == 0xF0:
            seq_len = 4
        else:
            # Invalid lead byte; nothing worth holding back.
            return 0
        return i if i < seq_len else 0
    return 0


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
    final = False

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
            final = True
            break

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            break

        try:
            await asyncio.wait_for(output_notify.wait(), timeout=remaining_s)
        except asyncio.TimeoutError:
            break
        output_notify.clear()

    # A multi-byte character may be split across two collection windows. Unless
    # the process is done (no further windows will arrive), hold back a trailing
    # incomplete UTF-8 sequence and push it to the front of the shared deque so
    # the next window can complete it, instead of both halves decoding to U+FFFD.
    if not final:
        tail_len = _incomplete_utf8_tail_len(output)
        if tail_len:
            tail = bytes(output[-tail_len:])
            del output[-tail_len:]
            async with output_lock:
                output_chunks.appendleft(tail)

    text = output.decode("utf-8", errors="replace")
    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count

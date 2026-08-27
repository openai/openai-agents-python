from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable

from .pty_types import truncate_text_by_tokens


def _incomplete_utf8_suffix_length(data: bytes | bytearray) -> int:
    """Return how many trailing bytes start a UTF-8 sequence that is not finished yet."""
    for back in range(1, min(3, len(data)) + 1):
        byte = data[-back]
        if byte < 0x80:
            return 0
        if byte >= 0xC0:
            if byte >= 0xF0:
                needed = 4
            elif byte >= 0xE0:
                needed = 3
            else:
                needed = 2
            return back if back < needed else 0
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
        # A multi-byte character can straddle two collection windows. Decoding a partial
        # sequence with errors="replace" destroys those bytes, so hold the unfinished tail
        # back for the next window. Once the producer is done nothing can complete it, so
        # the replacement behaviour below is the right answer then.
        carry = _incomplete_utf8_suffix_length(output)
        if carry:
            tail = bytes(output[-carry:])
            del output[-carry:]
            async with output_lock:
                output_chunks.appendleft(tail)

    text = output.decode("utf-8", errors="replace")
    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count

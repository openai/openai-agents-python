from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable

from .pty_types import truncate_text_by_tokens


def _incomplete_utf8_suffix_len(data: bytes | bytearray) -> int:
    """Length of a trailing UTF-8 sequence that is still waiting for its remaining bytes.

    Returns 0 when the buffer does not end mid character.
    """
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte < 0x80:
            return 0
        if byte >= 0xC0:
            if byte < 0xE0:
                expected = 2
            elif byte < 0xF0:
                expected = 3
            else:
                expected = 4
            return back if back < expected else 0
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

    # PTY output is collected in repeated windows over one persistent deque, so a
    # character whose bytes straddle a window boundary would be replaced twice and
    # lost. Hand the incomplete tail back for the next window to finish. Once the
    # provider is done there is no next window, so the bytes are genuinely invalid.
    if not is_done():
        held_back = _incomplete_utf8_suffix_len(output)
        if held_back:
            tail = bytes(output[-held_back:])
            del output[-held_back:]
            async with output_lock:
                output_chunks.appendleft(tail)

    text = output.decode("utf-8", errors="replace")
    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count

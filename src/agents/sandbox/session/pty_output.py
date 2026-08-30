from __future__ import annotations

import asyncio
import codecs
import time
from collections import deque
from collections.abc import Callable

from .pty_types import truncate_text_by_tokens


def decode_pty_window(data: bytes, *, final: bool) -> tuple[str, bytes]:
    """Decode one window of PTY output.

    PTY output is collected in repeated windows over one persistent stream, so a
    multi-byte character can be split across a window boundary. Decode
    incrementally: invalid bytes still become U+FFFD immediately (matching
    ``errors="replace"``), while a genuinely incomplete trailing sequence is
    returned as ``leftover`` for the caller to prepend to the next window. When
    ``final`` is set there is no next window, so nothing is held back.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(data, final=final)
    leftover = b"" if final else bytes(decoder.getstate()[0])
    return text, leftover


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

    # Re-check completion at the finalization boundary: if the process finished
    # after the loop broke on its deadline, there is no further window, so we
    # must flush rather than requeue (the requeued entry would be discarded).
    final = final or is_done()

    text, leftover = decode_pty_window(bytes(output), final=final)
    if leftover:
        async with output_lock:
            output_chunks.appendleft(leftover)

    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count

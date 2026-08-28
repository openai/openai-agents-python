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

    # Output is collected in repeated windows over one persistent deque, so a character
    # whose bytes straddle a window boundary used to be replaced twice and lost. An
    # incremental decoder keeps that trailing partial sequence instead of replacing it,
    # and it is handed back for the next window to finish. Bytes that cannot begin a
    # character are not held, they are replaced straight away as before. Completing the
    # decoder once the provider is done replaces a tail that no later window will finish.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(output, final=is_done())
    pending = decoder.getstate()[0]
    if pending:
        async with output_lock:
            output_chunks.appendleft(bytes(pending))

    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count

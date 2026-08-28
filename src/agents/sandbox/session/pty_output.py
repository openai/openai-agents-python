from __future__ import annotations

import asyncio
import codecs
import time
from collections import deque
from collections.abc import Callable

from .pty_types import truncate_text_by_tokens


def decode_pty_window(data: bytes | bytearray, *, is_final: bool) -> tuple[str, bytes]:
    """Decode one collection window, and return what has to wait for the next one.

    PTY output arrives in repeated windows, so decoding each one with ``errors="replace"``
    destroys any character whose bytes straddle a boundary. The returned bytes are the tail
    the caller has to put back in front of the next window.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(data, final=is_final)
    pending = bytes(decoder.getstate()[0])

    # The decoder holds ED A0..BF, which leads a surrogate, even though no third byte can
    # complete it. Those 32 prefixes are the only thing it ever buffers that cannot become a
    # character, so handing them back would keep the output hidden for as long as the process
    # runs. Replace them here instead, the same way the decoder would once it is closed.
    if len(pending) == 2 and pending[0] == 0xED and pending[1] >= 0xA0:
        text += pending.decode("utf-8", errors="replace")
        pending = b""

    return text, pending


def close_pty_tail(
    *,
    leftover: bytes | bytearray,
    output: bytes,
    original_token_count: int | None,
    max_output_tokens: int | None,
) -> tuple[bytes, int | None]:
    """Fold a tail a collection window left behind into the output of a finished session.

    A window hands an unfinished character back while the stream still looks open, but each
    backend decides separately that the process has gone, and it can decide that after the last
    collection. Whatever is still waiting then has no later window to complete it, so it is
    replaced here rather than leaving with the session.

    The result is truncated again because the tail is added after the window already applied
    ``max_output_tokens``, and the returned count covers the text including it.
    """
    if not leftover:
        return output, original_token_count

    tail, _ = decode_pty_window(leftover, is_final=True)
    if not tail:
        return output, original_token_count

    text = output.decode("utf-8", errors="replace") + tail
    truncated, recounted = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), recounted


async def flush_pty_tail(
    *,
    output_chunks: deque[bytes],
    output_lock: asyncio.Lock,
    output: bytes,
    original_token_count: int | None,
    max_output_tokens: int | None,
) -> tuple[bytes, int | None]:
    """Drain what a session still holds and close it with :func:`close_pty_tail`."""
    leftover = bytearray()
    async with output_lock:
        while output_chunks:
            leftover.extend(output_chunks.popleft())

    return close_pty_tail(
        leftover=leftover,
        output=output,
        original_token_count=original_token_count,
        max_output_tokens=max_output_tokens,
    )


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
    text, pending = decode_pty_window(output, is_final=is_done())
    if pending:
        async with output_lock:
            output_chunks.appendleft(pending)

    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

from .pty_types import truncate_text_by_tokens


def _incomplete_utf8_suffix_length(data: bytes | bytearray) -> int:
    """Return the trailing byte count that can still become one valid UTF-8 scalar."""
    continuation_count = 0
    for byte in reversed(data[-3:]):
        if 0x80 <= byte <= 0xBF:
            continuation_count += 1
            continue
        break

    lead_index = len(data) - continuation_count - 1
    if lead_index < 0:
        return 0

    lead = data[lead_index]
    if 0xC2 <= lead <= 0xDF:
        expected_length = 2
    elif 0xE0 <= lead <= 0xEF:
        expected_length = 3
    elif 0xF0 <= lead <= 0xF4:
        expected_length = 4
    else:
        return 0

    suffix_length = continuation_count + 1
    if suffix_length >= expected_length:
        return 0

    if continuation_count:
        second = data[lead_index + 1]
        if (
            (lead == 0xE0 and second < 0xA0)
            or (lead == 0xED and second > 0x9F)
            or (lead == 0xF0 and second < 0x90)
            or (lead == 0xF4 and second > 0x8F)
        ):
            return 0

    return suffix_length


async def _drain_output_chunks(
    output_chunks: deque[bytes],
    output_lock: asyncio.Lock,
    output: bytearray,
) -> None:
    async with output_lock:
        while output_chunks:
            output.extend(output_chunks.popleft())


async def _drain_and_carry_incomplete_suffix(
    output_chunks: deque[bytes],
    output_lock: asyncio.Lock,
    output: bytearray,
) -> None:
    """Drain and restore a carryable suffix without yielding between ownership changes."""
    async with output_lock:
        while output_chunks:
            output.extend(output_chunks.popleft())

        carry = _incomplete_utf8_suffix_length(output)
        if carry:
            tail = bytes(output[-carry:])
            del output[-carry:]
            output_chunks.appendleft(tail)


async def _restore_unreturned_output(
    output_chunks: deque[bytes],
    output_lock: asyncio.Lock,
    output: bytearray,
) -> None:
    """Restore bytes owned by a cancelled collection ahead of later queued bytes."""
    if not output:
        return

    async with output_lock:
        output_chunks.appendleft(bytes(output))


async def collect_pty_output(
    *,
    output_chunks: deque[bytes],
    output_lock: asyncio.Lock,
    output_notify: asyncio.Event,
    is_done: Callable[[], bool],
    should_return: Callable[[], bool] | None = None,
    yield_time_ms: int,
    max_output_tokens: int | None,
    poll_output: Callable[[float], Awaitable[None]] | None = None,
    settle_output: Callable[[], Awaitable[None]] | None = None,
    wait_for_output: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[bytes, int | None, bool]:
    """Collect raw PTY bytes until the deadline or producer completion.

    poll_output adapts pull-based providers into output_chunks. Queue draining,
    timeout settlement, UTF-8 carry, and decoding remain shared for every backend.
    """
    deadline = time.monotonic() + (yield_time_ms / 1000)
    output = bytearray()
    output_closed = False

    try:
        while True:
            if time.monotonic() >= deadline:
                break

            if poll_output is not None:
                await poll_output(deadline)
            await _drain_output_chunks(output_chunks, output_lock, output)

            if time.monotonic() >= deadline:
                break

            if is_done():
                output_closed = True
                if settle_output is not None:
                    await settle_output()
                elif poll_output is not None:
                    await poll_output(deadline)
                await _drain_output_chunks(output_chunks, output_lock, output)
                break

            if should_return is not None and should_return():
                break

            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break

            if wait_for_output is not None:
                await wait_for_output(remaining_s)
            else:
                try:
                    await asyncio.wait_for(output_notify.wait(), timeout=remaining_s)
                except asyncio.TimeoutError:
                    break
                output_notify.clear()

        # Settle bytes that were queued around the final deadline or completion check.
        if settle_output is not None:
            await settle_output()
        elif poll_output is not None:
            await poll_output(deadline)

        if not output_closed and is_done():
            output_closed = True
            if settle_output is not None:
                await settle_output()
            elif poll_output is not None:
                await poll_output(deadline)

        if output_closed:
            await _drain_output_chunks(output_chunks, output_lock, output)
        else:
            await _drain_and_carry_incomplete_suffix(output_chunks, output_lock, output)
    except asyncio.CancelledError:
        restore_task = asyncio.create_task(
            _restore_unreturned_output(output_chunks, output_lock, output)
        )
        while not restore_task.done():
            try:
                await asyncio.shield(restore_task)
            except asyncio.CancelledError:
                continue
        restore_task.result()
        raise

    text = output.decode("utf-8", errors="replace")
    truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
    return truncated.encode("utf-8", errors="replace"), original_token_count, output_closed

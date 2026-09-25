from __future__ import annotations

from collections.abc import AsyncIterable


async def collect_bounded(chunks: AsyncIterable[bytes], max_bytes: int) -> bytes:
    """Collect a prefix; the caller owns and closes the underlying stream."""
    result = bytearray()
    async for chunk in chunks:
        result.extend(chunk[: max_bytes - len(result)])
        if len(result) == max_bytes:
            break
    return bytes(result)

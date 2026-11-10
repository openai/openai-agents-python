"""Async assertions across supported Python versions."""

import asyncio
import sys
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")

if sys.version_info >= (3, 11):
    from asyncio import timeout as timeout
else:
    from async_timeout import timeout as timeout


async def capture_cancellation(
    operation: Awaitable[_T], errors: list[asyncio.CancelledError]
) -> _T:
    # Python 3.10 replaces cancellation at the Task boundary, losing its args.
    try:
        return await operation
    except asyncio.CancelledError as error:
        errors.append(error)
        raise

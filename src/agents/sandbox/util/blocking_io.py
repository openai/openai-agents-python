"""Run unbounded blocking workspace I/O off the event loop without abandoning it.

`asyncio.to_thread()` does not stop its worker when the awaiting task is cancelled, so a
cancelled caller can return while the thread is still writing. Snapshot resume closes the
archive stream and clears the workspace root as soon as its await returns, which would let a
surviving worker extract into a workspace that is being deleted.

Callers therefore keep waiting for the worker even while cancelled, matching the mutation
semantics the session backends already rely on in `agents.memory.sqlite_session`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


async def run_blocking_workspace_io(function: Callable[..., _T], /, *args: Any) -> _T:
    """Run `function` in a worker thread and keep ownership until that worker finishes."""
    task = asyncio.ensure_future(asyncio.to_thread(function, *args))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation from None
        raise
    if cancellation is not None:
        raise cancellation from None
    return result

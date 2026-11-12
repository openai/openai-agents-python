"""An ordered, bounded observer for one admitted managed child turn."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy

from .native_agents import NativeRoleToolInvocation, NativeRoleToolStreamEvent
from .observation import Observation, _observation_sizes

_logger = logging.getLogger("agents.native.managed_tool")
_MAX_RETAINED_BYTES = 32 * 1024 * 1024


class _ManagedToolStream:
    def __init__(
        self,
        invocation: NativeRoleToolInvocation,
        submission_id: str,
        callback: Callable[[NativeRoleToolStreamEvent], None | Awaitable[None]],
        limit: int | None,
        overflow: Callable[[], None],
        *,
        admit: Callable[[], bool] | None = None,
    ) -> None:
        self.invocation = invocation
        self.submission_id = submission_id
        self._callback = callback
        self._limit = limit
        self._overflow = overflow
        self._admit = admit if admit is not None else lambda: True
        self._pending: deque[tuple[Observation, int]] = deque()
        self._retained_bytes = 0
        self._next: asyncio.Future[tuple[Observation, int] | None] | None = (
            asyncio.get_running_loop().create_future()
        )
        self._finished = False
        self.error: BaseException | None = None
        self.task = asyncio.create_task(self._run(self._next))
        self.task.add_done_callback(self._retired)

    def _retired(self, task: asyncio.Task[None]) -> None:
        self._finished = True
        self._pending.clear()
        self._retained_bytes = 0
        self._next = None

    def append(self, event: Observation) -> None:
        if self._finished:
            return
        size = sum(_observation_sizes(event, utf8=True))
        if size > _MAX_RETAINED_BYTES - self._retained_bytes:
            self.error = RuntimeError("Managed role stream retained byte limit exceeded")
            self.abort()
            self._overflow()
            return
        event = deepcopy(event)
        # A waiting worker claims this event immediately. Its handler does not
        # consume one of the pending slots, even before its task resumes.
        if self._next is not None:
            if self._next.done():
                self.abort()
                return
            self._retained_bytes += size
            self._next.set_result((event, size))
            self._next = None
        elif self._limit is not None and len(self._pending) >= self._limit:
            self.error = RuntimeError("Managed role stream pending event limit exceeded")
            self.abort()
            self._overflow()
        else:
            self._retained_bytes += size
            self._pending.append((event, size))

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._next is not None:
            if not self._next.done():
                self._next.set_result(None)
            self._next = None

    def abort(self) -> None:
        self._finished = True
        self._pending.clear()
        self.task.cancel()

    async def _run(self, first: asyncio.Future[tuple[Observation, int] | None]) -> None:
        try:
            entry = await first
            while entry is not None:
                event, size = entry
                if not self._admit():
                    self._pending.clear()
                    return
                try:
                    result = self._callback({"event": event, "invocation": self.invocation.copy()})
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    _logger.error("Managed child stream callback failed")
                finally:
                    self._retained_bytes -= size
                if self._pending:
                    entry = self._pending.popleft()
                elif self._finished:
                    return
                else:
                    self._next = asyncio.get_running_loop().create_future()
                    entry = await self._next
        except asyncio.CancelledError:
            return

    async def drain(self) -> None:
        try:
            await asyncio.shield(self.task)
        except asyncio.CancelledError:
            # Overflow can retire the task before its coroutine starts.
            if not self.task.cancelled() or self.error is None:
                raise
        if self.error is not None:
            raise self.error

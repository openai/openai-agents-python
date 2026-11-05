"""Shared synchronous run ownership and joined asynchronous cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextvars import ContextVar
from threading import RLock
from typing import Any, TypeVar

_ResultT = TypeVar("_ResultT")


class _SyncRun:
    """Own a sync loop until its last deferred Environment can retire."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.deferred = 0
        self.lock = RLock()

    def _close_if_idle(self) -> None:
        if self.deferred:
            return
        try:
            # Match asyncio.run's task, async generator and executor shutdown.
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                for task in pending:
                    if not task.cancelled() and task.exception() is not None:
                        self.loop.call_exception_handler(
                            {
                                "message": "unhandled exception during run_sync shutdown",
                                "exception": task.exception(),
                                "task": task,
                            }
                        )
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
        finally:
            self.loop.close()

    def run(self, operation: Awaitable[_ResultT]) -> _ResultT:
        # Other providers from this sync call may be closed on another thread.
        # Their on-demand recovery must wait until this call stops driving its loop.
        with self.lock:
            token = _sync_run.set(self)
            try:
                asyncio.set_event_loop(self.loop)
                return self.loop.run_until_complete(operation)
            finally:
                _sync_run.reset(token)
                try:
                    self._close_if_idle()
                finally:
                    asyncio.set_event_loop(None)

    def recover(self, operation: Callable[[], Awaitable[None]]) -> None:
        with self.lock:
            # Concurrent closes may already have joined every deferred manager.
            if self.loop.is_closed():
                return
            try:
                asyncio.set_event_loop(self.loop)
                self.loop.run_until_complete(operation())
            finally:
                try:
                    self._close_if_idle()
                finally:
                    asyncio.set_event_loop(None)


_sync_run: ContextVar[_SyncRun | None] = ContextVar("agents_sync_run", default=None)


async def _join(task: asyncio.Future[Any], *, outcome: Future[Any] | None = None) -> Any:
    """Join owned cleanup before propagating cancellation of its passive waiter."""
    cancelled = None
    try:
        while True:
            try:
                value = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                if task.done() and task.cancelled():
                    task.result()
                cancelled = cancelled or error
                if task.done():
                    value = task.result()
                    break
        if outcome is not None:
            value = outcome.result()
    except BaseException as error:
        if cancelled is None:
            raise
        _retain_cleanup_error(cancelled, error, _error_contexts(cancelled.__context__))
        raise cancelled  # noqa: B904 - keep the caller's explicit cause and owned cleanup contexts
    if cancelled is not None:
        raise cancelled
    return value


def _error_contexts(error: BaseException | None) -> tuple[BaseException, ...]:
    contexts: list[BaseException] = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        contexts.append(error)
        error = error.__context__
    return tuple(contexts)


def _retain_cleanup_error(
    primary: BaseException,
    cleanup_error: BaseException,
    previous: tuple[BaseException, ...],
) -> None:
    """Retain each distinct context without replacing explicit causes."""
    contexts: list[BaseException] = []
    seen = {id(primary)}
    for error in _error_contexts(cleanup_error) + _error_contexts(primary.__context__) + previous:
        if id(error) not in seen:
            seen.add(id(error))
            contexts.append(error)
    context: BaseException | None = None
    for error in reversed(contexts):
        error.__context__ = context
        context = error
    primary.__context__ = context


async def _close_after_error(primary: BaseException, close: Callable[[], Awaitable[None]]) -> None:
    """Join close and retain its failures before exception transport can replace context."""
    previous = _error_contexts(primary.__context__)

    async def capture() -> tuple[BaseException, tuple[BaseException, ...]] | None:
        # Eager task factories must also leave the caller's active error handler.
        await asyncio.sleep(0)
        try:
            await close()
        except BaseException as error:  # retain every cleanup failure
            return error, _error_contexts(error.__context__)
        return None

    pending = asyncio.create_task(capture())
    try:
        await _join(pending)
    except asyncio.CancelledError:
        # The first failure wins; _join has finished the owned close task.
        pass
    captured = pending.result()
    if captured is not None:
        cleanup_error, contexts = captured
        _retain_cleanup_error(primary, cleanup_error, contexts + previous)

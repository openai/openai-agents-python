from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import warnings
from contextvars import ContextVar
from typing import Any
from weakref import WeakKeyDictionary

from ..sandbox._cleanup_owner import force_cancel_cleanup_owner

_IS_SYNC_RUN: ContextVar[bool] = ContextVar("agents_is_sync_run", default=False)
_SYNC_BACKGROUND_TASKS: WeakKeyDictionary[asyncio.AbstractEventLoop, set[asyncio.Task[Any]]] = (
    WeakKeyDictionary()
)
_SYNC_LOOP_DRIVERS: WeakKeyDictionary[asyncio.AbstractEventLoop, _SyncLoopDriver] = (
    WeakKeyDictionary()
)
_SYNC_LOOP_LOCAL = threading.local()
_SYNC_DRIVER_LOCK = threading.Lock()
_SYNC_BACKGROUND_SETTLEMENT_TIMEOUT_S = 5.0


class _SyncLoopDriver:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="agents.sync-deferred-cleanup",
            daemon=True,
        )
        self._settlement_task: asyncio.Task[None] | None = None
        self._settlement_future: concurrent.futures.Future[None] | None = None
        self._handoff_requested = False
        self._stop_after_settlement = False
        self._shutdown_asyncgens_started = False
        self._running = False

    def start(self) -> None:
        self.thread.start()
        self.started.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._running = True
        try:
            # Signal readiness from the loop so submitters cannot race run_forever.
            self.loop.call_soon(self.started.set)
            self.loop.run_forever()
        finally:
            self._running = False
            with _SYNC_DRIVER_LOCK:
                if _SYNC_LOOP_DRIVERS.get(self.loop) is self:
                    _SYNC_LOOP_DRIVERS.pop(self.loop, None)
            self.stopped.set()

    def schedule_settlement(self) -> concurrent.futures.Future[None]:
        if self._settlement_future is not None:
            return self._settlement_future

        result: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._settlement_future = result

        def create_settlement_task() -> None:
            if self._settlement_task is not None and not self._settlement_task.done():
                return
            task = self.loop.create_task(
                self._settle_background_work(),
                name="agents.sync_deferred_cleanup_settlement",
            )
            self._settlement_task = task

            def complete(done: asyncio.Task[None]) -> None:
                try:
                    result.set_result(done.result())
                except BaseException as exc:
                    result.set_exception(exc)
                finally:
                    # Complete the handoff only after this callback has observed the task result.
                    # In particular, do not cancel an in-progress async-generator finalizer.
                    if self._running and (
                        self._stop_after_settlement or not self._handoff_requested
                    ):
                        self.loop.stop()

            task.add_done_callback(complete)

        try:
            self.loop.call_soon_threadsafe(create_settlement_task)
        except BaseException as exc:
            result.set_exception(exc)
        return result

    async def _settle_background_work(self) -> None:
        await _settle_pending_sync_background_tasks(self.loop)
        self._shutdown_asyncgens_started = True
        await self.loop.shutdown_asyncgens()

    def stop(self) -> None:
        if not self.thread.is_alive():
            return

        self._handoff_requested = True

        def stop_loop() -> None:
            settlement_task = self._settlement_task
            if settlement_task is not None and not settlement_task.done():
                if self._shutdown_asyncgens_started:
                    # Async-generator ``finally`` blocks can release provider resources. Let the
                    # settlement task finish before handing the loop back to the caller.
                    self._stop_after_settlement = True
                    return
                settlement_task.cancel()
                self._stop_after_settlement = True
                return
            self.loop.stop()

        try:
            self.loop.call_soon_threadsafe(stop_loop)
        except RuntimeError:
            pass
        if threading.current_thread() is not self.thread:
            self.thread.join()


async def _settle_pending_sync_background_tasks(loop: asyncio.AbstractEventLoop) -> None:
    while True:
        tasks = _get_pending_sync_background_tasks(loop)
        if not tasks:
            break
        # asyncio.wait observes completion without cancelling provider cleanup when this
        # settlement task is interrupted to hand the loop back to a synchronous run.
        done, _ = await asyncio.wait(tasks)
        for task in done:
            if not task.cancelled():
                task.exception()
        # Let each task's done callbacks update the registry before checking it again.
        await asyncio.sleep(0)


async def _settle_sync_background_work(loop: asyncio.AbstractEventLoop) -> None:
    """Settle tracked work synchronously when its owner is a caller-provided loop."""

    await _settle_pending_sync_background_tasks(loop)
    await loop.shutdown_asyncgens()


def _force_cancel_sync_background_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Force-cancel cleanup owners when caller-owned loop settlement exceeds its bound."""

    for task in _get_pending_sync_background_tasks(loop):
        force_cancel_cleanup_owner(task)


def _get_default_loop() -> asyncio.AbstractEventLoop | None:
    """Return an existing open policy loop without creating or replacing one."""

    policy = asyncio.get_event_loop_policy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            loop = policy.get_event_loop()
        except RuntimeError:
            return None
    if loop.is_closed():
        return None
    return loop


def _get_sync_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_SYNC_LOOP_LOCAL, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _SYNC_LOOP_LOCAL.loop = loop
    return loop


def _get_pending_sync_background_tasks(
    loop: asyncio.AbstractEventLoop,
) -> tuple[asyncio.Task[Any], ...]:
    with _SYNC_DRIVER_LOCK:
        tasks = _SYNC_BACKGROUND_TASKS.get(loop)
        if not tasks:
            return ()
        completed_tasks = {task for task in tasks if task.done()}
        tasks.difference_update(completed_tasks)
        if not tasks:
            _SYNC_BACKGROUND_TASKS.pop(loop, None)
            return ()
        return tuple(tasks)


def _track_sync_background_task(task: asyncio.Task[Any]) -> None:
    if not _IS_SYNC_RUN.get() or task.done():
        return
    loop = task.get_loop()
    with _SYNC_DRIVER_LOCK:
        tasks = _SYNC_BACKGROUND_TASKS.setdefault(loop, set())
        tasks.add(task)

    def forget(done: asyncio.Task[Any]) -> None:
        loop = done.get_loop()
        with _SYNC_DRIVER_LOCK:
            tasks = _SYNC_BACKGROUND_TASKS.get(loop)
            if tasks is None:
                return
            tasks.discard(done)
            if not tasks:
                _SYNC_BACKGROUND_TASKS.pop(loop, None)

    task.add_done_callback(forget)


def _stop_sync_loop_driver(loop: asyncio.AbstractEventLoop) -> None:
    with _SYNC_DRIVER_LOCK:
        driver = _SYNC_LOOP_DRIVERS.get(loop)
    if driver is not None:
        driver.stop()


def _start_sync_loop_driver(loop: asyncio.AbstractEventLoop) -> _SyncLoopDriver:
    _get_pending_sync_background_tasks(loop)
    with _SYNC_DRIVER_LOCK:
        driver = _SYNC_LOOP_DRIVERS.get(loop)
        if driver is not None and driver.thread.is_alive():
            return driver
        driver = _SyncLoopDriver(loop)
        _SYNC_LOOP_DRIVERS[loop] = driver
    driver.start()
    return driver


def _create_sync_task(
    loop: asyncio.AbstractEventLoop,
    coroutine: Any,
) -> asyncio.Task[Any]:
    """Create a sync-run task on Python 3.10 and newer."""

    if sys.version_info >= (3, 11):
        return loop.create_task(coroutine, context=None)
    return loop.create_task(coroutine)

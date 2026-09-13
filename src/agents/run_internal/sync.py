from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from typing import Any
from weakref import WeakKeyDictionary

_IS_SYNC_RUN: ContextVar[bool] = ContextVar("agents_is_sync_run", default=False)
_SYNC_BACKGROUND_TASKS: WeakKeyDictionary[asyncio.AbstractEventLoop, set[asyncio.Task[Any]]] = (
    WeakKeyDictionary()
)
_SYNC_LOOP_DRIVERS: WeakKeyDictionary[asyncio.AbstractEventLoop, _SyncLoopDriver] = (
    WeakKeyDictionary()
)
_SYNC_DRIVER_LOCK = threading.Lock()


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

    def start(self) -> None:
        self.thread.start()
        self.started.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.started.set()
        try:
            self.loop.run_forever()
        finally:
            with _SYNC_DRIVER_LOCK:
                if _SYNC_LOOP_DRIVERS.get(self.loop) is self:
                    _SYNC_LOOP_DRIVERS.pop(self.loop, None)
            self.stopped.set()

    def stop(self) -> None:
        if not self.thread.is_alive():
            return
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            # The loop may have been closed by its owner while the driver was
            # exiting. The driver thread will observe the closed loop and stop.
            pass
        if threading.current_thread() is not self.thread:
            self.thread.join()


def _track_sync_background_task(task: asyncio.Task[Any]) -> None:
    if not _IS_SYNC_RUN.get():
        return
    loop = task.get_loop()
    with _SYNC_DRIVER_LOCK:
        tasks = _SYNC_BACKGROUND_TASKS.setdefault(loop, set())
        tasks.add(task)

    def forget(done: asyncio.Task[Any]) -> None:
        with _SYNC_DRIVER_LOCK:
            tasks.discard(done)
            should_stop = not tasks and _SYNC_LOOP_DRIVERS.get(loop) is not None
        if should_stop:
            loop.stop()

    task.add_done_callback(forget)


def _stop_sync_loop_driver(loop: asyncio.AbstractEventLoop) -> None:
    with _SYNC_DRIVER_LOCK:
        driver = _SYNC_LOOP_DRIVERS.get(loop)
    if driver is not None:
        driver.stop()
        with _SYNC_DRIVER_LOCK:
            if not driver.thread.is_alive():
                _SYNC_LOOP_DRIVERS.pop(loop, None)


def _start_sync_loop_driver(loop: asyncio.AbstractEventLoop) -> None:
    with _SYNC_DRIVER_LOCK:
        tasks = _SYNC_BACKGROUND_TASKS.get(loop)
        if not tasks or not any(not task.done() for task in tasks):
            return
        driver = _SYNC_LOOP_DRIVERS.get(loop)
        if driver is not None and driver.thread.is_alive():
            return
        driver = _SyncLoopDriver(loop)
        _SYNC_LOOP_DRIVERS[loop] = driver
    driver.start()

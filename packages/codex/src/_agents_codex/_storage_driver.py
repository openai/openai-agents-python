"""Run async canonical storage driver commands on the application's loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any


class _StorageDriver:
    def __init__(self, *, asynchronous: bool, timeout: float) -> None:
        self._loop = asyncio.get_running_loop() if asynchronous else None
        self._timeout = timeout

    def require_worker(self) -> None:
        if self._loop is None:
            return
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if current_loop is self._loop:
            raise RuntimeError(
                "Async storage client operations must run off the client's event loop"
            )

    def call(self, operation: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        loop = self._loop
        if loop is None:
            return operation(*args, **kwargs)
        self.require_worker()
        if not loop.is_running():
            raise RuntimeError("The async storage client's event loop is no longer running")

        async def invoke() -> Any:
            return await operation(*args, **kwargs)

        command = invoke()
        try:
            result = asyncio.run_coroutine_threadsafe(command, loop)
        except BaseException:
            command.close()
            raise
        try:
            return result.result(timeout=self._timeout)
        except FutureTimeout:
            result.cancel()
            raise

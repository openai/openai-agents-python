"""Cancel SDK-owned Python tasks once on every supported Python version."""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from weakref import WeakSet

__all__ = ("cancel_once",)

if sys.version_info >= (3, 11):

    def cancel_once(task: asyncio.Task[Any]) -> None:
        if not task.cancelling():
            task.cancel()

else:
    _cancelled_by_sdk: WeakSet[asyncio.Task[Any]] = WeakSet()

    def cancel_once(task: asyncio.Task[Any]) -> None:
        if task not in _cancelled_by_sdk and task.cancel():
            _cancelled_by_sdk.add(task)

"""Cancel SDK-owned Python tasks once on every supported Python version."""

from __future__ import annotations

import asyncio as asyncio
import sys as sys
from typing import Any as Any
from weakref import WeakSet as WeakSet

from ._public import preserve_module_identity as _preserve_module_identity

# Preserve SDK cancellation if a callback suppresses it or calls uncancel().
_cancelled_by_sdk: WeakSet[asyncio.Task[Any]] = WeakSet()

if sys.version_info >= (3, 11):

    def cancellation_requested(task: asyncio.Task[Any]) -> bool:
        return task in _cancelled_by_sdk or bool(task.cancelling())

    def cancel_once(task: asyncio.Task[Any]) -> None:
        if task.cancelling() or task.cancel():
            _cancelled_by_sdk.add(task)

else:

    def cancellation_requested(task: asyncio.Task[Any]) -> bool:
        return task in _cancelled_by_sdk

    def cancel_once(task: asyncio.Task[Any]) -> None:
        if task not in _cancelled_by_sdk and task.cancel():
            _cancelled_by_sdk.add(task)


_preserve_module_identity(globals(), "agents._compat")

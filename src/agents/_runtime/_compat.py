"""Cancel SDK-owned Python tasks once on every supported Python version."""

from __future__ import annotations

import asyncio as asyncio
import sys as sys
from typing import Any as Any
from weakref import WeakSet as WeakSet

from ._public import preserve_module_identity as _preserve_module_identity

if sys.version_info >= (3, 11):

    def cancel_once(task: asyncio.Task[Any]) -> None:
        if not task.cancelling():
            task.cancel()

else:
    _cancelled_by_sdk: WeakSet[asyncio.Task[Any]] = WeakSet()

    def cancel_once(task: asyncio.Task[Any]) -> None:
        if task not in _cancelled_by_sdk and task.cancel():
            _cancelled_by_sdk.add(task)


_preserve_module_identity(globals(), "agents._compat")

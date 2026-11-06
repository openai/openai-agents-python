"""Cancel SDK-owned Python tasks once on every supported Python version."""

from __future__ import annotations

from ._runtime._compat import (
    Any as Any,
    WeakSet as WeakSet,
    asyncio as asyncio,
    cancel_once as cancel_once,
    sys as sys,
)

__all__ = ("cancel_once",)

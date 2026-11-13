"""Private routing hints for provider prompt caches."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import Any, Literal

_SAVED_KEY = re.compile(r"agents-sdk:(?:conversation|group):[0-9a-f]{32}")
_opening: ContextVar[tuple[object, asyncio.Task[Any] | None, str] | None] = ContextVar(
    "_sdk_prompt_cache_opening", default=None
)


def _cache_key(kind: Literal["conversation", "group"], identity: str | None) -> str | None:
    if not isinstance(identity, str) or not (identity := identity.strip()):
        return None
    return f"agents-sdk:{kind}:{sha256(identity.encode()).hexdigest()[:32]}"


def _saved_cache_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAVED_KEY.fullmatch(value):
        raise ValueError("Invalid saved prompt cache affinity")
    return value


@asynccontextmanager
async def _cache_affinity_opening(agent: object, key: str | None) -> AsyncIterator[None]:
    if key is None:
        yield
        return
    token = _opening.set((agent, asyncio.current_task(), key))
    try:
        yield
    finally:
        _opening.reset(token)


def _claim_cache_affinity(agent: object) -> str | None:
    selected = _opening.get()
    if selected is None or selected[0] is not agent or selected[1] is not asyncio.current_task():
        return None
    _opening.set(None)
    return selected[2]

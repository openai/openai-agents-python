"""Test checks on owned stop cleanup."""

from __future__ import annotations

import asyncio
from typing import Any

from agents import Session


async def join_interrupt_cleanup(session: Session[Any, Any]) -> None:
    """Observe accepted cleanup owners before testing reuse; this is not a public wait API."""
    pending = (*session._root_interrupts, *session._graph_interrupts)
    if pending:
        # Wait for done callbacks to release admission gates, even for completed tasks.
        await asyncio.wait(pending)
        for task in pending:
            task.result()

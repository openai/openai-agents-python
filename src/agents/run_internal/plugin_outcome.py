"""Observe one public submission without retaining its inputs or results."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from ..logger import logger
from ..plugins import RunOutcome

if TYPE_CHECKING:
    from ..agent import Agent


class Submission:
    """A snapshot of the starting agent's callbacks, scoped to this invocation."""

    def __init__(self, callbacks: tuple[Callable[..., Any], ...]) -> None:
        self.callbacks = callbacks
        self.started = time.monotonic()

    async def finish(self, status: Literal["completed", "paused", "failed", "cancelled"]) -> None:
        # Claim delivery before awaiting any callback, including on a second stream drain.
        callbacks, self.callbacks = self.callbacks, ()
        if not callbacks:
            return
        receipt = RunOutcome(status, time.monotonic() - self.started)
        for callback in callbacks:
            try:
                value = callback(receipt)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                # Never include the observer's exception, which may contain credentials.
                logger.warning("plugin_observer_failed on_run_end")


def submission(agent: Agent[Any]) -> Submission | None:
    """Keep the default Runner free of clocks and callback state."""
    if not agent.plugins:
        return None
    callbacks = tuple(
        plugin.hooks.on_run_end
        for plugin in agent.plugins
        if plugin.hooks is not None and plugin.hooks.on_run_end is not None
    )
    return Submission(callbacks) if callbacks else None


def failure_status(error: BaseException) -> Literal["cancelled", "failed"]:
    return "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "failed"

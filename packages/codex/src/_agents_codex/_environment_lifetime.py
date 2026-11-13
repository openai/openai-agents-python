"""Task-affine Environment acquisition and joined retirement ownership."""

from __future__ import annotations

import asyncio
import sys
from contextlib import AbstractAsyncContextManager
from contextvars import Context, copy_context
from typing import TYPE_CHECKING, Any

from ._compat import cancel_once
from ._lifecycle import _close_after_error, _join, _SyncRun
from ._managed_workspace import _SANDBOX_EVENTS_ATTRIBUTE, _WORKSPACE_INSTRUCTIONS_ATTRIBUTE
from .executor import Environment
from .sandbox_memory import _MEMORY_OWNER_ATTRIBUTE, _SandboxMemoryOwner
from .telemetry import _create_task

if TYPE_CHECKING:
    from ._sandbox_events import _OperationObserver


class _EnvironmentLease:
    """One task holds the supplied manager from entry until native retirement."""

    def __init__(self, source: AbstractAsyncContextManager[Environment[Any]]) -> None:
        loop = asyncio.get_running_loop()
        self._ready: asyncio.Future[Environment[Any]] = loop.create_future()
        self._retired: asyncio.Future[None] = loop.create_future()
        self.source = source
        self._borrower = asyncio.current_task()
        self._sync_run: _SyncRun | None = None
        self._woke_borrower = False
        self.interrupted = False
        self._workspace_instructions: str | None = None
        self._sandbox_events: _OperationObserver | None = None
        self._sandbox_memory: _SandboxMemoryOwner | None = None
        self._sandbox_memory_scope: Context | None = None
        self._owner = _create_task(self._hold(source))

    async def _hold(self, source: AbstractAsyncContextManager[Environment[Any]]) -> None:
        # Manager timeouts and task groups cancel this task, distinct from
        # cancellation of the borrowing application. Entry and exit stay together.
        async with source as environment:
            if self._ready.cancelled():
                return
            instructions = getattr(source, _WORKSPACE_INSTRUCTIONS_ATTRIBUTE, None)
            if instructions is not None and not isinstance(instructions, str):
                raise TypeError("Workspace instructions must be captured text")
            self._workspace_instructions = instructions
            observer = getattr(source, _SANDBOX_EVENTS_ATTRIBUTE, None)
            if observer is not None:
                from ._sandbox_events import _OperationObserver

                if not isinstance(observer, _OperationObserver):
                    raise TypeError(
                        "Native sandbox observations require a sandbox_environment source"
                    )
            self._sandbox_events = observer
            memory = getattr(source, _MEMORY_OWNER_ATTRIBUTE, None)
            if memory is not None:
                if not isinstance(memory, _SandboxMemoryOwner):
                    raise TypeError("Sandbox memory requires a native sandbox acquisition")
                self._sandbox_memory = memory
                self._sandbox_memory_scope = copy_context()
            self._ready.set_result(environment)
            try:
                await asyncio.shield(self._retired)
            except BaseException as error:
                # A Source ending early must not release an executor in native use.
                # Wake only its own borrower and wait for the existing opening/close
                # path to join native work. Never join the Source from native close.
                self.interrupted = True
                if self._borrower is not None:
                    self._woke_borrower = self._borrower.cancel(self)
                await _close_after_error(error, lambda: _join(self._retired))
                raise

    async def acquire(self) -> Environment[Any]:
        try:
            await asyncio.wait((self._owner, self._ready), return_when=asyncio.FIRST_COMPLETED)
            if self._owner.done():
                await self._owner
            return self._ready.result()
        except BaseException as error:
            # Before successful entry, the source owns rollback. Cancellation must
            # join that entry and must never start native work from a late result.
            if not self._ready.done():
                self._ready.cancel()
                cancel_once(self._owner)
            await _close_after_error(error, lambda: _release_environment(self))
            self.raise_if_interrupted(error)
            raise

    def raise_if_interrupted(self, error: BaseException) -> None:
        if self.interrupted and isinstance(error, asyncio.CancelledError) and error.args == (self,):
            if error.__context__ is not None:
                raise error.__context__ from None
            raise RuntimeError("Environment lease ended before Session retirement") from None

    def detach_borrower(self) -> None:
        if self._woke_borrower:
            self._woke_borrower = False
            if sys.version_info >= (3, 11):
                assert self._borrower is not None
                self._borrower.uncancel()
        self._borrower = None


async def _release_environment(lease: _EnvironmentLease) -> None:
    # Only signal after native retirement; the Source owns normal exception policy
    # inside its manager. It cannot suppress an error in the separate Session body.
    if not lease._owner.done() and lease._owner.get_loop() is not asyncio.get_running_loop():
        raise RuntimeError("Close the retained Environment on the event loop where it opened")
    if not lease._retired.done():
        lease._retired.set_result(None)
    try:
        await _join(lease._owner)
    finally:
        lease.detach_borrower()

from __future__ import annotations

import asyncio

import pytest

from agents.sandbox.session.base_sandbox_session import BaseSandboxSession


class _Session(BaseSandboxSession):
    async def _exec_internal(self, *command: str, timeout: float | None = None):
        raise NotImplementedError

    async def hydrate_workspace(self, *args, **kwargs):
        raise NotImplementedError

    async def persist_workspace(self, *args, **kwargs):
        raise NotImplementedError

    async def read(self, *args, **kwargs):
        raise NotImplementedError

    async def running(self):
        raise NotImplementedError

    async def write(self, *args, **kwargs):
        raise NotImplementedError


def _session() -> BaseSandboxSession:
    return _Session()


@pytest.mark.asyncio
async def test_pty_cleanup_completes_before_propagating_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = False

    async def cleanup() -> None:
        nonlocal completed
        started.set()
        await release.wait()
        completed = True

    task = asyncio.create_task(_session()._settle_pty_cleanup(cleanup()))
    await started.wait()
    task.cancel()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed


@pytest.mark.asyncio
async def test_pty_cleanup_preserves_cleanup_exception() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("cleanup failed")

    task = asyncio.create_task(_session()._settle_pty_cleanup(cleanup()))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await task

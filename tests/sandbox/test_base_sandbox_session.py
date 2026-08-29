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
async def test_pty_cleanup_preserves_cancellation_reason() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(_session()._settle_pty_cleanup(cleanup()))
    await started.wait()
    task.cancel("caller stopped cleanup")
    release.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task
    assert exc_info.value.args == ("caller stopped cleanup",)


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


@pytest.mark.asyncio
async def test_pty_cleanup_settles_a_sequential_batch() -> None:
    started: list[int] = []
    release = asyncio.Event()
    completed: list[int] = []

    async def cleanup_all() -> None:
        for entry in (1, 2):
            started.append(entry)
            await release.wait()
            completed.append(entry)
            release.clear()
            if entry == 1:
                release.set()

    task = asyncio.create_task(_session()._settle_pty_cleanup(cleanup_all()))
    while started != [1]:
        await asyncio.sleep(0)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert started == [1, 2]
    assert completed == [1, 2]

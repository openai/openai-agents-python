from __future__ import annotations

import asyncio
import sys
from contextlib import suppress

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
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel("cleanup requested")
        task.cancel("cleanup requested again")
        release.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task
        assert completed
        if sys.version_info >= (3, 11):
            assert exc_info.value.args == ("cleanup requested",)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task


@pytest.mark.asyncio
async def test_pty_cleanup_preserves_cancellation_reason() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(_session()._settle_pty_cleanup(cleanup()))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel("caller stopped cleanup")
        release.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task
        if sys.version_info >= (3, 11):
            assert exc_info.value.args == ("caller stopped cleanup",)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task


@pytest.mark.asyncio
async def test_pty_cleanup_preserves_cleanup_exception() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("cleanup failed")

    task = asyncio.create_task(_session()._settle_pty_cleanup(cleanup()))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        release.set()

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with suppress(BaseException):
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
    try:
        async def wait_for_first_entry() -> None:
            while started != [1]:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_first_entry(), timeout=5)
        task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert started == [1, 2]
        assert completed == [1, 2]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task

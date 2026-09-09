from __future__ import annotations

import asyncio
import inspect
import sys
from contextlib import suppress
from types import SimpleNamespace

import pytest

from agents.sandbox.manifest import Manifest
from agents.sandbox.session import base_sandbox_session
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
async def test_pty_cleanup_timeout_preserves_cancellation_and_owned_task() -> None:
    session = _session()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()
        completed.set()

    task = asyncio.create_task(session._settle_pty_cleanup(cleanup(), timeout=0.01))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel("caller stopped cleanup")

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task
        if sys.version_info >= (3, 11):
            assert exc_info.value.args == ("caller stopped cleanup",)
        assert not completed.is_set()
        assert session._pty_cleanup_tasks

        release.set()
        await asyncio.wait_for(completed.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert session._pty_cleanup_tasks == set()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task


@pytest.mark.asyncio
async def test_pty_cleanup_can_detach_after_timeout() -> None:
    session = _session()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()
        completed.set()

    task = asyncio.create_task(
        session._settle_pty_cleanup(cleanup(), timeout=0.01, propagate_timeout=False)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(task, timeout=0.5)
        assert not completed.is_set()
        assert session._pty_cleanup_tasks

        release.set()
        await asyncio.wait_for(completed.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert session._pty_cleanup_tasks == set()
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


@pytest.mark.asyncio
async def test_pty_cleanup_attempts_remaining_entries_after_failure() -> None:
    attempted: list[int] = []

    async def cleanup(entry: int) -> None:
        attempted.append(entry)
        if entry == 1:
            raise RuntimeError("first cleanup failed")

    with pytest.raises(RuntimeError, match="first cleanup failed"):
        await _session()._cleanup_pty_entries((1, 2), cleanup)

    assert attempted == [1, 2]


@pytest.mark.asyncio
async def test_pty_cleanup_raises_first_entry_error_deterministically() -> None:
    second_failed = asyncio.Event()
    release_first = asyncio.Event()

    async def cleanup(entry: int) -> None:
        if entry == 1:
            await release_first.wait()
        else:
            second_failed.set()
        raise RuntimeError(f"cleanup {entry} failed")

    task = asyncio.create_task(_session()._cleanup_pty_entries((1, 2), cleanup))
    try:
        await asyncio.wait_for(second_failed.wait(), timeout=0.5)
        assert not task.done()
        release_first.set()

        with pytest.raises(RuntimeError, match="cleanup 1 failed"):
            await task
    finally:
        release_first.set()
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task


@pytest.mark.asyncio
async def test_pty_cleanup_batch_uses_one_deadline_and_starts_every_entry() -> None:
    session = _session()
    started: list[int] = []
    release = asyncio.Event()
    completed: list[int] = []

    async def cleanup(entry: int) -> None:
        started.append(entry)
        await release.wait()
        completed.append(entry)

    batch = asyncio.create_task(session._cleanup_pty_entries((1, 2), cleanup, timeout=0.01))
    try:

        async def wait_for_all_started() -> None:
            while started != [1, 2]:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_all_started(), timeout=0.5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(batch, timeout=0.5)
        assert completed == []

        release.set()
        cleanup_tasks = tuple(session._pty_cleanup_tasks or ())
        await asyncio.wait_for(asyncio.gather(*cleanup_tasks), timeout=0.5)
    finally:
        release.set()
        if not batch.done():
            batch.cancel()
        with suppress(BaseException):
            await batch
        cleanup_tasks = tuple(session._pty_cleanup_tasks or ())
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    assert completed == [1, 2]


@pytest.mark.asyncio
async def test_pty_start_rollback_removes_and_terminates_exact_entry() -> None:
    session = _session()
    entry = object()
    session._pty_lock = asyncio.Lock()
    session._pty_processes = {7: entry}
    session._reserved_pty_process_ids = {7}
    terminated = False

    async def terminate() -> None:
        nonlocal terminated
        terminated = True

    await session._rollback_pty_start(7, entry, session._pty_processes, terminate)

    assert session._pty_processes == {}
    assert session._reserved_pty_process_ids == set()
    assert terminated


@pytest.mark.asyncio
async def test_pty_start_rollback_accepts_provider_session_registry() -> None:
    session = _session()
    entry = object()
    registry = {7: entry}
    session._pty_lock = asyncio.Lock()
    session._reserved_pty_process_ids = {7}
    terminated = False

    async def terminate() -> None:
        nonlocal terminated
        terminated = True

    await session._rollback_pty_start(7, entry, registry, terminate)

    assert registry == {}
    assert session._reserved_pty_process_ids == set()
    assert terminated


@pytest.mark.asyncio
async def test_pty_start_rollback_settles_registry_removal_before_cancellation() -> None:
    session = _session()
    entry = object()
    registry = {7: entry}
    session._pty_lock = asyncio.Lock()
    session._reserved_pty_process_ids = {7}
    await session._pty_lock.acquire()
    terminated = False

    async def terminate() -> None:
        nonlocal terminated
        terminated = True

    task = asyncio.create_task(session._rollback_pty_start(7, entry, registry, terminate))
    await asyncio.sleep(0)
    task.cancel("rollback cancelled")
    session._pty_lock.release()

    await task
    assert registry == {}
    assert session._reserved_pty_process_ids == set()
    assert terminated


@pytest.mark.asyncio
async def test_stop_persists_snapshot_after_cleanup_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.state = SimpleNamespace(manifest=Manifest(), type="test")
    monkeypatch.setattr(
        base_sandbox_session,
        "validate_manifest_mount_credential_boundaries",
        lambda *args, **kwargs: None,
    )
    persisted = False
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def before_stop() -> None:
        raise asyncio.CancelledError("cleanup cancelled")

    async def persist_snapshot() -> None:
        nonlocal persisted
        snapshot_started.set()
        await release_snapshot.wait()
        persisted = True

    session._before_stop = before_stop
    session._persist_snapshot = persist_snapshot

    stop_task = asyncio.create_task(session.stop())
    await asyncio.wait_for(snapshot_started.wait(), timeout=0.5)
    stop_task.cancel("second cleanup cancellation")
    await asyncio.sleep(0)
    assert not stop_task.done()
    release_snapshot.set()

    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert persisted


@pytest.mark.asyncio
async def test_stop_preserves_original_cleanup_failure_when_snapshot_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.state = SimpleNamespace(manifest=Manifest(), type="test")
    monkeypatch.setattr(
        base_sandbox_session,
        "validate_manifest_mount_credential_boundaries",
        lambda *args, **kwargs: None,
    )

    async def before_stop() -> None:
        raise RuntimeError("pty cleanup failed")

    async def persist_snapshot() -> None:
        raise ValueError("snapshot failed")

    session._before_stop = before_stop
    session._persist_snapshot = persist_snapshot

    with pytest.raises(RuntimeError) as exc_info:
        await inspect.unwrap(BaseSandboxSession.stop)(session)

    assert str(exc_info.value) == "pty cleanup failed"
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert session._should_preserve_backend_on_cleanup()


@pytest.mark.asyncio
async def test_stop_wraps_cleanup_failure_when_snapshot_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.state = SimpleNamespace(manifest=Manifest(), type="test")
    monkeypatch.setattr(
        base_sandbox_session,
        "validate_manifest_mount_credential_boundaries",
        lambda *args, **kwargs: None,
    )
    source_error = RuntimeError("cleanup failed")
    snapshot_error = ValueError("snapshot failed")

    async def before_stop() -> None:
        raise source_error

    async def persist_snapshot() -> None:
        raise snapshot_error

    session._before_stop = before_stop
    session._persist_snapshot = persist_snapshot
    session._wrap_stop_error = lambda error: RuntimeError("wrapped cleanup failed")

    with pytest.raises(RuntimeError, match="wrapped cleanup failed") as exc_info:
        await inspect.unwrap(BaseSandboxSession.stop)(session)

    assert exc_info.value.__cause__ is snapshot_error
    assert session._should_preserve_backend_on_cleanup()

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
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


def test_asyncio_run_shutdown_has_bounded_cleanup_owner() -> None:
    session = _session()
    started = threading.Event()
    finalized = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    gate_holder: dict[str, asyncio.Event] = {}

    async def run() -> None:
        gate = asyncio.Event()
        loop_holder["loop"] = asyncio.get_running_loop()
        gate_holder["gate"] = gate

        async def cleanup() -> None:
            started.set()
            try:
                await gate.wait()
            finally:
                finalized.set()

        with suppress(asyncio.TimeoutError):
            await session._settle_pty_cleanup(cleanup(), timeout=0.01)

    def run_on_own_loop() -> None:
        try:
            asyncio.run(run())
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run_on_own_loop, daemon=True)
    thread.start()
    try:
        assert started.wait(1), "cleanup did not start"
        assert finished.wait(1), "asyncio.run did not bound stalled cleanup shutdown"
        assert finalized.is_set()
        assert not failures
    finally:
        loop = loop_holder.get("loop")
        gate = gate_holder.get("gate")
        if loop is not None and gate is not None and not loop.is_closed():
            loop.call_soon_threadsafe(gate.set)
        thread.join(timeout=2)


def test_asyncio_run_propagates_forced_shutdown_from_deferred_cleanup_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_cleanup_owner = base_sandbox_session.create_cleanup_owner

    def create_fast_cleanup_owner(awaitable, *, name, cancel_grace_s=0.1):
        return create_cleanup_owner(awaitable, name=name, cancel_grace_s=0.1)

    monkeypatch.setattr(base_sandbox_session, "create_cleanup_owner", create_fast_cleanup_owner)
    session = _session()
    started = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    async def run() -> None:
        session._pty_cleanup_tasks = {asyncio.get_running_loop().create_future()}
        session._schedule_deferred_dependency_close()
        started.set()

    def run_on_own_loop() -> None:
        try:
            asyncio.run(run())
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run_on_own_loop, daemon=True)
    thread.start()
    assert started.wait(1), "deferred cleanup did not start"
    assert finished.wait(1), "asyncio.run did not propagate forced shutdown"
    assert not failures
    thread.join(timeout=1)


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
async def test_pty_cleanup_batch_preserves_caller_cancellation_after_deadline() -> None:
    session = _session()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def cleanup(_entry: int) -> None:
        started.set()
        await release.wait()
        completed.set()

    batch = asyncio.create_task(session._cleanup_pty_entries((1,), cleanup, timeout=0.01))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.5)
        batch.cancel("caller stopped PTY cleanup")

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await batch
        if sys.version_info >= (3, 11):
            assert exc_info.value.args == ("caller stopped PTY cleanup",)
        assert session._pty_cleanup_tasks
        assert not completed.is_set()

        release.set()
        cleanup_tasks = tuple(session._pty_cleanup_tasks or ())
        await asyncio.wait_for(asyncio.gather(*cleanup_tasks), timeout=0.5)
        await asyncio.sleep(0)
        assert completed.is_set()
        assert session._pty_cleanup_tasks == set()
    finally:
        release.set()
        if not batch.done():
            batch.cancel()
        with suppress(BaseException):
            await batch
        cleanup_tasks = tuple(session._pty_cleanup_tasks or ())
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_does_not_snapshot_while_pty_cleanup_is_still_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.state = SimpleNamespace(manifest=Manifest(), type="test")
    monkeypatch.setattr(
        base_sandbox_session,
        "validate_manifest_mount_credential_boundaries",
        lambda *args, **kwargs: None,
    )
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    snapshot_started = asyncio.Event()

    async def pending_cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    async def before_stop() -> None:
        session._track_pty_cleanup_task(asyncio.create_task(pending_cleanup()))
        await cleanup_started.wait()
        raise RuntimeError("pty cleanup timed out")

    async def persist_snapshot() -> None:
        snapshot_started.set()

    session._before_stop = before_stop
    session._persist_snapshot = persist_snapshot

    try:
        with pytest.raises(RuntimeError, match="pty cleanup timed out"):
            await inspect.unwrap(BaseSandboxSession.stop)(session)

        assert not snapshot_started.is_set()
        assert session._should_preserve_backend_on_cleanup()
    finally:
        release_cleanup.set()
        cleanup_tasks = tuple(session._pty_cleanup_tasks or ())
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_aclose_defers_shutdown_until_fallback_snapshot_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ShutdownProbeSession(_Session):
        def __init__(self) -> None:
            self.state = SimpleNamespace(manifest=Manifest(), type="test")
            self.shutdown_started = asyncio.Event()
            self.release_snapshot = asyncio.Event()
            self.shutdown_calls = 0

        def _pty_cleanup_timeout_s(self) -> float:
            return 0.01

        async def _before_stop(self) -> None:
            raise RuntimeError("stop cleanup failed")

        async def stop(self) -> None:
            await inspect.unwrap(BaseSandboxSession.stop)(self)

        async def _persist_snapshot(self) -> None:
            await self.release_snapshot.wait()

        async def _shutdown_backend(self) -> None:
            self.shutdown_calls += 1
            self.shutdown_started.set()

    monkeypatch.setattr(
        base_sandbox_session,
        "validate_manifest_mount_credential_boundaries",
        lambda *args, **kwargs: None,
    )
    session = ShutdownProbeSession()

    with pytest.raises(RuntimeError, match="stop cleanup failed"):
        await inspect.unwrap(BaseSandboxSession.aclose)(session)

    assert session.shutdown_calls == 0
    deferred_task = session._deferred_dependency_close_task
    assert deferred_task is not None

    session.release_snapshot.set()
    await asyncio.wait_for(deferred_task, timeout=0.5)
    assert session.shutdown_calls == 1


@pytest.mark.asyncio
async def test_aclose_retry_joins_detached_snapshot_before_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetrySnapshotSession(_Session):
        def __init__(self) -> None:
            self.state = SimpleNamespace(manifest=Manifest(), type="test")
            self.snapshot_started = asyncio.Event()
            self.release_snapshot = asyncio.Event()
            self.snapshot_calls = 0
            self.shutdown_calls = 0
            self.deferred_cleanup_started = asyncio.Event()
            self.shutdown_started = asyncio.Event()
            self.release_shutdown = asyncio.Event()
            self.retry_stop_finished = asyncio.Event()

        def _pty_cleanup_timeout_s(self) -> float:
            return 0.01

        async def _before_stop(self) -> None:
            if self.snapshot_calls == 0:
                raise RuntimeError("stop cleanup failed")

        async def stop(self) -> None:
            await inspect.unwrap(BaseSandboxSession.stop)(self)
            self.retry_stop_finished.set()

        async def _persist_snapshot(self) -> None:
            self.snapshot_calls += 1
            self.snapshot_started.set()
            await self.release_snapshot.wait()

        async def _wait_for_tracked_cleanup_tasks(
            self, *, timeout: float | None = None
        ) -> tuple[asyncio.CancelledError | None, bool]:
            self.deferred_cleanup_started.set()
            return await super()._wait_for_tracked_cleanup_tasks(timeout=timeout)

        async def _shutdown_backend(self) -> None:
            self.shutdown_calls += 1
            self.shutdown_started.set()
            await self.release_shutdown.wait()

    session = RetrySnapshotSession()
    monkeypatch.setattr(
        base_sandbox_session,
        "validate_manifest_mount_credential_boundaries",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(RuntimeError, match="stop cleanup failed"):
        await inspect.unwrap(BaseSandboxSession.aclose)(session)
    await asyncio.wait_for(session.snapshot_started.wait(), timeout=0.5)
    await asyncio.wait_for(session.deferred_cleanup_started.wait(), timeout=0.5)

    retry = asyncio.create_task(inspect.unwrap(BaseSandboxSession.aclose)(session))
    await asyncio.sleep(0)
    assert not retry.done()
    assert session.snapshot_calls == 1

    session.release_snapshot.set()
    await asyncio.wait_for(session.shutdown_started.wait(), timeout=0.5)
    await asyncio.wait_for(session.retry_stop_finished.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert session.shutdown_calls == 1

    session.release_shutdown.set()
    await asyncio.wait_for(retry, timeout=0.5)

    assert session.snapshot_calls == 1
    assert session.shutdown_calls == 1


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
async def test_stop_prioritizes_caller_cancellation_after_snapshot_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.state = SimpleNamespace(manifest=Manifest(), type="test")
    monkeypatch.setattr(
        base_sandbox_session,
        "validate_manifest_mount_credential_boundaries",
        lambda *args, **kwargs: None,
    )
    cleanup_error = RuntimeError("pty cleanup failed")
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def before_stop() -> None:
        raise cleanup_error

    async def persist_snapshot() -> None:
        snapshot_started.set()
        await release_snapshot.wait()

    session._before_stop = before_stop
    session._persist_snapshot = persist_snapshot

    stop_task = asyncio.create_task(inspect.unwrap(BaseSandboxSession.stop)(session))
    await asyncio.wait_for(snapshot_started.wait(), timeout=0.5)
    stop_task.cancel("caller cancellation")
    await asyncio.sleep(0)
    assert not stop_task.done()
    release_snapshot.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await stop_task

    assert exc_info.value.__cause__ is cleanup_error


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

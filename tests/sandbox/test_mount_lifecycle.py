from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from agents.sandbox.errors import WorkspaceArchiveReadError
from agents.sandbox.session.mount_lifecycle import with_ephemeral_mounts_removed


class _FakeMountStrategy:
    def __init__(
        self,
        events: list[str],
        *,
        name: str,
        fail_teardown: bool = False,
        fail_restore: bool = False,
    ) -> None:
        self._events = events
        self._name = name
        self._fail_teardown = fail_teardown
        self._fail_restore = fail_restore

    async def teardown_for_snapshot(
        self,
        mount: object,
        session: object,
        path: Path,
    ) -> None:
        _ = (mount, session, path)
        self._events.append(f"teardown:{self._name}")
        if self._fail_teardown:
            raise RuntimeError(f"teardown failed: {self._name}")

    async def restore_after_snapshot(
        self,
        mount: object,
        session: object,
        path: Path,
    ) -> None:
        _ = (mount, session, path)
        self._events.append(f"restore:{self._name}")
        if self._fail_restore:
            raise RuntimeError(f"restore failed: {self._name}")


class _FakeMount:
    def __init__(self, strategy: _FakeMountStrategy) -> None:
        self.mount_strategy = strategy


class _FakeManifest:
    def __init__(self, mounts: list[tuple[_FakeMount, Path]]) -> None:
        self._mounts = mounts

    def ephemeral_mount_targets(self) -> list[tuple[_FakeMount, Path]]:
        return self._mounts


class _FakeState:
    def __init__(self, manifest: _FakeManifest) -> None:
        self.manifest = manifest


class _FakeSession:
    def __init__(self, manifest: _FakeManifest) -> None:
        self.state = _FakeState(manifest)
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    async def _terminate_ambiguous_mount_transition(self) -> None:
        await self.shutdown()


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_restores_in_reverse_order() -> None:
    events: list[str] = []
    left = _FakeMount(_FakeMountStrategy(events, name="left"))
    right = _FakeMount(_FakeMountStrategy(events, name="right"))
    session = _FakeSession(
        _FakeManifest(
            [
                (left, Path("/workspace/left")),
                (right, Path("/workspace/right")),
            ]
        )
    )

    async def operation() -> str:
        events.append("operation")
        return "persisted"

    result = await with_ephemeral_mounts_removed(
        cast(Any, session),
        operation,
        error_path=Path("/workspace"),
        error_cls=WorkspaceArchiveReadError,
        operation_error_context_key="snapshot_error_before_remount_corruption",
    )

    assert result == "persisted"
    assert events == [
        "teardown:left",
        "teardown:right",
        "operation",
        "restore:right",
        "restore:left",
    ]


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_reports_restore_error_after_operation_error() -> None:
    events: list[str] = []
    mount = _FakeMount(_FakeMountStrategy(events, name="mount", fail_restore=True))
    session = _FakeSession(_FakeManifest([(mount, Path("/workspace/mount"))]))
    operation_error = WorkspaceArchiveReadError(
        path=Path("/workspace"),
        context={"reason": "persist_failed"},
    )

    async def operation() -> bytes:
        events.append("operation")
        raise operation_error

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await with_ephemeral_mounts_removed(
            cast(Any, session),
            operation,
            error_path=Path("/workspace"),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key="snapshot_error_before_remount_corruption",
        )

    assert events == ["teardown:mount", "operation", "restore:mount"]
    assert exc_info.value.context["snapshot_error_before_remount_corruption"] == {
        "message": operation_error.message,
    }
    assert isinstance(exc_info.value.cause, RuntimeError)
    assert session.shutdown_calls == 1


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_restores_after_unexpected_operation_error() -> None:
    events: list[str] = []
    mount = _FakeMount(_FakeMountStrategy(events, name="mount"))
    session = _FakeSession(_FakeManifest([(mount, Path("/workspace/mount"))]))
    operation_error = RuntimeError("unexpected persistence failure")

    async def operation() -> bytes:
        events.append("operation")
        raise operation_error

    with pytest.raises(RuntimeError) as exc_info:
        await with_ephemeral_mounts_removed(
            cast(Any, session),
            operation,
            error_path=Path("/workspace"),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key=None,
        )

    assert exc_info.value is operation_error
    assert events == ["teardown:mount", "operation", "restore:mount"]
    assert session.shutdown_calls == 0


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_restores_before_ambiguous_shutdown() -> None:
    events: list[str] = []
    left = _FakeMount(_FakeMountStrategy(events, name="left"))
    right = _FakeMount(_FakeMountStrategy(events, name="right", fail_teardown=True))
    session = _FakeSession(
        _FakeManifest(
            [
                (left, Path("/workspace/left")),
                (right, Path("/workspace/right")),
            ]
        )
    )

    async def operation() -> None:
        raise AssertionError("operation must not run after teardown failure")

    with pytest.raises(WorkspaceArchiveReadError):
        await with_ephemeral_mounts_removed(
            cast(Any, session),
            operation,
            error_path=Path("/workspace"),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key=None,
        )

    assert events == ["teardown:left", "teardown:right", "restore:left"]
    assert session.shutdown_calls == 1


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_settles_ambiguous_shutdown_after_cancellation() -> (
    None
):
    events: list[str] = []
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()

    class _BlockingShutdownSession(_FakeSession):
        async def shutdown(self) -> None:
            self.shutdown_calls += 1
            shutdown_started.set()
            await release_shutdown.wait()
            events.append("shutdown-complete")

    mount = _FakeMount(_FakeMountStrategy(events, name="mount", fail_teardown=True))
    session = _BlockingShutdownSession(_FakeManifest([(mount, Path("/workspace/mount"))]))

    async def operation() -> None:
        raise AssertionError("operation must not run after teardown failure")

    task = asyncio.create_task(
        with_ephemeral_mounts_removed(
            cast(Any, session),
            operation,
            error_path=Path("/workspace"),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key=None,
        )
    )
    await shutdown_started.wait()
    task.cancel()
    release_shutdown.set()

    with pytest.raises(WorkspaceArchiveReadError):
        await task

    assert session.shutdown_calls == 1
    assert events == ["teardown:mount", "shutdown-complete"]


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_restores_after_operation_cancellation() -> None:
    events: list[str] = []
    mount = _FakeMount(_FakeMountStrategy(events, name="mount"))
    session = _FakeSession(_FakeManifest([(mount, Path("/workspace/mount"))]))
    operation_started = asyncio.Event()

    async def operation() -> None:
        events.append("operation")
        operation_started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        with_ephemeral_mounts_removed(
            cast(Any, session),
            operation,
            error_path=Path("/workspace"),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key=None,
        )
    )
    await operation_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert exc_info.value.args == ()
    assert events == ["teardown:mount", "operation", "restore:mount"]


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_settles_cancelled_teardown() -> None:
    events: list[str] = []
    teardown_started = asyncio.Event()
    release_teardown = asyncio.Event()

    class _BlockingTeardownStrategy(_FakeMountStrategy):
        async def teardown_for_snapshot(
            self,
            mount: object,
            session: object,
            path: Path,
        ) -> None:
            _ = (mount, session, path)
            events.append("teardown:mount")
            teardown_started.set()
            await release_teardown.wait()
            events.append("teardown-complete:mount")

    mount = _FakeMount(_BlockingTeardownStrategy(events, name="mount"))
    session = _FakeSession(_FakeManifest([(mount, Path("/workspace/mount"))]))

    async def operation() -> None:
        events.append("operation")

    task = asyncio.create_task(
        with_ephemeral_mounts_removed(
            cast(Any, session),
            operation,
            error_path=Path("/workspace"),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key=None,
        )
    )
    await teardown_started.wait()
    task.cancel()
    release_teardown.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["teardown:mount", "teardown-complete:mount", "restore:mount"]


@pytest.mark.asyncio
async def test_with_ephemeral_mounts_removed_settles_cancelled_restore() -> None:
    events: list[str] = []
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()

    class _BlockingRestoreStrategy(_FakeMountStrategy):
        async def restore_after_snapshot(
            self,
            mount: object,
            session: object,
            path: Path,
        ) -> None:
            _ = (mount, session, path)
            events.append("restore:mount")
            restore_started.set()
            await release_restore.wait()
            events.append("restore-complete:mount")

    mount = _FakeMount(_BlockingRestoreStrategy(events, name="mount"))
    session = _FakeSession(_FakeManifest([(mount, Path("/workspace/mount"))]))

    async def operation() -> None:
        events.append("operation")

    task = asyncio.create_task(
        with_ephemeral_mounts_removed(
            cast(Any, session),
            operation,
            error_path=Path("/workspace"),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key=None,
        )
    )
    await restore_started.wait()
    task.cancel()
    release_restore.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "teardown:mount",
        "operation",
        "restore:mount",
        "restore-complete:mount",
    ]

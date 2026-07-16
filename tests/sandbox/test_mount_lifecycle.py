from __future__ import annotations

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

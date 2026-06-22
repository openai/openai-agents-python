from __future__ import annotations

from pathlib import Path

from agents.sandbox.errors import (
    ErrorCode,
    ExecTimeoutError,
    GitCloneError,
    GitCopyError,
    SandboxError,
    SnapshotPersistError,
    SnapshotRestoreError,
    WorkspaceArchiveReadError,
    WorkspaceReadNotFoundError,
    WorkspaceStopError,
    WorkspaceWriteTypeError,
)


def test_sandbox_error_retryable_can_be_set_explicitly() -> None:
    error = SandboxError(
        message="backend is unavailable",
        error_code=ErrorCode.EXEC_TRANSPORT_ERROR,
        op="exec",
        context={},
        retryable=True,
    )

    assert error.retryable is True


def test_wrapped_sandbox_error_inherits_retryable_from_cause() -> None:
    cause = WorkspaceArchiveReadError(
        path=Path("/workspace"),
        retryable=False,
    )

    error = WorkspaceStopError(path=Path("/workspace"), cause=cause)

    assert error.retryable is False


def test_deterministic_sandbox_errors_are_non_retryable() -> None:
    assert WorkspaceReadNotFoundError(path=Path("/workspace/missing.txt")).retryable is False
    assert (
        WorkspaceWriteTypeError(path=Path("/workspace/out.txt"), actual_type="str").retryable
        is False
    )
    assert ExecTimeoutError(command=("python", "script.py"), timeout_s=1.0).retryable is False


def test_broad_archive_errors_default_to_unknown_retryability() -> None:
    error = WorkspaceArchiveReadError(path=Path("/workspace"))

    assert error.retryable is None


def test_broad_materialization_and_snapshot_errors_default_to_unknown_retryability() -> None:
    assert GitCloneError(url="https://example.test/repo.git", ref="main").retryable is None
    assert GitCopyError(src_root="/tmp/repo", dest=Path("/workspace")).retryable is None
    assert SnapshotPersistError(snapshot_id="snap", path=Path("/tmp/snap")).retryable is None
    assert SnapshotRestoreError(snapshot_id="snap", path=Path("/tmp/snap")).retryable is None

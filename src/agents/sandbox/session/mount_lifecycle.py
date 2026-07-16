from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, TypeVar, cast

from ..errors import (
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceIOError,
)

if TYPE_CHECKING:
    from ..entries import Mount
    from .base_sandbox_session import BaseSandboxSession

ArchiveError: TypeAlias = WorkspaceArchiveReadError | WorkspaceArchiveWriteError
ArchiveErrorClass: TypeAlias = type[WorkspaceArchiveReadError] | type[WorkspaceArchiveWriteError]

_ResultT = TypeVar("_ResultT")
_MISSING = object()


async def with_ephemeral_mounts_removed(
    session: BaseSandboxSession,
    operation: Callable[[], Awaitable[_ResultT]],
    *,
    error_path: Path,
    error_cls: ArchiveErrorClass,
    operation_error_context_key: str | None,
) -> _ResultT:
    detached_mounts: list[tuple[Mount, Path]] = []
    detach_error: ArchiveError | None = None
    for mount_entry, mount_path in session.state.manifest.ephemeral_mount_targets():
        try:
            await mount_entry.mount_strategy.teardown_for_snapshot(mount_entry, session, mount_path)
        except Exception as exc:
            detach_error = error_cls(path=error_path, cause=exc)
            break
        detached_mounts.append((mount_entry, mount_path))

    operation_error: ArchiveError | None = None
    operation_result: object = _MISSING
    if detach_error is None:
        try:
            operation_result = await operation()
        except WorkspaceIOError as exc:
            if not isinstance(exc, error_cls):
                raise
            operation_error = cast(ArchiveError, exc)

    restore_error = await restore_detached_mounts(
        session,
        detached_mounts,
        error_path=error_path,
        error_cls=error_cls,
    )

    if restore_error is not None:
        if operation_error is not None and operation_error_context_key is not None:
            restore_error.context[operation_error_context_key] = {
                "message": operation_error.message
            }
        raise restore_error
    if detach_error is not None:
        raise detach_error
    if operation_error is not None:
        raise operation_error

    assert operation_result is not _MISSING
    return cast(_ResultT, operation_result)


async def restore_detached_mounts(
    session: BaseSandboxSession,
    detached_mounts: list[tuple[Mount, Path]],
    *,
    error_path: Path,
    error_cls: ArchiveErrorClass,
) -> ArchiveError | None:
    restore_error: ArchiveError | None = None
    for mount_entry, mount_path in reversed(detached_mounts):
        try:
            await mount_entry.mount_strategy.restore_after_snapshot(
                mount_entry, session, mount_path
            )
        except Exception as exc:
            current_error = error_cls(path=error_path, cause=exc)
            if restore_error is None:
                restore_error = current_error
            else:
                additional_errors = restore_error.context.setdefault(
                    "additional_remount_errors", []
                )
                assert isinstance(additional_errors, list)
                additional_errors.append(workspace_archive_error_summary(current_error))
    return restore_error


def workspace_archive_error_summary(error: ArchiveError) -> dict[str, str]:
    summary = {"message": error.message}
    if error.cause is not None:
        summary["cause_type"] = type(error.cause).__name__
        summary["cause"] = str(error.cause)
    return summary


__all__ = [
    "restore_detached_mounts",
    "with_ephemeral_mounts_removed",
    "workspace_archive_error_summary",
]

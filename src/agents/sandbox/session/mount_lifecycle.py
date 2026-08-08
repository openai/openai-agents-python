from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, TypeVar, cast

from ..errors import (
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceIOError,
    WorkspaceStartError,
)

if TYPE_CHECKING:
    from ..entries import Mount
    from .base_sandbox_session import BaseSandboxSession

ArchiveError: TypeAlias = (
    WorkspaceArchiveReadError | WorkspaceArchiveWriteError | WorkspaceStartError
)
ArchiveErrorClass: TypeAlias = (
    type[WorkspaceArchiveReadError] | type[WorkspaceArchiveWriteError] | type[WorkspaceStartError]
)

_ResultT = TypeVar("_ResultT")
_MISSING = object()
_MOUNT_TRANSITION_OWNER: ContextVar[tuple[object, asyncio.Task[object]] | None] = ContextVar(
    "sandbox_mount_transition_owner",
    default=None,
)


async def with_ephemeral_mounts_removed(
    session: BaseSandboxSession,
    operation: Callable[[], Awaitable[_ResultT]],
    *,
    error_path: Path,
    error_cls: ArchiveErrorClass,
    operation_error_context_key: str | None,
    restore_on_success: bool = True,
) -> _ResultT:
    detached_mounts: list[tuple[Mount, Path]] = []
    detach_error: ArchiveError | None = None
    detach_transition_ambiguous = False
    caller_cancelled = False
    for mount_entry, mount_path in session.state.manifest.ephemeral_mount_targets():
        transition_error, transition_cancelled = await _settle_mount_transition(
            session,
            mount_entry.mount_strategy.teardown_for_snapshot(mount_entry, session, mount_path),
        )
        caller_cancelled = caller_cancelled or transition_cancelled
        if transition_error is not None:
            detach_error = _mount_transition_error(
                error_cls,
                error_path=error_path,
                transition_error=transition_error,
                reason="mount_teardown_cancelled",
            )
            detach_transition_ambiguous = True
            break
        detached_mounts.append((mount_entry, mount_path))
        if caller_cancelled:
            break

    operation_error: BaseException | None = None
    operation_result: object = _MISSING
    if detach_error is None and not caller_cancelled:
        try:
            operation_result = await operation()
        except asyncio.CancelledError:
            caller_cancelled = True
        except WorkspaceIOError as exc:
            operation_error = exc
        except BaseException as exc:
            operation_error = exc

    restore_error: ArchiveError | None = None
    should_restore = (
        (operation_result is not _MISSING and restore_on_success is True)
        or detach_error is not None
        or operation_error is not None
        or caller_cancelled
    )
    if should_restore:
        restore_error, restore_cancelled = await _restore_detached_mounts_settled(
            session,
            detached_mounts,
            error_path=error_path,
            error_cls=error_cls,
        )
        caller_cancelled = caller_cancelled or restore_cancelled
    if detach_transition_ambiguous and restore_error is None:
        terminal_error, terminal_cancelled = await _terminate_ambiguous_mount_session(session)
        caller_cancelled = caller_cancelled or terminal_cancelled
        if terminal_error is not None and detach_error is not None:
            detach_error.context["terminal_cleanup_failed"] = True

    if restore_error is not None:
        if (
            isinstance(operation_error, WorkspaceIOError)
            and operation_error_context_key is not None
        ):
            restore_error.context[operation_error_context_key] = {
                "message": operation_error.message
            }
        raise restore_error
    if detach_error is not None:
        raise detach_error
    if operation_error is not None:
        raise operation_error
    if caller_cancelled:
        raise asyncio.CancelledError() from None

    assert operation_result is not _MISSING
    return cast(_ResultT, operation_result)


async def restore_detached_mounts(
    session: BaseSandboxSession,
    detached_mounts: list[tuple[Mount, Path]],
    *,
    error_path: Path,
    error_cls: ArchiveErrorClass,
) -> ArchiveError | None:
    restore_error, caller_cancelled = await _restore_detached_mounts_settled(
        session,
        detached_mounts,
        error_path=error_path,
        error_cls=error_cls,
    )
    if restore_error is not None:
        return restore_error
    if caller_cancelled:
        raise asyncio.CancelledError() from None
    return None


async def _restore_detached_mounts_settled(
    session: BaseSandboxSession,
    detached_mounts: list[tuple[Mount, Path]],
    *,
    error_path: Path,
    error_cls: ArchiveErrorClass,
) -> tuple[ArchiveError | None, bool]:
    restore_error: ArchiveError | None = None
    caller_cancelled = False
    for mount_entry, mount_path in reversed(detached_mounts):
        transition_error, transition_cancelled = await _settle_mount_transition(
            session,
            mount_entry.mount_strategy.restore_after_snapshot(mount_entry, session, mount_path),
        )
        caller_cancelled = caller_cancelled or transition_cancelled
        if transition_error is not None:
            current_error = _mount_transition_error(
                error_cls,
                error_path=error_path,
                transition_error=transition_error,
                reason="mount_restore_cancelled",
            )
            if restore_error is None:
                restore_error = current_error
            else:
                additional_errors = restore_error.context.setdefault(
                    "additional_remount_errors", []
                )
                assert isinstance(additional_errors, list)
                additional_errors.append(workspace_archive_error_summary(current_error))
    if restore_error is not None:
        terminal_error, terminal_cancelled = await _terminate_ambiguous_mount_session(session)
        caller_cancelled = caller_cancelled or terminal_cancelled
        if terminal_error is not None:
            restore_error.context["terminal_cleanup_failed"] = True
    return restore_error, caller_cancelled


async def _settle_mount_transition(
    session: BaseSandboxSession,
    operation: Awaitable[None],
) -> tuple[BaseException | None, bool]:
    async def run_registered_transition() -> None:
        current_task = asyncio.current_task()
        assert current_task is not None
        owner_token = _MOUNT_TRANSITION_OWNER.set(
            (session, cast(asyncio.Task[object], current_task))
        )
        try:
            await operation
        finally:
            _MOUNT_TRANSITION_OWNER.reset(owner_token)

    task = asyncio.create_task(
        run_registered_transition(),
        name="agents.mount_transition",
    )
    completion = asyncio.create_task(asyncio.wait((task,)))
    caller_cancelled = False
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            caller_cancelled = True
    completion.result()
    try:
        task.result()
    except BaseException as exc:
        return exc, caller_cancelled
    return None, caller_cancelled


def current_task_owns_mount_transition(session: BaseSandboxSession) -> bool:
    owner = _MOUNT_TRANSITION_OWNER.get()
    current_task = asyncio.current_task()
    return owner is not None and owner[0] is session and owner[1] is current_task


async def _terminate_ambiguous_mount_session(
    session: BaseSandboxSession,
) -> tuple[BaseException | None, bool]:
    return await _settle_mount_transition(
        session,
        session._terminate_ambiguous_mount_transition(),
    )


def _mount_transition_error(
    error_cls: ArchiveErrorClass,
    *,
    error_path: Path,
    transition_error: BaseException,
    reason: str,
) -> ArchiveError:
    if isinstance(transition_error, asyncio.CancelledError):
        return error_cls(path=error_path, context={"reason": reason})
    return error_cls(path=error_path, cause=transition_error)


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

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from ..apply_diff import ApplyDiffMode, apply_diff
from ..editor import ApplyPatchOperation, ApplyPatchOperationType, ApplyPatchResult
from .errors import (
    ApplyPatchDecodeError,
    ApplyPatchDiffError,
    ApplyPatchFileNotFoundError,
    ApplyPatchPathError,
    InvalidManifestPathError,
    WorkspaceReadNotFoundError,
)

if TYPE_CHECKING:
    from .session.base_sandbox_session import BaseSandboxSession
    from .types import User


@runtime_checkable
class PatchFormat(Protocol):
    @staticmethod
    def apply_diff(input: str, diff: str, mode: ApplyDiffMode = "default") -> str: ...


class V4AFormat:
    @staticmethod
    def apply_diff(input: str, diff: str, mode: ApplyDiffMode = "default") -> str:
        return apply_diff(input, diff, mode=mode)


class WorkspaceEditor:
    def __init__(
        self,
        session: BaseSandboxSession,
        *,
        user: str | User | None = None,
    ) -> None:
        self._session = session
        self._user = user

    async def apply_patch(
        self,
        operations: ApplyPatchOperation
        | dict[str, object]
        | list[ApplyPatchOperation | dict[str, object]],
        *,
        patch_format: PatchFormat | Literal["v4a"] = "v4a",
    ) -> str:
        format_impl = _resolve_patch_format(patch_format)
        for operation in _coerce_operations(operations):
            await self.apply_operation(operation, patch_format=format_impl)
        return "Done!"

    async def apply_operation(
        self,
        operation: ApplyPatchOperation,
        *,
        patch_format: PatchFormat | Literal["v4a"] = "v4a",
    ) -> ApplyPatchResult:
        format_impl = _resolve_patch_format(patch_format)
        relative_path = self._validate_path(operation.path)
        destination = self._session.normalize_path(relative_path)
        display_path = relative_path.as_posix()

        if operation.type == "delete_file":
            await self._ensure_exists(destination, display_path=display_path)
            await self._session.rm(destination, user=self._user)
            return ApplyPatchResult(output=f"Deleted {display_path}")

        if operation.diff is None:
            raise ApplyPatchDiffError(
                message=(
                    f"Missing diff for operation type {operation.type} on path {operation.path}"
                ),
                path=operation.path,
            )

        if operation.type == "update_file":
            original_text = await self._read_text(destination, op_path=operation.path)
            try:
                updated_text = format_impl.apply_diff(original_text, operation.diff, mode="default")
            except ValueError as exc:
                raise ApplyPatchDiffError(
                    message=str(exc),
                    path=operation.path,
                    cause=exc,
                ) from exc
            if operation.move_to is None:
                await self._write_text(destination, updated_text)
                return ApplyPatchResult(output=f"Updated {display_path}")

            moved_relative_path = self._validate_path(operation.move_to)
            moved_destination = self._session.normalize_path(moved_relative_path)
            await self._write_text(moved_destination, updated_text)
            if moved_destination != destination:
                await self._session.rm(destination)
            moved_display_path = moved_relative_path.as_posix()
            return ApplyPatchResult(
                output=f"Updated {display_path}\nMoved {display_path} to {moved_display_path}"
            )

        if operation.type == "create_file":
            try:
                created_text = format_impl.apply_diff("", operation.diff, mode="create")
            except ValueError as exc:
                raise ApplyPatchDiffError(
                    message=str(exc),
                    path=operation.path,
                    cause=exc,
                ) from exc
            await self._write_text(destination, created_text)
            return ApplyPatchResult(output=f"Created {display_path}")

        raise ApplyPatchDiffError(
            message=f"Unknown operation type: {operation.type}",
            path=operation.path,
        )

    def _validate_path(self, path: str | Path) -> Path:
        if isinstance(path, str):
            if not path.strip():
                raise ApplyPatchPathError(path=path, reason="empty")
            normalized_path = Path(path)
        else:
            normalized_path = path

        try:
            return self._session._workspace_path_policy().relative_path(normalized_path)
        except InvalidManifestPathError as exc:
            raise ApplyPatchPathError(
                path=normalized_path,
                reason="escape_root",
                cause=exc,
            ) from exc

    async def _ensure_exists(self, destination: Path, *, display_path: str) -> None:
        try:
            handle = await self._session.read(destination, user=self._user)
        except (FileNotFoundError, WorkspaceReadNotFoundError) as exc:
            raise ApplyPatchFileNotFoundError(path=Path(display_path), cause=exc) from exc
        else:
            handle.close()

    async def _read_text(self, destination: Path, *, op_path: str) -> str:
        try:
            handle = await self._session.read(destination, user=self._user)
        except (FileNotFoundError, WorkspaceReadNotFoundError) as exc:
            raise ApplyPatchFileNotFoundError(path=Path(op_path), cause=exc) from exc

        try:
            payload = handle.read()
        finally:
            handle.close()

        if isinstance(payload, str):
            return payload
        if isinstance(payload, bytes | bytearray):
            try:
                return bytes(payload).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ApplyPatchDecodeError(path=destination, cause=exc) from exc
        raise ApplyPatchDiffError(
            message=f"apply_patch read() returned non-text content: {type(payload).__name__}",
            path=op_path,
        )

    async def _write_text(self, destination: Path, text: str) -> None:
        await self._session.mkdir(destination.parent, parents=True, user=self._user)
        await self._session.write(
            destination,
            io.BytesIO(text.encode("utf-8")),
            user=self._user,
        )


def _coerce_operations(
    operations: ApplyPatchOperation
    | dict[str, object]
    | list[ApplyPatchOperation | dict[str, object]],
) -> list[ApplyPatchOperation]:
    if isinstance(operations, ApplyPatchOperation):
        return [operations]
    if isinstance(operations, dict):
        return [_coerce_operation_mapping(operations)]
    if isinstance(operations, list):
        coerced: list[ApplyPatchOperation] = []
        for operation in operations:
            if isinstance(operation, ApplyPatchOperation):
                coerced.append(operation)
            elif isinstance(operation, dict):
                coerced.append(_coerce_operation_mapping(operation))
            else:
                raise ApplyPatchDiffError(
                    message=f"Invalid apply_patch operation type: {type(operation).__name__}"
                )
        return coerced
    raise ApplyPatchDiffError(
        message=f"Invalid apply_patch operations payload: {type(operations).__name__}"
    )


def _coerce_operation_mapping(operation: dict[str, object]) -> ApplyPatchOperation:
    raw_type = operation.get("type")
    raw_path = operation.get("path")
    raw_diff = operation.get("diff")
    raw_ctx_wrapper = operation.get("ctx_wrapper")

    if raw_type not in {"create_file", "update_file", "delete_file"}:
        raise ApplyPatchDiffError(
            message=f"Invalid apply_patch operation type: {type(raw_type).__name__}"
        )
    if not isinstance(raw_path, str):
        raise ApplyPatchDiffError(
            message=f"Invalid apply_patch path type: {type(raw_path).__name__}"
        )
    if raw_diff is not None and not isinstance(raw_diff, str):
        raise ApplyPatchDiffError(
            message=f"Invalid apply_patch diff type: {type(raw_diff).__name__}"
        )
    return ApplyPatchOperation(
        type=cast(ApplyPatchOperationType, raw_type),
        path=raw_path,
        diff=raw_diff,
        ctx_wrapper=cast(Any, raw_ctx_wrapper),
    )


def _resolve_patch_format(
    patch_format: PatchFormat | Literal["v4a"],
) -> PatchFormat:
    if patch_format == "v4a":
        return V4AFormat
    if isinstance(patch_format, PatchFormat):
        return patch_format
    raise ApplyPatchDiffError(message=f"Unsupported patch format: {patch_format!r}")


__all__ = ["PatchFormat", "V4AFormat", "WorkspaceEditor"]

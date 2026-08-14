from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import PurePath, PurePosixPath
from typing import Any, cast

from ...editor import ApplyPatchOperation
from ...run_context import RunContextWrapper
from ...tool import ToolOutputImage
from ..session.base_sandbox_session import BaseSandboxSession
from ..types import User
from ..workspace_paths import coerce_posix_path, windows_absolute_path
from .tools import (
    ExecCommandArgs,
    ExecCommandTool,
    SandboxApplyPatchTool,
    ViewImageArgs,
    ViewImageTool,
)

ApprovalFunction = Callable[[RunContextWrapper[Any], dict[str, Any], str], Awaitable[bool]]


def _rebase_run_cwd_path(path: str | PurePath, cwd: PurePosixPath | None) -> str:
    path_str = path.as_posix() if isinstance(path, PurePath) else path
    if cwd is None:
        return path_str
    if windows_absolute_path(path) is not None:
        return path_str

    posix_path = coerce_posix_path(path)
    if posix_path.is_absolute():
        return path_str
    return (cwd / posix_path).as_posix()


def _rebase_exec_params(params: dict[str, Any], cwd: PurePosixPath | None) -> dict[str, Any]:
    if cwd is None:
        return params
    effective = dict(params)
    raw_workdir = effective.get("workdir")
    if raw_workdir is None or (isinstance(raw_workdir, str) and raw_workdir.strip() == ""):
        effective["workdir"] = cwd.as_posix()
    elif isinstance(raw_workdir, str):
        effective["workdir"] = _rebase_run_cwd_path(raw_workdir, cwd)
    return effective


def _wrap_approval(
    approval: bool | ApprovalFunction,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool | ApprovalFunction:
    if isinstance(approval, bool):
        return approval

    async def wrapped(
        ctx: RunContextWrapper[Any], params: dict[str, Any], call_id: str
    ) -> bool:
        return await approval(ctx, transform(params), call_id)

    return wrapped


class RunCwdExecCommandTool(ExecCommandTool):
    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        user: str | User | None = None,
        cwd: PurePosixPath | None = None,
    ) -> None:
        self._run_cwd = cwd
        super().__init__(session=session, user=user)

    def finalize_run_cwd(self) -> None:
        self.needs_approval = _wrap_approval(
            cast(bool | ApprovalFunction, self.needs_approval),
            lambda params: _rebase_exec_params(params, self._run_cwd),
        )

    async def run(self, args: ExecCommandArgs) -> str:
        workdir = args.workdir
        if self._run_cwd is not None:
            if workdir is None or workdir.strip() == "":
                workdir = self._run_cwd.as_posix()
            else:
                workdir = _rebase_run_cwd_path(workdir, self._run_cwd)
        return await super().run(args.model_copy(update={"workdir": workdir}))


class RunCwdViewImageTool(ViewImageTool):
    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        user: str | User | None = None,
        cwd: PurePosixPath | None = None,
    ) -> None:
        self._run_cwd = cwd
        super().__init__(session=session, user=user)

    def finalize_run_cwd(self) -> None:
        def transform(params: dict[str, Any]) -> dict[str, Any]:
            effective = dict(params)
            raw_path = effective.get("path")
            if isinstance(raw_path, str):
                effective["path"] = _rebase_run_cwd_path(raw_path, self._run_cwd)
            return effective

        self.needs_approval = _wrap_approval(
            cast(bool | ApprovalFunction, self.needs_approval),
            transform,
        )

    async def run(self, args: ViewImageArgs) -> ToolOutputImage | str:
        path = _rebase_run_cwd_path(args.path, self._run_cwd)
        return await super().run(args.model_copy(update={"path": path}))


class RunCwdSandboxApplyPatchTool(SandboxApplyPatchTool):
    def __init__(
        self,
        *,
        session: BaseSandboxSession,
        user: str | User | None = None,
        cwd: PurePosixPath | None = None,
    ) -> None:
        self._run_cwd = cwd
        super().__init__(session=session, user=user)

    def parse_custom_input(self, raw_input: str) -> list[ApplyPatchOperation]:
        operations = super().parse_custom_input(raw_input)
        if self._run_cwd is None:
            return operations
        return [
            replace(
                operation,
                path=_rebase_run_cwd_path(operation.path, self._run_cwd),
                move_to=(
                    _rebase_run_cwd_path(operation.move_to, self._run_cwd)
                    if operation.move_to is not None
                    else None
                ),
            )
            for operation in operations
        ]

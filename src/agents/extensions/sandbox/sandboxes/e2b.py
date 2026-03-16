from __future__ import annotations

import base64
import binascii
import inspect
import io
import shlex
import tarfile
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import BaseModel, Field

from ....sandbox.codex_config import (
    CodexConfig,
    apply_codex_to_manifest,
    apply_codex_to_session_state,
)
from ....sandbox.entries import Mount, resolve_workspace_path
from ....sandbox.errors import (
    ExecNonZeroError,
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import SandboxSession, SandboxSessionState
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.sandbox_client import BaseSandboxClient
from ....sandbox.snapshot import SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult
from ....sandbox.util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_contains_type,
    exception_chain_has_status_code,
    retry_async,
)


class _E2BFilesAPI:
    def write(
        self,
        path: str,
        data: bytes,
        request_timeout: float | None = None,
    ) -> object:
        raise NotImplementedError

    def remove(self, path: str, request_timeout: float | None = None) -> object:
        raise NotImplementedError

    def make_dir(self, path: str, request_timeout: float | None = None) -> object:
        raise NotImplementedError

    def read(self, path: str, format: str = "bytes") -> object:
        raise NotImplementedError


class _E2BCommandsAPI:
    def run(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
    ) -> object:
        raise NotImplementedError


class _E2BSandboxAPI:
    sandbox_id: object
    files: _E2BFilesAPI
    commands: _E2BCommandsAPI

    def beta_pause(self) -> object:
        raise NotImplementedError

    def kill(self) -> object:
        raise NotImplementedError

    def is_running(self, request_timeout: float | None = None) -> object:
        raise NotImplementedError


class _E2BSandboxFactoryAPI:
    def create(
        self,
        *,
        template: str | None = None,
        timeout: int | None = None,
        metadata: dict[str, str] | None = None,
        envs: dict[str, str] | None = None,
        secure: bool = True,
        allow_internet_access: bool = True,
    ) -> object:
        raise NotImplementedError

    def _cls_connect(
        self,
        *,
        sandbox_id: str,
        timeout: int | None = None,
    ) -> object:
        raise NotImplementedError


# NOTE: We avoid importing `e2b_code_interpreter` or `e2b` at module import time so that users
# without the optional dependency can still import the sandbox package (they just can't use the
# E2B sandbox).


class E2BSandboxType(str, Enum):
    """Supported E2B sandbox implementations."""

    CODE_INTERPRETER_ASYNC = "e2b_code_interpreter_async"
    CODE_INTERPRETER = "e2b_code_interpreter"
    E2B_ASYNC = "e2b_async"
    E2B = "e2b"


def _coerce_sandbox_type(value: E2BSandboxType | str | None) -> E2BSandboxType:
    if value is None:
        raise ValueError(
            "E2BSandboxClientOptions.sandbox_type is required. "
            "Use one of: e2b_code_interpreter_async, e2b_code_interpreter, e2b_async, e2b."
        )
    if isinstance(value, E2BSandboxType):
        return value
    try:
        return E2BSandboxType(value)
    except ValueError as e:
        raise ValueError(
            "Invalid E2BSandboxClientOptions.sandbox_type. "
            "Use one of: e2b_code_interpreter_async, e2b_code_interpreter, e2b_async, e2b."
        ) from e


def _import_sandbox_class(sandbox_type: E2BSandboxType) -> _E2BSandboxFactoryAPI:
    if sandbox_type in {
        E2BSandboxType.CODE_INTERPRETER_ASYNC,
        E2BSandboxType.CODE_INTERPRETER,
    }:
        module_name = "e2b_code_interpreter"
        class_name = (
            "AsyncSandbox" if sandbox_type is E2BSandboxType.CODE_INTERPRETER_ASYNC else "Sandbox"
        )
        missing_msg = (
            "E2BSandboxClient requires the optional `e2b-code-interpreter` dependency.\n"
            "Install the E2B extra before using this sandbox backend."
        )
    else:
        module_name = "e2b"
        class_name = "AsyncSandbox" if sandbox_type is E2BSandboxType.E2B_ASYNC else "Sandbox"
        missing_msg = (
            "E2BSandboxClient requires the optional `e2b` dependency.\n"
            "Install the E2B extra before using this sandbox backend."
        )

    try:
        module = __import__(module_name, fromlist=[class_name])
        sandbox_cls = getattr(module, class_name)
    except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
        if module_name == "e2b":
            try:
                module = __import__("e2b.sandbox", fromlist=[class_name])
                sandbox_cls = getattr(module, class_name)
            except Exception:
                raise ImportError(missing_msg) from e
        else:
            raise ImportError(missing_msg) from e

    return cast(_E2BSandboxFactoryAPI, sandbox_cls)


def _as_sandbox_api(sandbox: object) -> _E2BSandboxAPI:
    return cast(_E2BSandboxAPI, sandbox)


def _sandbox_id(sandbox: object) -> object:
    return _as_sandbox_api(sandbox).sandbox_id


def _sandbox_write_file(
    sandbox: object,
    path: str,
    data: bytes,
    *,
    request_timeout: float | None = None,
) -> object:
    return _as_sandbox_api(sandbox).files.write(
        path,
        data,
        request_timeout=request_timeout,
    )


def _sandbox_remove_file(
    sandbox: object,
    path: str,
    *,
    request_timeout: float | None = None,
) -> object:
    return _as_sandbox_api(sandbox).files.remove(path, request_timeout=request_timeout)


def _sandbox_make_dir(
    sandbox: object,
    path: str,
    *,
    request_timeout: float | None = None,
) -> object:
    return _as_sandbox_api(sandbox).files.make_dir(path, request_timeout=request_timeout)


def _sandbox_read_file(sandbox: object, path: str, *, format: str = "bytes") -> object:
    return _as_sandbox_api(sandbox).files.read(path, format=format)


def _sandbox_run_command(
    sandbox: object,
    command: str,
    *,
    timeout: float | None = None,
    cwd: str | None = None,
    envs: dict[str, str] | None = None,
    user: str | None = None,
) -> object:
    return _as_sandbox_api(sandbox).commands.run(
        command,
        timeout=timeout,
        cwd=cwd,
        envs=envs,
        user=user,
    )


def _sandbox_pause(sandbox: object) -> object:
    return _as_sandbox_api(sandbox).beta_pause()


def _sandbox_kill(sandbox: object) -> object:
    return _as_sandbox_api(sandbox).kill()


def _sandbox_is_running(sandbox: object, *, request_timeout: float | None = None) -> object:
    return _as_sandbox_api(sandbox).is_running(request_timeout=request_timeout)


def _sandbox_create(
    sandbox_class: _E2BSandboxFactoryAPI,
    *,
    template: str | None = None,
    timeout: int | None = None,
    metadata: dict[str, str] | None = None,
    envs: dict[str, str] | None = None,
    secure: bool = True,
    allow_internet_access: bool = True,
) -> object:
    return sandbox_class.create(
        template=template,
        timeout=timeout,
        metadata=metadata,
        envs=envs,
        secure=secure,
        allow_internet_access=allow_internet_access,
    )


def _sandbox_connect(
    sandbox_class: _E2BSandboxFactoryAPI,
    *,
    sandbox_id: str,
    timeout: int | None = None,
) -> object:
    return sandbox_class._cls_connect(sandbox_id=sandbox_id, timeout=timeout)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await cast(Awaitable[object], value)
    return value


def _import_e2b_exceptions() -> Mapping[str, type[BaseException]]:
    """Best-effort import of E2B exception classes for classification."""

    try:
        from e2b.exceptions import (  # type: ignore[import-untyped]
            NotFoundException,
            SandboxException,
            TimeoutException,
        )
    except Exception:  # pragma: no cover - handled by fallbacks
        return {}

    return {
        "not_found": cast(type[BaseException], NotFoundException),
        "sandbox": cast(type[BaseException], SandboxException),
        "timeout": cast(type[BaseException], TimeoutException),
    }


def _import_command_exit_exception() -> type[BaseException] | None:
    try:
        from e2b.sandbox.commands.command_handle import (  # type: ignore[import-untyped]
            CommandExitException,
        )
    except Exception:  # pragma: no cover - handled by fallbacks
        return None
    return cast(type[BaseException], CommandExitException)


def _retryable_persist_workspace_error_types() -> tuple[type[BaseException], ...]:
    excs = _import_e2b_exceptions()
    retryable: list[type[BaseException]] = []
    timeout_exc = excs.get("timeout")
    if timeout_exc is not None:
        retryable.append(timeout_exc)
    return tuple(retryable)


class E2BSandboxTimeouts(BaseModel):
    """Timeout configuration for E2B operations."""

    # E2B commands default to a 60s timeout when `timeout=None`. Sandbox semantics
    # for `timeout=None` are "no timeout", so we pass a large sentinel value instead.
    exec_timeout_unbounded_s: float = Field(default=24 * 60 * 60, ge=1)  # 24 hours

    # Keepalive / is_running should be quick; if it does not return promptly,
    # the sandbox is unhealthy.
    keepalive_s: float = Field(default=5, ge=1)

    # best-effort cleanup (e.g., removing temp tar files) should not block shutdown for long.
    cleanup_s: float = Field(default=30, ge=1)

    # fast, small ops like `mkdir -p` / `cat` / metadata-ish operations.
    fast_op_s: float = Field(default=10, ge=1)

    # uploading tar contents can take longer than fast ops.
    file_upload_s: float = Field(default=30, ge=1)

    # snapshot tar ops can be heavier on large workspaces.
    snapshot_tar_s: float = Field(default=60, ge=1)


@dataclass(frozen=True)
class E2BSandboxClientOptions:
    """Client options for the E2B sandbox."""

    sandbox_type: E2BSandboxType | str
    template: str | None = None
    timeout: int | None = None
    metadata: dict[str, str] | None = None
    envs: dict[str, str] | None = None
    secure: bool = True
    allow_internet_access: bool = True
    timeouts: E2BSandboxTimeouts | dict[str, object] | None = None
    pause_on_exit: bool = False


class E2BSandboxSessionState(SandboxSessionState):
    sandbox_id: str
    sandbox_type: E2BSandboxType = Field(default=E2BSandboxType.CODE_INTERPRETER_ASYNC)
    template: str | None = None
    sandbox_timeout: int | None = None
    metadata: dict[str, str] | None = None
    base_envs: dict[str, str] = Field(default_factory=dict)
    secure: bool = True
    allow_internet_access: bool = True
    timeouts: E2BSandboxTimeouts = Field(default_factory=E2BSandboxTimeouts)
    pause_on_exit: bool = False
    workspace_root_ready: bool = False


class E2BSandboxSession(BaseSandboxSession):
    """E2B-backed sandbox session implementation."""

    state: E2BSandboxSessionState
    _sandbox: _E2BSandboxAPI
    _skip_start: bool
    _workspace_root_ready: bool
    _resume_preserves_system_state: bool

    def __init__(
        self,
        *,
        state: E2BSandboxSessionState,
        sandbox: object,
    ) -> None:
        self.state = state
        self._sandbox = _as_sandbox_api(sandbox)
        self._skip_start = False
        self._workspace_root_ready = state.workspace_root_ready
        self._resume_preserves_system_state = False

    @classmethod
    def from_state(
        cls,
        state: E2BSandboxSessionState,
        *,
        sandbox: object,
    ) -> E2BSandboxSession:
        return cls(state=state, sandbox=sandbox)

    @property
    def sandbox_id(self) -> str:
        return self.state.sandbox_id

    async def _resolved_envs(self) -> dict[str, str]:
        manifest_envs = await self.state.manifest.environment.resolve()
        # Manifest envs take precedence over base envs supplied via client options.
        return {**self.state.base_envs, **manifest_envs}

    def _coerce_exec_timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return float(self.state.timeouts.exec_timeout_unbounded_s)
        if timeout_s <= 0:
            # Sandbox timeout cannot be <= 0; use 1s and rely on caller semantics.
            return 1.0
        return float(timeout_s)

    async def _ensure_dir(self, path: Path, *, reason: str) -> None:
        """Create a directory using the E2B Files API."""
        if path == Path("/"):
            return
        try:
            await _maybe_await(
                _sandbox_make_dir(
                    self._sandbox,
                    str(path),
                    request_timeout=self.state.timeouts.fast_op_s,
                )
            )
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            raise WorkspaceArchiveWriteError(path=path, context={"reason": reason}, cause=e) from e

    async def _ensure_workspace_root(self) -> None:
        """Ensure the workspace root exists before materialization starts."""
        await self._ensure_dir(Path(self.state.manifest.root), reason="root_make_failed")

    async def _prepare_workspace_root_for_exec(self) -> None:
        """Create the workspace root through the command API before using it as `cwd`."""
        root = str(Path(self.state.manifest.root))
        envs = await self._resolved_envs()
        result = await _maybe_await(
            _sandbox_run_command(
                self._sandbox,
                f"mkdir -p -- {shlex.quote(root)}",
                timeout=self.state.timeouts.fast_op_s,
                cwd="/",
                envs=envs,
            )
        )
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            raise WorkspaceStartError(
                path=Path(self.state.manifest.root),
                context={
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": exit_code,
                    "stderr": str(getattr(result, "stderr", "") or ""),
                },
            )
        self._workspace_root_ready = True
        self.state.workspace_root_ready = True

    def should_provision_manifest_accounts_on_resume(self) -> bool:
        return not self._resume_preserves_system_state

    async def start(self) -> None:
        if self._skip_start:
            if not self._workspace_root_ready:
                try:
                    await self._prepare_workspace_root_for_exec()
                except WorkspaceStartError:
                    raise
                except Exception as e:
                    raise WorkspaceStartError(path=Path(self.state.manifest.root), cause=e) from e
            return
        try:
            # Ensure the workspace root exists before manifest materialization/hydration occurs.
            await self._ensure_workspace_root()
            await self._prepare_workspace_root_for_exec()
        except WorkspaceStartError:
            raise
        except Exception as e:
            raise WorkspaceStartError(path=Path(self.state.manifest.root), cause=e) from e

        await super().start()

    async def stop(self) -> None:
        await super().stop()

    async def shutdown(self) -> None:
        # Best-effort kill of the remote sandbox.
        try:
            if self.state.pause_on_exit:
                await _maybe_await(_sandbox_pause(self._sandbox))
            else:
                await _maybe_await(_sandbox_kill(self._sandbox))
        except Exception:
            if self.state.pause_on_exit:
                try:
                    await _maybe_await(_sandbox_kill(self._sandbox))
                except Exception:
                    pass
            else:
                pass

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        command_list = [str(c) for c in command]
        envs = await self._resolved_envs()
        cwd = self.state.manifest.root if self._workspace_root_ready else None
        user: str | None = None
        if command_list and command_list[0] == "sudo" and len(command_list) >= 4:
            # Handle the `sudo -u <user> -- ...` prefix introduced by SandboxSession.exec.
            if command_list[1] == "-u" and command_list[3] == "--":
                user = command_list[2]
                command_list = command_list[4:]

        cmd_str = shlex.join(command_list)
        exec_timeout = self._coerce_exec_timeout(timeout)

        e2b_exc = _import_e2b_exceptions()
        timeout_exc = e2b_exc.get("timeout")
        command_exit_exc = _import_command_exit_exception()

        try:
            result = await _maybe_await(
                _sandbox_run_command(
                    self._sandbox,
                    cmd_str,
                    timeout=exec_timeout,
                    cwd=cwd,
                    envs=envs,
                    user=user,
                )
            )
            return ExecResult(
                stdout=str(getattr(result, "stdout", "") or "").encode("utf-8", errors="replace"),
                stderr=str(getattr(result, "stderr", "") or "").encode("utf-8", errors="replace"),
                exit_code=int(getattr(result, "exit_code", 0) or 0),
            )
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            if timeout_exc is not None and isinstance(e, timeout_exc):
                raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e

            if command_exit_exc is not None and isinstance(e, command_exit_exc):
                exit_code = int(getattr(e, "exit_code", 1) or 1)
                stdout = str(getattr(e, "stdout", "") or "")
                stderr = str(getattr(e, "stderr", "") or "")
                return ExecResult(
                    stdout=stdout.encode("utf-8", errors="replace"),
                    stderr=stderr.encode("utf-8", errors="replace"),
                    exit_code=exit_code,
                )

            raise ExecTransportError(command=command, cause=e) from e

    async def read(self, path: Path) -> io.IOBase:
        workspace_path = resolve_workspace_path(
            Path(self.state.manifest.root),
            path,
            allow_absolute_within_root=True,
        )

        e2b_exc = _import_e2b_exceptions()
        not_found_exc = e2b_exc.get("not_found")

        try:
            content = await _maybe_await(
                _sandbox_read_file(self._sandbox, str(workspace_path), format="bytes")
            )
            if isinstance(content, (bytes, bytearray)):
                data = bytes(content)
            elif isinstance(content, str):
                data = content.encode("utf-8", errors="replace")
            else:
                data = str(content).encode("utf-8", errors="replace")
            return io.BytesIO(data)
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            if not_found_exc is not None and isinstance(e, not_found_exc):
                raise WorkspaceReadNotFoundError(path=path, cause=e) from e
            raise WorkspaceArchiveReadError(path=path, cause=e) from e

    async def write(self, path: Path, data: io.IOBase) -> None:
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=path, actual_type=type(payload).__name__)

        workspace_path = resolve_workspace_path(
            Path(self.state.manifest.root),
            path,
            allow_absolute_within_root=True,
        )

        try:
            await _maybe_await(
                _sandbox_write_file(
                    self._sandbox,
                    str(workspace_path),
                    bytes(payload),
                    request_timeout=self.state.timeouts.file_upload_s,
                )
            )
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        if not self._workspace_root_ready:
            return False
        try:
            return bool(
                await _maybe_await(
                    _sandbox_is_running(
                        self._sandbox,
                        request_timeout=self.state.timeouts.keepalive_s,
                    )
                )
            )
        except Exception:
            return False

    async def mkdir(self, path: Path | str, *, parents: bool = False) -> None:
        path = self.normalize_path(path)
        if not parents:
            parent = path.parent
            test = await self.exec("test", "-d", str(parent), shell=False)
            if not test.ok():
                raise ExecNonZeroError(test, command=("test", "-d", str(parent)))
        await self._ensure_dir(path, reason="mkdir_failed")

    def _tar_exclude_args(self) -> list[str]:
        excludes: list[str] = []
        for rel in sorted(
            self.state.manifest.ephemeral_persistence_paths(), key=lambda p: p.as_posix()
        ):
            rel_posix = rel.as_posix().lstrip("/")
            if not rel_posix or rel_posix in {".", "/"}:
                continue
            excludes.append(f"--exclude={shlex.quote(rel_posix)}")
            excludes.append(f"--exclude={shlex.quote(f'./{rel_posix}')}")
        return excludes

    @retry_async(
        retry_if=lambda exc, self, tar_cmd: exception_chain_contains_type(
            exc, _retryable_persist_workspace_error_types()
        )
        or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
    )
    async def _run_persist_workspace_command(self, tar_cmd: str) -> str:
        try:
            envs = await self._resolved_envs()
            result = await _maybe_await(
                _sandbox_run_command(
                    self._sandbox,
                    tar_cmd,
                    timeout=self.state.timeouts.snapshot_tar_s,
                    cwd="/",
                    envs=envs,
                )
            )
            exit_code = int(getattr(result, "exit_code", 0) or 0)
            if exit_code != 0:
                raise WorkspaceArchiveReadError(
                    path=Path(self.state.manifest.root),
                    context={
                        "reason": "snapshot_nonzero_exit",
                        "exit_code": exit_code,
                        "stderr": str(getattr(result, "stderr", "") or ""),
                    },
                )
            return str(getattr(result, "stdout", "") or "")
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            raise WorkspaceArchiveReadError(path=Path(self.state.manifest.root), cause=e) from e

    async def persist_workspace(self) -> io.IOBase:
        def _error_context_summary(error: WorkspaceArchiveReadError) -> dict[str, str]:
            summary = {"message": error.message}
            if error.cause is not None:
                summary["cause_type"] = type(error.cause).__name__
                summary["cause"] = str(error.cause)
            return summary

        root = Path(self.state.manifest.root)
        excludes = " ".join(self._tar_exclude_args())
        tar_cmd = f"tar {excludes} -C {shlex.quote(str(root))} -cf - . | base64 -w0"
        unmounted_mounts: list[tuple[Mount, Path]] = []
        unmount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in self.state.manifest.ephemeral_mount_targets():
            try:
                await mount_entry.unmount_path(self, mount_path)
            except Exception as e:
                unmount_error = WorkspaceArchiveReadError(path=root, cause=e)
                break
            unmounted_mounts.append((mount_entry, mount_path))

        snapshot_error: WorkspaceArchiveReadError | None = None
        raw: bytes | None = None
        if unmount_error is None:
            try:
                encoded = await self._run_persist_workspace_command(tar_cmd)
                try:
                    raw = base64.b64decode(encoded.encode("utf-8"), validate=True)
                except (binascii.Error, ValueError) as e:
                    raise WorkspaceArchiveReadError(
                        path=root,
                        context={"reason": "snapshot_invalid_base64"},
                        cause=e,
                    ) from e
            except WorkspaceArchiveReadError as e:
                snapshot_error = e

        remount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in reversed(unmounted_mounts):
            try:
                await mount_entry.mount(self, mount_path)
            except Exception as e:
                current_error = WorkspaceArchiveReadError(path=root, cause=e)
                if remount_error is None:
                    remount_error = current_error
                    if unmount_error is not None:
                        remount_error.context["earlier_unmount_error"] = _error_context_summary(
                            unmount_error
                        )
                else:
                    additional_remount_errors = remount_error.context.setdefault(
                        "additional_remount_errors", []
                    )
                    assert isinstance(additional_remount_errors, list)
                    additional_remount_errors.append(_error_context_summary(current_error))

        if remount_error is not None:
            if snapshot_error is not None:
                remount_error.context["snapshot_error_before_remount_corruption"] = (
                    _error_context_summary(snapshot_error)
                )
            raise remount_error
        if unmount_error is not None:
            raise unmount_error
        if snapshot_error is not None:
            raise snapshot_error

        assert raw is not None
        return io.BytesIO(raw)

    def _validate_tar_bytes(self, raw: bytes) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
                for member in tar.getmembers():
                    name = member.name
                    if name in ("", ".", "./"):
                        continue
                    rel = PurePosixPath(name)
                    if rel.is_absolute():
                        raise ValueError(f"absolute path member: {name}")
                    if ".." in rel.parts:
                        raise ValueError(f"parent traversal member: {name}")
                    if member.issym() or member.islnk():
                        raise ValueError(f"link member not allowed: {name}")
                    if not (member.isdir() or member.isreg()):
                        raise ValueError(f"unsupported member type: {name}")
        except (tarfile.TarError, OSError) as e:
            raise ValueError("invalid tar stream") from e

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = Path(self.state.manifest.root)
        tar_path = f"/tmp/uc-hydrate-{self.state.session_id.hex}.tar"

        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=Path(tar_path), actual_type=type(raw).__name__)

        try:
            self._validate_tar_bytes(bytes(raw))
        except ValueError as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={"reason": "unsafe_or_invalid_tar", "detail": str(e)},
                cause=e,
            ) from e

        try:
            await self._ensure_workspace_root()
            envs = await self._resolved_envs()
            await _maybe_await(
                _sandbox_write_file(
                    self._sandbox,
                    tar_path,
                    bytes(raw),
                    request_timeout=self.state.timeouts.file_upload_s,
                )
            )
            result = await _maybe_await(
                _sandbox_run_command(
                    self._sandbox,
                    f"tar -C {shlex.quote(str(root))} -xf {shlex.quote(tar_path)}",
                    timeout=self.state.timeouts.snapshot_tar_s,
                    cwd="/",
                    envs=envs,
                )
            )
            exit_code = int(getattr(result, "exit_code", 0) or 0)
            if exit_code != 0:
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "reason": "hydrate_nonzero_exit",
                        "exit_code": exit_code,
                        "stderr": str(getattr(result, "stderr", "") or ""),
                    },
                )
            self._workspace_root_ready = True
            self.state.workspace_root_ready = True
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e
        finally:
            try:
                envs = await self._resolved_envs()
                await _maybe_await(
                    _sandbox_run_command(
                        self._sandbox,
                        f"rm -f -- {shlex.quote(tar_path)}",
                        timeout=self.state.timeouts.cleanup_s,
                        cwd="/",
                        envs=envs,
                    )
                )
            except Exception:
                pass


class E2BSandboxClient(BaseSandboxClient[E2BSandboxClientOptions]):
    backend_id = "e2b"
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | None = None,
        manifest: Manifest | None = None,
        codex: bool | CodexConfig = False,
        options: E2BSandboxClientOptions,
    ) -> SandboxSession:
        if options is None:
            raise ValueError("E2BSandboxClient.create requires options")

        manifest = apply_codex_to_manifest(manifest, codex)
        sandbox_type = _coerce_sandbox_type(options.sandbox_type)

        timeouts_in = options.timeouts
        if isinstance(timeouts_in, E2BSandboxTimeouts):
            timeouts = timeouts_in
        elif timeouts_in is None:
            timeouts = E2BSandboxTimeouts()
        else:
            timeouts = E2BSandboxTimeouts.model_validate(timeouts_in)

        base_envs = dict(options.envs or {})
        manifest_envs = await manifest.environment.resolve()
        envs = {**base_envs, **manifest_envs} or None

        SandboxClass = _import_sandbox_class(sandbox_type)
        sandbox = await _maybe_await(
            _sandbox_create(
                SandboxClass,
                template=options.template,
                timeout=options.timeout,
                metadata=options.metadata,
                envs=envs,
                secure=options.secure,
                allow_internet_access=options.allow_internet_access,
            )
        )

        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = E2BSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            sandbox_id=str(_sandbox_id(sandbox)),
            sandbox_type=sandbox_type,
            template=options.template,
            sandbox_timeout=options.timeout,
            metadata=options.metadata,
            base_envs=base_envs,
            secure=options.secure,
            allow_internet_access=options.allow_internet_access,
            timeouts=timeouts,
            pause_on_exit=options.pause_on_exit,
        )
        inner = E2BSandboxSession.from_state(state, sandbox=sandbox)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, E2BSandboxSession):
            raise TypeError("E2BSandboxClient.delete expects an E2BSandboxSession")
        return session

    async def resume(
        self,
        state: SandboxSessionState,
        *,
        codex: bool | CodexConfig = False,
    ) -> SandboxSession:
        if not isinstance(state, E2BSandboxSessionState):
            raise TypeError("E2BSandboxClient.resume expects an E2BSandboxSessionState")
        state = apply_codex_to_session_state(state, codex)

        sandbox_type = _coerce_sandbox_type(state.sandbox_type)
        SandboxClass = _import_sandbox_class(sandbox_type)

        base_envs = dict(state.base_envs)
        manifest_envs = await state.manifest.environment.resolve()
        envs = {**base_envs, **manifest_envs} or None

        sandbox: object
        reconnected = False
        try:
            # `_cls_connect` is the current async entrypoint for re-attaching to a sandbox id.
            sandbox = await _maybe_await(
                _sandbox_connect(
                    SandboxClass,
                    sandbox_id=state.sandbox_id,
                    timeout=state.sandbox_timeout,
                )
            )
            if not state.pause_on_exit:
                is_running = await _maybe_await(
                    _sandbox_is_running(sandbox, request_timeout=state.timeouts.keepalive_s)
                )
                if not is_running:
                    raise RuntimeError("sandbox_not_running")
            reconnected = True
        except Exception:
            sandbox = await _maybe_await(
                _sandbox_create(
                    SandboxClass,
                    template=state.template,
                    timeout=state.sandbox_timeout,
                    metadata=state.metadata,
                    envs=envs,
                    secure=state.secure,
                    allow_internet_access=state.allow_internet_access,
                )
            )
            state.sandbox_id = str(_sandbox_id(sandbox))

        inner = E2BSandboxSession.from_state(state, sandbox=sandbox)
        inner._resume_preserves_system_state = reconnected
        if state.pause_on_exit and reconnected:
            inner._skip_start = True
        else:
            inner._skip_start = False
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return E2BSandboxSessionState.model_validate(payload)


__all__ = [
    "E2BSandboxClient",
    "E2BSandboxClientOptions",
    "E2BSandboxSession",
    "E2BSandboxSessionState",
    "E2BSandboxTimeouts",
    "E2BSandboxType",
]

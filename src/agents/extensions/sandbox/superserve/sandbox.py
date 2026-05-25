"""
Superserve sandbox (https://superserve.ai) implementation.

This module provides a Superserve-backed sandbox client/session implementation backed by
`superserve.AsyncSandbox`.

The `superserve` dependency is optional, so package-level exports should guard imports of this
module. Within this module, Superserve SDK imports happen lazily so users without the extra can
still import the package.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecNonZeroError,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
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
from ....sandbox.session.mount_lifecycle import with_ephemeral_mounts_removed
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.retry import (
    exception_chain_contains_type,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import (
    coerce_posix_path,
    posix_path_as_path,
    sandbox_path_str,
)

DEFAULT_SUPERSERVE_WORKSPACE_ROOT = "/workspace"
DEFAULT_SUPERSERVE_TEMPLATE = "superserve/base"
_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)
_SUPERSERVE_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
_SUPERSERVE_ACTIVE_STATUSES: frozenset[str] = frozenset({"active"})
_SUPERSERVE_RESUMING_STATUSES: frozenset[str] = frozenset({"paused", "resuming"})
_SUPERSERVE_TERMINAL_STATUSES: frozenset[str] = frozenset({"failed"})

logger = logging.getLogger(__name__)


def _import_superserve_sdk() -> tuple[Any, Any]:
    """Lazily import Superserve SDK classes, raising a clear error if missing."""
    try:
        from superserve import AsyncSandbox, NetworkConfig

        return AsyncSandbox, NetworkConfig
    except ImportError as exc:
        raise ImportError(
            "SuperserveSandboxClient requires the optional `superserve` dependency.\n"
            "Install the Superserve extra before using this sandbox backend."
        ) from exc


def _import_superserve_errors() -> dict[str, type[BaseException]]:
    """Best-effort import of Superserve exception classes for fine-grained mapping."""
    try:
        from superserve import (
            AuthenticationError,
            ConflictError,
            NotFoundError,
            SandboxError,
            SandboxTimeoutError,
            ServerError,
            ValidationError,
        )
    except Exception:
        return {}
    return {
        "base": SandboxError,
        "authentication": AuthenticationError,
        "validation": ValidationError,
        "not_found": NotFoundError,
        "conflict": ConflictError,
        "timeout": SandboxTimeoutError,
        "server": ServerError,
    }


def _provider_error_detail(error: BaseException) -> str | None:
    message = str(error)
    status = getattr(error, "status_code", None)
    code = getattr(error, "code", None)
    parts: list[str] = []
    if isinstance(status, int):
        parts.append(f"HTTP {status}")
    if isinstance(code, str) and code:
        parts.append(code)
    if message:
        parts.append(message)
    if not parts:
        return type(error).__name__
    return ": ".join(parts)


def _superserve_error_context(error: BaseException) -> dict[str, object]:
    """Structured error context — split status/code/message so consumers don't parse strings."""
    context: dict[str, object] = {
        "backend": "superserve",
        "cause_type": type(error).__name__,
    }
    message = str(error)
    if message:
        context["provider_message"] = message
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        context["http_status"] = status
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        context["provider_code"] = code
    return context


def _superserve_exec_transport_error(
    *,
    command: tuple[str | Path, ...],
    cause: BaseException,
    sandbox_id: str | None = None,
) -> ExecTransportError:
    context = _superserve_error_context(cause)
    if sandbox_id:
        context["sandbox_id"] = sandbox_id
    detail = _provider_error_detail(cause)
    message = "Superserve exec failed"
    if detail:
        message = f"{message}: {detail}"
    return ExecTransportError(command=command, context=context, cause=cause, message=message)


def _is_superserve_conflict(error: BaseException, conflict_exc: type[BaseException] | None) -> bool:
    if conflict_exc is not None and isinstance(error, conflict_exc):
        return True
    return exception_chain_has_status_code(error, frozenset({409}))


def _is_transient_error(exc: BaseException) -> bool:
    return exception_chain_has_status_code(
        exc, _SUPERSERVE_TRANSIENT_STATUS_CODES
    ) or exception_chain_contains_type(exc, (asyncio.TimeoutError,))


def _resolve_manifest_root(manifest: Manifest | None) -> Manifest:
    """Resolve the manifest root for a Superserve sandbox.

    - No manifest → fresh manifest rooted at `/workspace`.
    - Manifest whose root is the SDK's default placeholder (`Manifest.model_fields["root"].default`)
      → rewrite the root to the Superserve default `/workspace` for ergonomics.
    - Caller-provided non-default root (anywhere on the filesystem) → keep verbatim. Arbitrary
      roots are accepted so callers can stage work outside `/workspace` deliberately. For
      confinement, set extra path grants on the manifest.
    """
    if manifest is None:
        return Manifest(root=DEFAULT_SUPERSERVE_WORKSPACE_ROOT)
    if manifest.root == _DEFAULT_MANIFEST_ROOT:
        return manifest.model_copy(update={"root": DEFAULT_SUPERSERVE_WORKSPACE_ROOT})
    return manifest


def _resolve_template(value: str | None) -> str:
    return value or DEFAULT_SUPERSERVE_TEMPLATE


class SuperserveSandboxTimeouts(BaseModel):
    """Timeout configuration for Superserve sandbox operations (seconds)."""

    model_config = {"frozen": True}

    exec_timeout_unbounded_s: int = Field(default=24 * 60 * 60, ge=1)
    keepalive_s: int = Field(default=10, ge=1)
    cleanup_s: int = Field(default=30, ge=1)
    fast_op_s: int = Field(default=30, ge=1)
    file_upload_s: int = Field(default=300, ge=1)
    file_download_s: int = Field(default=300, ge=1)
    workspace_tar_s: int = Field(default=300, ge=1)
    resume_ready_timeout_s: int = Field(default=60, ge=1)
    resume_ready_poll_interval_s: float = Field(default=1.0, gt=0)


class SuperserveSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Superserve sandbox backend."""

    type: Literal["superserve"] = "superserve"
    template: str | None = None
    name: str | None = None
    env_vars: dict[str, str] | None = None
    metadata: dict[str, str] | None = None
    network: dict[str, object] | None = None
    timeout_seconds: int | None = None
    pause_on_exit: bool = False
    api_key: str | None = None
    base_url: str | None = None
    exposed_ports: tuple[int, ...] = ()
    timeouts: SuperserveSandboxTimeouts | dict[str, object] | None = None

    def __init__(
        self,
        template: str | None = None,
        name: str | None = None,
        env_vars: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
        network: dict[str, object] | None = None,
        timeout_seconds: int | None = None,
        pause_on_exit: bool = False,
        api_key: str | None = None,
        base_url: str | None = None,
        exposed_ports: tuple[int, ...] = (),
        timeouts: SuperserveSandboxTimeouts | dict[str, object] | None = None,
        *,
        type: Literal["superserve"] = "superserve",
    ) -> None:
        super().__init__(
            type=type,
            template=template,
            name=name,
            env_vars=env_vars,
            metadata=metadata,
            network=network,
            timeout_seconds=timeout_seconds,
            pause_on_exit=pause_on_exit,
            api_key=api_key,
            base_url=base_url,
            exposed_ports=exposed_ports,
            timeouts=timeouts,
        )


class SuperserveSandboxSessionState(SandboxSessionState):
    """Serializable state for a Superserve-backed session."""

    type: Literal["superserve"] = "superserve"
    sandbox_id: str
    template: str = DEFAULT_SUPERSERVE_TEMPLATE
    name: str | None = None
    base_env_vars: dict[str, str] = Field(default_factory=dict)
    base_metadata: dict[str, str] = Field(default_factory=dict)
    base_network: dict[str, object] | None = None
    timeout_seconds: int | None = None
    pause_on_exit: bool = False
    base_url: str | None = None
    api_key: str | None = None
    timeouts: SuperserveSandboxTimeouts = Field(default_factory=SuperserveSandboxTimeouts)


class SuperserveSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by a Superserve sandbox."""

    state: SuperserveSandboxSessionState
    _sandbox: Any | None

    def __init__(
        self,
        *,
        state: SuperserveSandboxSessionState,
        sandbox: Any | None = None,
    ) -> None:
        self.state = state
        self._sandbox = sandbox

    @classmethod
    def from_state(
        cls,
        state: SuperserveSandboxSessionState,
        *,
        sandbox: Any | None = None,
    ) -> SuperserveSandboxSession:
        return cls(state=state, sandbox=sandbox)

    @property
    def sandbox_id(self) -> str:
        return self.state.sandbox_id

    def supports_pty(self) -> bool:
        return False

    def _reject_user_arg(
        self, *, op: Literal["exec", "read", "write"], user: str | User
    ) -> None:
        user_name = user.name if isinstance(user, User) else user
        raise ConfigurationError(
            message=(
                "SuperserveSandboxSession does not support sandbox-local users; "
                f"`{op}` must be called without `user`"
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op=op,
            context={"backend": "superserve", "user": user_name},
        )

    def _prepare_exec_command(
        self,
        *command: str | Path,
        shell: bool | list[str],
        user: str | User | None,
    ) -> list[str]:
        if user is not None:
            self._reject_user_arg(op="exec", user=user)
        return super()._prepare_exec_command(*command, shell=shell, user=user)

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        """Invalidate helper-script cache when the backing sandbox is swapped on resume."""
        return self.state.sandbox_id or None

    async def _resolved_envs(self) -> dict[str, str]:
        manifest_envs = await self.state.manifest.environment.resolve()
        resolved: dict[str, str] = {}
        for key, value in {**self.state.base_env_vars, **manifest_envs}.items():
            if value is None:
                continue
            resolved[key] = value
        return resolved

    async def _ensure_sandbox(self) -> Any:
        sandbox = self._sandbox
        if sandbox is not None:
            return sandbox

        AsyncSandbox, NetworkConfig = _import_superserve_sdk()
        sup_errors = _import_superserve_errors()
        conflict_exc = sup_errors.get("conflict")
        env_vars = await self._resolved_envs()
        network_payload = self.state.base_network
        network = (
            NetworkConfig.model_validate(network_payload) if network_payload is not None else None
        )
        try:
            sandbox = await AsyncSandbox.create(
                name=self.state.name or self.state.session_id.hex,
                from_template=self.state.template,
                timeout_seconds=self.state.timeout_seconds,
                metadata=dict(self.state.base_metadata) or None,
                env_vars=env_vars or None,
                network=network,
                api_key=self.state.api_key,
                base_url=self.state.base_url,
            )
        except Exception as exc:
            reason = (
                "name_collision" if _is_superserve_conflict(exc, conflict_exc) else "create_failed"
            )
            context = _superserve_error_context(exc)
            context["reason"] = reason
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context=context,
                cause=exc,
                message=f"failed to start Superserve sandbox: {_provider_error_detail(exc)}",
            ) from exc

        self._sandbox = sandbox
        self.state.sandbox_id = sandbox.id
        return sandbox

    async def _wait_until_active(
        self,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        """Poll get_info() until status is `active`, or raise.

        Used after `await sandbox.resume()` to guarantee the sandbox is ready before the caller
        runs the first exec. Superserve's resume() returns once the API has accepted the request;
        the sandbox may still be in `resuming` for a short window.
        """
        sandbox = self._sandbox
        if sandbox is None:
            return
        deadline = time.monotonic() + (timeout_s or self.state.timeouts.resume_ready_timeout_s)
        interval = poll_interval_s or self.state.timeouts.resume_ready_poll_interval_s
        last_status: str | None = None
        while True:
            try:
                info = await asyncio.wait_for(
                    sandbox.get_info(),
                    timeout=self.state.timeouts.keepalive_s,
                )
            except Exception as exc:
                raise WorkspaceStartError(
                    path=self._workspace_root_path(),
                    context=_superserve_error_context(exc) | {"reason": "wait_until_active_failed"},
                    cause=exc,
                    message=f"failed to confirm sandbox active: {_provider_error_detail(exc)}",
                ) from exc
            status = getattr(info, "status", None)
            last_status = getattr(status, "value", status)
            if last_status in _SUPERSERVE_ACTIVE_STATUSES:
                return
            if last_status in _SUPERSERVE_TERMINAL_STATUSES:
                raise WorkspaceStartError(
                    path=self._workspace_root_path(),
                    context={
                        "backend": "superserve",
                        "reason": "sandbox_failed_during_resume",
                        "sandbox_status": last_status,
                    },
                    message=(
                        f"sandbox reached terminal status {last_status!r} during resume"
                    ),
                )
            if time.monotonic() >= deadline:
                raise WorkspaceStartError(
                    path=self._workspace_root_path(),
                    context={
                        "backend": "superserve",
                        "reason": "wait_until_active_timeout",
                        "sandbox_status": last_status,
                        "timeout_s": timeout_s or self.state.timeouts.resume_ready_timeout_s,
                    },
                    message=(
                        f"sandbox did not become active within "
                        f"{timeout_s or self.state.timeouts.resume_ready_timeout_s}s "
                        f"(last status: {last_status!r})"
                    ),
                )
            await asyncio.sleep(interval)

    async def _prepare_backend_workspace(self) -> None:
        root = self._workspace_root_path()
        sandbox = await self._ensure_sandbox()
        try:
            result = await sandbox.commands.run(
                f"mkdir -p -- {shlex.quote(root.as_posix())}",
                timeout_seconds=self.state.timeouts.fast_op_s,
            )
        except Exception as exc:
            context = _superserve_error_context(exc)
            context["reason"] = "workspace_root_setup_failed"
            raise WorkspaceStartError(
                path=root,
                context=context,
                cause=exc,
                message=(
                    "failed to start session: Superserve workspace root setup failed: "
                    f"{_provider_error_detail(exc)}"
                ),
            ) from exc

        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            raise WorkspaceStartError(
                path=root,
                context={
                    "backend": "superserve",
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                },
                message=(
                    f"failed to start session: Superserve workspace root setup exited with "
                    f"{exit_code}"
                ),
            )

    async def running(self) -> bool:
        sandbox = self._sandbox
        if sandbox is None:
            return False
        try:
            info = await asyncio.wait_for(
                sandbox.get_info(),
                timeout=self.state.timeouts.keepalive_s,
            )
        except Exception:
            return False
        status = getattr(info, "status", None)
        status_value = getattr(status, "value", status)
        return status_value == "active"

    async def shutdown(self) -> None:
        await self._shutdown_backend()

    async def _shutdown_backend(self) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            if self.state.pause_on_exit:
                await sandbox.pause()
            else:
                await sandbox.kill()
        except Exception:
            pass
        finally:
            self._sandbox = None

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        sandbox = await self._ensure_sandbox()
        sup_errors = _import_superserve_errors()
        timeout_exc = sup_errors.get("timeout")
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        command_str = shlex.join(normalized)
        envs = await self._resolved_envs()
        cwd = sandbox_path_str(self.state.manifest.root)
        # Superserve accepts only int seconds; round up so we never undershoot the caller.
        timeout_seconds = None if timeout is None else max(1, math.ceil(timeout))

        try:
            result = await sandbox.commands.run(
                command_str,
                cwd=cwd,
                env=envs or None,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ExecTimeoutError(
                command=tuple(normalized), timeout_s=timeout, cause=exc
            ) from exc
        except Exception as exc:
            if timeout_exc is not None and isinstance(exc, timeout_exc):
                raise ExecTimeoutError(
                    command=tuple(normalized), timeout_s=timeout, cause=exc
                ) from exc
            raise _superserve_exec_transport_error(
                command=tuple(normalized),
                cause=exc,
                sandbox_id=self.state.sandbox_id,
            ) from exc

        stdout = (getattr(result, "stdout", "") or "").encode("utf-8", errors="replace")
        stderr = (getattr(result, "stderr", "") or "").encode("utf-8", errors="replace")
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        raise ExposedPortUnavailableError(
            port=port,
            exposed_ports=self.state.exposed_ports,
            reason="backend_unavailable",
            context={
                "backend": "superserve",
                "detail": "exposed_ports_not_supported",
            },
        )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            self._reject_user_arg(op="read", user=user)
        sup_errors = _import_superserve_errors()
        not_found_exc = sup_errors.get("not_found")

        normalized_path = await self._validate_path_access(path)
        sandbox = await self._ensure_sandbox()
        try:
            payload = await sandbox.files.read(
                sandbox_path_str(normalized_path),
                timeout=self.state.timeouts.file_download_s,
            )
        except Exception as exc:
            if not_found_exc is not None and isinstance(exc, not_found_exc):
                raise WorkspaceReadNotFoundError(path=normalized_path, cause=exc) from exc
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc
        return io.BytesIO(payload)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            self._reject_user_arg(op="write", user=user)

        normalized_path = await self._validate_path_access(path, for_write=True)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=normalized_path,
                actual_type=type(payload).__name__,
            )
        try:
            await self._write_bytes_with_retry(
                sandbox_path_str(normalized_path), bytes(payload)
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc

    @retry_async(
        retry_if=lambda exc, self, _path, _data: _is_transient_error(exc),
    )
    async def _write_bytes_with_retry(self, path: str, data: bytes) -> None:
        sandbox = await self._ensure_sandbox()
        await sandbox.files.write(path, data, timeout=self.state.timeouts.file_upload_s)

    async def persist_workspace(self) -> io.IOBase:
        return await with_ephemeral_mounts_removed(
            self,
            self._persist_workspace_with_retry,
            error_path=self._workspace_root_path(),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key="snapshot_error_before_remount_corruption",
        )

    @retry_async(retry_if=lambda exc, self: _is_transient_error(exc))
    async def _persist_workspace_with_retry(self) -> io.IOBase:
        return await self._persist_workspace_internal()

    async def _persist_workspace_internal(self) -> io.IOBase:
        root = self._workspace_root_path()
        archive_path = posix_path_as_path(
            coerce_posix_path(
                f"/tmp/openai-agents-persist-{self.state.session_id.hex}.tar"
            )
        )
        excludes = [
            f"--exclude=./{rel_path.as_posix()}"
            for rel_path in sorted(
                self._persist_workspace_skip_relpaths(),
                key=lambda item: item.as_posix(),
            )
        ]
        tar_command = ("tar", "cf", archive_path.as_posix(), *excludes, ".")

        sandbox = await self._ensure_sandbox()
        sup_errors = _import_superserve_errors()
        not_found_exc = sup_errors.get("not_found")

        try:
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_command,
                        context={
                            "backend": "superserve",
                            "sandbox_id": self.state.sandbox_id,
                        },
                    ),
                )

            try:
                archive = await sandbox.files.read(
                    archive_path.as_posix(),
                    timeout=self.state.timeouts.file_download_s,
                )
            except Exception as exc:
                if not_found_exc is not None and isinstance(exc, not_found_exc):
                    raise WorkspaceReadNotFoundError(path=archive_path, cause=exc) from exc
                raise

            return io.BytesIO(archive)
        except (WorkspaceArchiveReadError, WorkspaceReadNotFoundError):
            raise
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=root, cause=exc) from exc
        finally:
            try:
                await self.exec(
                    "rm",
                    "-f",
                    "--",
                    archive_path.as_posix(),
                    shell=False,
                )
            except Exception:
                pass

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=self._workspace_root_path(),
                actual_type=type(raw).__name__,
            )

        await with_ephemeral_mounts_removed(
            self,
            lambda: self._hydrate_workspace_with_retry(bytes(raw)),
            error_path=self._workspace_root_path(),
            error_cls=WorkspaceArchiveWriteError,
            operation_error_context_key="hydrate_error_before_remount_corruption",
        )

    @retry_async(retry_if=lambda exc, self, _raw: _is_transient_error(exc))
    async def _hydrate_workspace_with_retry(self, raw: bytes) -> None:
        await self._hydrate_workspace_internal(raw)

    async def _hydrate_workspace_internal(self, raw: bytes) -> None:
        root = self._workspace_root_path()
        archive_path = posix_path_as_path(
            coerce_posix_path(
                f"/tmp/openai-agents-hydrate-{self.state.session_id.hex}.tar"
            )
        )
        tar_command = ("tar", "xf", archive_path.as_posix(), "-C", root.as_posix())

        try:
            validate_tar_bytes(raw, allow_external_symlink_targets=False)
        except UnsafeTarMemberError as exc:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "unsafe_or_invalid_tar",
                    "member": exc.member,
                    "detail": str(exc),
                },
                cause=exc,
            ) from exc

        try:
            await self.mkdir(root, parents=True)
            await self._write_bytes_with_retry(archive_path.as_posix(), raw)
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_command,
                        context={
                            "backend": "superserve",
                            "sandbox_id": self.state.sandbox_id,
                        },
                    ),
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=root, cause=exc) from exc
        finally:
            try:
                await self.exec(
                    "rm",
                    "-f",
                    "--",
                    archive_path.as_posix(),
                    shell=False,
                )
            except Exception:
                pass


class SuperserveSandboxClient(BaseSandboxClient[SuperserveSandboxClientOptions]):
    """Superserve-backed sandbox client managing sandbox lifecycle via AsyncSandbox."""

    backend_id = "superserve"
    _instrumentation: Instrumentation
    _api_key: str | None
    _base_url: str | None

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    def _resolve_timeouts(
        self,
        value: SuperserveSandboxTimeouts | dict[str, object] | None,
    ) -> SuperserveSandboxTimeouts:
        if isinstance(value, SuperserveSandboxTimeouts):
            return value
        if value is None:
            return SuperserveSandboxTimeouts()
        return SuperserveSandboxTimeouts.model_validate(value)

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: SuperserveSandboxClientOptions,
    ) -> SandboxSession:
        resolved_manifest = _resolve_manifest_root(manifest)
        timeouts = self._resolve_timeouts(options.timeouts)
        api_key = options.api_key or self._api_key
        base_url = options.base_url or self._base_url
        template = _resolve_template(options.template)

        session_id = uuid.uuid4()
        sandbox_name = options.name or f"openai-agents-{session_id.hex[:12]}"
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))

        state = SuperserveSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sandbox_id="",
            template=template,
            name=sandbox_name,
            base_env_vars=dict(options.env_vars or {}),
            base_metadata=dict(options.metadata or {}),
            base_network=dict(options.network) if options.network is not None else None,
            timeout_seconds=options.timeout_seconds,
            pause_on_exit=options.pause_on_exit,
            base_url=base_url,
            api_key=api_key,
            timeouts=timeouts,
            exposed_ports=options.exposed_ports,
        )
        inner = SuperserveSandboxSession.from_state(state)
        await inner._ensure_sandbox()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, SuperserveSandboxSession):
            raise TypeError(
                "SuperserveSandboxClient.delete expects a SuperserveSandboxSession"
            )
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, SuperserveSandboxSessionState):
            raise TypeError(
                "SuperserveSandboxClient.resume expects a SuperserveSandboxSessionState"
            )

        AsyncSandbox, _ = _import_superserve_sdk()
        sup_errors = _import_superserve_errors()
        not_found_exc = sup_errors.get("not_found")

        api_key = state.api_key or self._api_key
        base_url = state.base_url or self._base_url
        if state.api_key is None and api_key is not None:
            state.api_key = api_key
        if state.base_url is None and base_url is not None:
            state.base_url = base_url

        sandbox: Any | None = None
        reconnected = False

        if state.sandbox_id:
            sandbox, reconnected = await self._reattach_sandbox(
                AsyncSandbox=AsyncSandbox,
                state=state,
                api_key=api_key,
                base_url=base_url,
                not_found_exc=not_found_exc,
            )

        if sandbox is None:
            state.sandbox_id = ""
            state.workspace_root_ready = False

        inner = SuperserveSandboxSession.from_state(state, sandbox=sandbox)
        if sandbox is None:
            await inner._ensure_sandbox()
        inner._set_start_state_preserved(reconnected, system=reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def _reattach_sandbox(
        self,
        *,
        AsyncSandbox: Any,
        state: SuperserveSandboxSessionState,
        api_key: str | None,
        base_url: str | None,
        not_found_exc: type[BaseException] | None,
    ) -> tuple[Any | None, bool]:
        """Try to reattach to an existing Superserve sandbox by id.

        Returns (sandbox, reconnected). On any failure path, returns (None, False) so the caller
        falls back to recreating from scratch.
        """
        try:
            sandbox = await AsyncSandbox.connect(
                state.sandbox_id,
                api_key=api_key,
                base_url=base_url,
            )
        except Exception as exc:
            if not_found_exc is not None and isinstance(exc, not_found_exc):
                logger.debug(
                    "superserve sandbox %s not found, will recreate", state.sandbox_id
                )
            else:
                logger.debug(
                    "superserve connect failed for %s (will recreate): %s",
                    state.sandbox_id,
                    exc,
                )
            return None, False

        status = getattr(sandbox, "status", None)
        status_value = getattr(status, "value", status)

        if status_value in _SUPERSERVE_TERMINAL_STATUSES:
            logger.debug(
                "superserve sandbox %s is in terminal status %r; recreating",
                state.sandbox_id,
                status_value,
            )
            return None, False

        if status_value in _SUPERSERVE_RESUMING_STATUSES:
            # Only call resume() if the sandbox is paused; for `resuming` just wait. Calling
            # resume() while resume is in flight typically 409s on the API.
            if status_value == "paused":
                try:
                    await sandbox.resume()
                except Exception as exc:
                    logger.debug(
                        "superserve resume() failed for %s, will recreate: %s",
                        state.sandbox_id,
                        exc,
                    )
                    return None, False

            probe = SuperserveSandboxSession.from_state(state, sandbox=sandbox)
            try:
                await probe._wait_until_active()
            except WorkspaceStartError as exc:
                logger.debug(
                    "superserve sandbox %s did not become active after resume: %s",
                    state.sandbox_id,
                    exc,
                )
                return None, False
            return sandbox, True

        if status_value in _SUPERSERVE_ACTIVE_STATUSES:
            return sandbox, True

        # Unknown or transitional status (e.g. "stopping", future enum values) — don't trust it.
        logger.debug(
            "superserve sandbox %s has unrecognized status %r; recreating",
            state.sandbox_id,
            status_value,
        )
        return None, False

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return SuperserveSandboxSessionState.model_validate(payload)


__all__ = [
    "DEFAULT_SUPERSERVE_TEMPLATE",
    "DEFAULT_SUPERSERVE_WORKSPACE_ROOT",
    "SuperserveSandboxClient",
    "SuperserveSandboxClientOptions",
    "SuperserveSandboxSession",
    "SuperserveSandboxSessionState",
    "SuperserveSandboxTimeouts",
]

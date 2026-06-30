"""
Islo sandbox (https://islo.dev) implementation.

This module provides an Islo-backed sandbox client/session implementation backed by
the Islo Python SDK.

The ``islo`` dependency is optional, so package-level exports should guard imports of this
module. Within this module, Islo SDK imports are lazy so users without the extra can still
import the package.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import httpx
from pydantic import BaseModel, Field

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
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
from ....sandbox.session.mount_lifecycle import with_ephemeral_mounts_removed
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.session.tar_workspace import shell_tar_exclude_args
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, User
from ....sandbox.util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_contains_type,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

WorkspacePersistenceMode = Literal["tar", "snapshot"]

DEFAULT_ISLO_WORKSPACE_ROOT = "/workspace"
_DEFAULT_ISLO_BASE_URL = "https://api.islo.dev"
_DEFAULT_ISLO_COMPUTE_URL = "https://ca.compute.islo.dev"
_ENV_ISLO_API_KEY = "ISLO_API_KEY"
_ENV_ISLO_BASE_URL = "ISLO_BASE_URL"
_ENV_ISLO_COMPUTE_URL = "ISLO_COMPUTE_URL"
_WORKSPACE_PERSISTENCE_TAR: WorkspacePersistenceMode = "tar"
_WORKSPACE_PERSISTENCE_SNAPSHOT: WorkspacePersistenceMode = "snapshot"
_ISLO_SNAPSHOT_MAGIC = b"ISLO_SANDBOX_SNAPSHOT_V1\n"

logger = logging.getLogger(__name__)


def _import_islo_sdk() -> type[Any]:
    """Lazily import the Islo async client, raising a clear error if missing."""

    try:
        from islo import AsyncIslo

        return cast(type[Any], AsyncIslo)
    except ImportError as e:
        raise ImportError(
            "IsloSandboxClient requires the optional `islo` dependency.\n"
            "Install the Islo extra before using this sandbox backend."
        ) from e


def _import_islo_exec_helper() -> Any:
    try:
        from islo.custom.exec import exec_and_wait

        return exec_and_wait
    except ImportError as e:
        raise ImportError(
            "IsloSandboxClient requires the optional `islo` dependency.\n"
            "Install the Islo extra before using this sandbox backend."
        ) from e


def _import_islo_files_internals() -> Any:
    try:
        from islo.custom.files import _async_get_client_internals

        return _async_get_client_internals
    except ImportError as e:
        raise ImportError(
            "IsloSandboxClient requires the optional `islo` dependency.\n"
            "Install the Islo extra before using this sandbox backend."
        ) from e


def _import_islo_api_error() -> type[BaseException] | None:
    try:
        from islo.core.api_error import ApiError

        return cast(type[BaseException], ApiError)
    except Exception:
        return None


def _islo_provider_error_detail(error: BaseException) -> str | None:
    message = str(error)
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int):
        if message:
            return f"HTTP {status}: {message}"
        return f"HTTP {status}"
    if message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__


def _islo_transport_error(
    *,
    command: tuple[str | Path, ...],
    cause: BaseException,
) -> ExecTransportError:
    detail = _islo_provider_error_detail(cause)
    context: dict[str, object] = {"backend": "islo"}
    if detail:
        context["provider_error"] = detail
    status = getattr(cause, "status_code", None) or getattr(cause, "status", None)
    if isinstance(status, int):
        context["http_status"] = status
    message = "Islo exec failed"
    if detail:
        message = f"{message}: {detail}"
    return ExecTransportError(command=command, context=context, cause=cause, message=message)


def _islo_workspace_error_context(resp: httpx.Response) -> dict[str, object]:
    context: dict[str, object] = {
        "backend": "islo",
        "http_status": resp.status_code,
    }
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload.get("message")
        if isinstance(detail, str) and detail:
            context["provider_error"] = detail
    elif resp.text:
        context["provider_error"] = resp.text[:1000]
    return context


def _is_not_found_error(error: BaseException) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    return status == 404 or exception_chain_has_status_code(error, {404})


def _is_name_conflict_error(error: BaseException) -> bool:
    detail = _islo_provider_error_detail(error) or ""
    return "already exists" in detail.lower()


def _is_timeout_error(error: BaseException) -> bool:
    api_error = _import_islo_api_error()
    timeout_types: list[type[BaseException]] = [
        asyncio.TimeoutError,
        TimeoutError,
        httpx.TimeoutException,
    ]
    if api_error is not None:
        timeout_types.append(api_error)
    if isinstance(error, tuple(timeout_types)):
        status = getattr(error, "status_code", None)
        return status is None or status in {408, 504}
    return exception_chain_contains_type(error, tuple(timeout_types))


def _raise_islo_exec_error(
    error: BaseException,
    *,
    command: tuple[str | Path, ...],
    timeout: float | None,
) -> NoReturn:
    if _is_timeout_error(error):
        raise ExecTimeoutError(command=command, timeout_s=timeout, cause=error) from error
    raise _islo_transport_error(command=command, cause=error) from error


def _encode_islo_snapshot_ref(*, snapshot_name: str) -> bytes:
    body = json.dumps(
        {"snapshot_name": snapshot_name}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return _ISLO_SNAPSHOT_MAGIC + body


def _decode_islo_snapshot_ref(raw: bytes) -> str | None:
    if not raw.startswith(_ISLO_SNAPSHOT_MAGIC):
        return None
    body = raw[len(_ISLO_SNAPSHOT_MAGIC) :]
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    snapshot_name = payload.get("snapshot_name") if isinstance(payload, dict) else None
    return snapshot_name if isinstance(snapshot_name, str) and snapshot_name else None


def _resolve_islo_base_url(base_url: str | None) -> str:
    return (base_url or os.environ.get(_ENV_ISLO_BASE_URL) or _DEFAULT_ISLO_BASE_URL).rstrip("/")


def _resolve_islo_compute_url(compute_url: str | None) -> str | None:
    resolved = compute_url or os.environ.get(_ENV_ISLO_COMPUTE_URL)
    return resolved.rstrip("/") if resolved else None


def _resolve_islo_api_key(api_key: str | None) -> str | None:
    return api_key or os.environ.get(_ENV_ISLO_API_KEY)


def _minimal_init_payload() -> dict[str, str]:
    return {"type": "minimal"}


def _minimal_init_kwargs(create_sandbox: Any) -> dict[str, object]:
    try:
        parameter_names = set(inspect.signature(create_sandbox).parameters)
    except (TypeError, ValueError):
        parameter_names = set()
    if "init" in parameter_names:
        return {"init": _minimal_init_payload()}
    if "init_capabilities" in parameter_names:
        return {"init_capabilities": []}
    raise ConfigurationError(
        message="Islo SDK create_sandbox does not support explicit init configuration",
        error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
        op="start",
        context={"backend": "islo"},
    )


class IsloSandboxTimeouts(BaseModel):
    """Timeout configuration for Islo sandbox operations."""

    model_config = {"frozen": True}

    exec_timeout_unbounded_s: float = Field(default=24 * 60 * 60, ge=1)
    keepalive_s: float = Field(default=10, ge=1)
    cleanup_s: float = Field(default=30, ge=1)
    file_upload_s: float = Field(default=1800, ge=1)
    file_download_s: float = Field(default=1800, ge=1)
    workspace_archive_s: float = Field(default=300, ge=1)
    snapshot_s: float = Field(default=300, ge=1)


class IsloSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Islo sandbox."""

    type: Literal["islo"] = "islo"
    base_url: str | None = None
    compute_url: str | None = None
    name: str | None = None
    image: str | None = None
    vcpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    snapshot_name: str | None = None
    env: dict[str, str] | None = None
    workdir: str | None = None
    gateway_profile: str | None = None
    cache_key: str | None = None
    pause_on_exit: bool = False
    timeouts: IsloSandboxTimeouts | dict[str, object] | None = None
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR

    def __init__(
        self,
        base_url: str | None = None,
        compute_url: str | None = None,
        name: str | None = None,
        image: str | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        snapshot_name: str | None = None,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        gateway_profile: str | None = None,
        cache_key: str | None = None,
        pause_on_exit: bool = False,
        timeouts: IsloSandboxTimeouts | dict[str, object] | None = None,
        workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR,
        *,
        type: Literal["islo"] = "islo",
    ) -> None:
        super().__init__(
            type=type,
            base_url=base_url,
            compute_url=compute_url,
            name=name,
            image=image,
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
            snapshot_name=snapshot_name,
            env=env,
            workdir=workdir,
            gateway_profile=gateway_profile,
            cache_key=cache_key,
            pause_on_exit=pause_on_exit,
            timeouts=timeouts,
            workspace_persistence=workspace_persistence,
        )


class IsloSandboxSessionState(SandboxSessionState):
    """Serializable state for an Islo-backed session."""

    type: Literal["islo"] = "islo"
    sandbox_id: str
    sandbox_name: str
    base_url: str
    compute_url: str | None = None
    name: str | None = None
    image: str | None = None
    vcpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    snapshot_name: str | None = None
    base_env: dict[str, str] = Field(default_factory=dict)
    workdir: str | None = None
    gateway_profile: str | None = None
    cache_key: str | None = None
    pause_on_exit: bool = False
    timeouts: IsloSandboxTimeouts = Field(default_factory=IsloSandboxTimeouts)
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR


class IsloSandboxSession(BaseSandboxSession):
    """Islo-backed sandbox session implementation."""

    state: IsloSandboxSessionState
    _client: Any

    def __init__(
        self,
        *,
        state: IsloSandboxSessionState,
        client: Any,
    ) -> None:
        self.state = state
        self._client = client

    @classmethod
    def from_state(
        cls,
        state: IsloSandboxSessionState,
        *,
        client: Any,
    ) -> IsloSandboxSession:
        return cls(state=state, client=client)

    async def _resolved_envs(self) -> dict[str, str]:
        manifest_envs = await self.state.manifest.environment.resolve()
        return {**self.state.base_env, **manifest_envs}

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.sandbox_name

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _coerce_exec_timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return float(self.state.timeouts.exec_timeout_unbounded_s)
        if timeout_s <= 0:
            return 0.001
        return float(timeout_s)

    async def _run_islo_command(
        self,
        command: list[str],
        *,
        timeout: float | None,
        workdir: str | None,
        envs: dict[str, str] | None,
        user: str | None = None,
    ) -> ExecResult:
        exec_and_wait = _import_islo_exec_helper()
        effective_timeout = self._coerce_exec_timeout(timeout)
        try:
            result = await exec_and_wait(
                self._client,
                self.state.sandbox_name,
                command,
                workdir=workdir,
                env=envs or None,
                user=user,
                timeout=effective_timeout,
            )
        except Exception as e:
            _raise_islo_exec_error(e, command=tuple(command), timeout=timeout)

        if bool(getattr(result, "timed_out", False)):
            raise ExecTimeoutError(command=tuple(command), timeout_s=timeout)
        return ExecResult(
            stdout=str(getattr(result, "stdout", "") or "").encode("utf-8", errors="replace"),
            stderr=str(getattr(result, "stderr", "") or "").encode("utf-8", errors="replace"),
            exit_code=int(getattr(result, "exit_code", 0) or 0),
        )

    async def _prepare_backend_workspace(self) -> None:
        root = sandbox_path_str(self.state.manifest.root)
        try:
            result = await self._run_islo_command(
                ["mkdir", "-p", "--", root],
                timeout=self.state.timeouts.keepalive_s,
                workdir="/",
                envs=await self._resolved_envs(),
            )
        except Exception as e:
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(root)), cause=e
            ) from e
        if not result.ok():
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(root)),
                context={
                    "backend": "islo",
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": result.exit_code,
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        command_list = [str(c) for c in command]
        if not command_list:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        user: str | None = None
        if len(command_list) >= 4 and command_list[0] == "sudo":
            if command_list[1] == "-u" and command_list[3] == "--":
                user = command_list[2]
                command_list = command_list[4:]

        return await self._run_islo_command(
            command_list,
            timeout=timeout,
            workdir=self.state.manifest.root,
            envs=await self._resolved_envs(),
            user=user,
        )

    async def _http_internals(self) -> tuple[str, dict[str, str]]:
        get_internals = _import_islo_files_internals()
        base_url, headers = await get_internals(self._client)
        return str(base_url).rstrip("/"), dict(headers)

    # The current Fern-generated Islo SDK lists file endpoints, but does not expose multipart
    # upload bodies or streaming download bytes. Keep the raw HTTP path isolated here until the
    # SDK grows typed file-transfer helpers.
    async def _download_file_via_http(self, path: str, *, timeout: float) -> bytes:
        base_url, headers = await self._http_internals()
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"{base_url}/sandboxes/{self.state.sandbox_name}/files",
                params={"path": path},
                headers=headers,
                timeout=timeout,
            )
        if response.status_code == 404:
            raise WorkspaceReadNotFoundError(path=posix_path_as_path(coerce_posix_path(path)))
        if response.status_code >= 400:
            raise WorkspaceArchiveReadError(
                path=posix_path_as_path(coerce_posix_path(path)),
                context=_islo_workspace_error_context(response),
            )
        return response.content

    async def _upload_file_via_http(self, path: str, payload: bytes, *, timeout: float) -> None:
        base_url, headers = await self._http_internals()
        filename = posix_path_as_path(coerce_posix_path(path)).name or "file"
        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"{base_url}/sandboxes/{self.state.sandbox_name}/files",
                params={"path": path},
                headers=headers,
                files={"file": (filename, payload)},
                timeout=timeout,
            )
        if response.status_code >= 400:
            raise WorkspaceArchiveWriteError(
                path=posix_path_as_path(coerce_posix_path(path)),
                context=_islo_workspace_error_context(response),
            )

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            workspace_path = await self._check_read_with_exec(path, user=user)
        else:
            workspace_path = await self._validate_path_access(path)
        try:
            data = await self._download_file_via_http(
                sandbox_path_str(workspace_path),
                timeout=self.state.timeouts.file_download_s,
            )
            return io.BytesIO(data)
        except WorkspaceReadNotFoundError as e:
            raise WorkspaceReadNotFoundError(path=error_path, cause=e) from e
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:
            if _is_not_found_error(e):
                raise WorkspaceReadNotFoundError(path=error_path, cause=e) from e
            raise WorkspaceArchiveReadError(path=error_path, cause=e) from e

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        error_path = posix_path_as_path(coerce_posix_path(path))
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=error_path, actual_type=type(payload).__name__)

        if user is not None:
            workspace_path = await self._check_write_with_exec(path, user=user)
            await self._write_file_as_user(workspace_path, bytes(payload), user=user)
            return

        workspace_path = await self._validate_path_access(path, for_write=True)
        try:
            await self._upload_file_via_http(
                sandbox_path_str(workspace_path),
                bytes(payload),
                timeout=self.state.timeouts.file_upload_s,
            )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def _write_file_as_user(
        self,
        workspace_path: Path,
        payload: bytes,
        *,
        user: str | User,
    ) -> None:
        temp_path = f"/tmp/openai-agents-islo-write-{self.state.session_id.hex}-{uuid.uuid4().hex}"
        try:
            await self._upload_file_via_http(
                temp_path,
                payload,
                timeout=self.state.timeouts.file_upload_s,
            )
            chmod = await self._run_islo_command(
                ["chmod", "a+r", "--", temp_path],
                timeout=self.state.timeouts.cleanup_s,
                workdir="/",
                envs=await self._resolved_envs(),
            )
            if not chmod.ok():
                raise WorkspaceArchiveWriteError(
                    path=workspace_path,
                    context={
                        "backend": "islo",
                        "reason": "temp_file_chmod_failed",
                        "exit_code": chmod.exit_code,
                    },
                )
            result = await self.exec(
                "sh",
                "-lc",
                'cat "$1" > "$2"',
                "sh",
                temp_path,
                sandbox_path_str(workspace_path),
                shell=False,
                user=user,
            )
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=workspace_path,
                    context={
                        "backend": "islo",
                        "reason": "user_write_failed",
                        "exit_code": result.exit_code,
                        "stdout": result.stdout.decode("utf-8", errors="replace"),
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e
        finally:
            try:
                await self._run_islo_command(
                    ["rm", "-f", "--", temp_path],
                    timeout=self.state.timeouts.cleanup_s,
                    workdir="/",
                    envs=await self._resolved_envs(),
                )
            except Exception:
                pass

    async def running(self) -> bool:
        try:
            sandbox = await asyncio.wait_for(
                self._client.sandboxes.get_sandbox(self.state.sandbox_name),
                timeout=self.state.timeouts.keepalive_s,
            )
        except Exception:
            return False
        return str(getattr(sandbox, "status", "")).lower() in {"running", "started"}

    def _tar_exclude_args(self) -> list[str]:
        return shell_tar_exclude_args(self._persist_workspace_skip_relpaths())

    @retry_async(
        retry_if=lambda exc, self, tar_cmd, tar_path: (
            exception_chain_contains_type(
                exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)
            )
            or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
        )
    )
    async def _run_persist_workspace_command(self, tar_cmd: str, tar_path: str) -> bytes:
        root = self._workspace_root_path()
        try:
            result = await self._run_islo_command(
                ["sh", "-lc", tar_cmd],
                timeout=self.state.timeouts.workspace_archive_s,
                workdir="/",
                envs=await self._resolved_envs(),
            )
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "backend": "islo",
                        "reason": "tar_failed",
                        "exit_code": result.exit_code,
                        "stdout": result.stdout.decode("utf-8", errors="replace"),
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
            return await self._download_file_via_http(
                tar_path,
                timeout=self.state.timeouts.file_download_s,
            )
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:
            raise WorkspaceArchiveReadError(path=root, cause=e) from e

    async def persist_workspace(self) -> io.IOBase:
        return await with_ephemeral_mounts_removed(
            self,
            self._persist_workspace_internal,
            error_path=self._workspace_root_path(),
            error_cls=WorkspaceArchiveReadError,
            operation_error_context_key="snapshot_error_before_remount_corruption",
        )

    async def _persist_workspace_internal(self) -> io.IOBase:
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT:
            if (
                not self._native_snapshot_requires_tar_fallback()
                and not self._persist_workspace_skip_relpaths()
            ):
                return await self._persist_workspace_via_snapshot()

        root = self._workspace_root_path()
        tar_path = f"/tmp/openai-agents-islo-{self.state.session_id.hex}.tar"
        excludes = " ".join(self._tar_exclude_args())
        tar_cmd = (
            f"tar {excludes} -C {shlex.quote(root.as_posix())} -cf {shlex.quote(tar_path)} ."
        ).strip()
        try:
            raw = await self._run_persist_workspace_command(tar_cmd, tar_path)
            return io.BytesIO(raw)
        finally:
            try:
                await self._run_islo_command(
                    ["rm", "-f", "--", tar_path],
                    timeout=self.state.timeouts.cleanup_s,
                    workdir="/",
                    envs=await self._resolved_envs(),
                )
            except Exception:
                pass

    async def _persist_workspace_via_snapshot(self) -> io.IOBase:
        root = self._workspace_root_path()
        snapshot_name = f"openai-agents-{self.state.session_id.hex}"
        try:
            snapshot = await asyncio.wait_for(
                self._client.snapshots.create_snapshot(
                    sandbox_name=self.state.sandbox_name,
                    name=snapshot_name,
                ),
                timeout=self.state.timeouts.snapshot_s,
            )
            resolved_name = getattr(snapshot, "name", None)
            if not isinstance(resolved_name, str) or not resolved_name:
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "backend": "islo",
                        "reason": "native_snapshot_unexpected_return",
                        "type": type(snapshot).__name__,
                    },
                )
            return io.BytesIO(_encode_islo_snapshot_ref(snapshot_name=resolved_name))
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:
            raise WorkspaceArchiveReadError(
                path=root,
                context={"backend": "islo", "reason": "native_snapshot_failed"},
                cause=e,
            ) from e

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        tar_path = f"/tmp/openai-agents-islo-hydrate-{self.state.session_id.hex}.tar"
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=Path(tar_path), actual_type=type(payload).__name__)
        raw = bytes(payload)

        snapshot_name = _decode_islo_snapshot_ref(raw)
        if snapshot_name is not None:
            await self._replace_sandbox_from_snapshot(snapshot_name)
            return

        try:
            validate_tar_bytes(raw, allow_external_symlink_targets=False)
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "backend": "islo",
                    "reason": "unsafe_or_invalid_tar",
                    "member": e.member,
                    "detail": str(e),
                },
                cause=e,
            ) from e

        await with_ephemeral_mounts_removed(
            self,
            lambda: self._hydrate_workspace_internal(raw, tar_path),
            error_path=root,
            error_cls=WorkspaceArchiveWriteError,
            operation_error_context_key="hydrate_error_before_remount_corruption",
        )

    async def _hydrate_workspace_internal(self, raw: bytes, tar_path: str) -> None:
        root = self._workspace_root_path()
        try:
            await self._run_islo_command(
                ["mkdir", "-p", "--", root.as_posix()],
                timeout=self.state.timeouts.keepalive_s,
                workdir="/",
                envs=await self._resolved_envs(),
            )
            await self._upload_file_via_http(
                tar_path,
                raw,
                timeout=self.state.timeouts.file_upload_s,
            )
            result = await self._run_islo_command(
                ["tar", "-C", root.as_posix(), "-xf", tar_path],
                timeout=self.state.timeouts.workspace_archive_s,
                workdir="/",
                envs=await self._resolved_envs(),
            )
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "backend": "islo",
                        "reason": "tar_extract_failed",
                        "exit_code": result.exit_code,
                        "stdout": result.stdout.decode("utf-8", errors="replace"),
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e
        finally:
            try:
                await self._run_islo_command(
                    ["rm", "-f", "--", tar_path],
                    timeout=self.state.timeouts.cleanup_s,
                    workdir="/",
                    envs=await self._resolved_envs(),
                )
            except Exception:
                pass

    async def _replace_sandbox_from_snapshot(self, snapshot_name: str) -> None:
        try:
            await self._delete_backend()
        except Exception:
            pass
        try:
            try:
                sandbox = await self._create_sandbox_from_state(snapshot_name=snapshot_name)
            except Exception as e:
                if not _is_name_conflict_error(e):
                    raise
                logger.debug(
                    "islo sandbox name is still reserved during snapshot restore, "
                    "recreating with generated name: %s",
                    e,
                )
                sandbox = await self._create_sandbox_from_state(
                    snapshot_name=snapshot_name,
                    include_name=False,
                )
        except Exception as e:
            raise WorkspaceArchiveWriteError(
                path=self._workspace_root_path(),
                context={
                    "backend": "islo",
                    "reason": "native_snapshot_restore_failed",
                    "snapshot_name": snapshot_name,
                },
                cause=e,
            ) from e
        self._apply_sandbox_response(sandbox)
        self.state.workspace_root_ready = True

    async def _create_sandbox_from_state(
        self,
        *,
        snapshot_name: str | None = None,
        include_name: bool = True,
    ) -> Any:
        create_sandbox = self._client.sandboxes.create_sandbox
        kwargs: dict[str, object] = _minimal_init_kwargs(create_sandbox)
        for key, value in (
            ("name", (self.state.name or self.state.sandbox_name) if include_name else None),
            ("image", self.state.image),
            ("vcpus", self.state.vcpus),
            ("memory_mb", self.state.memory_mb),
            ("disk_gb", self.state.disk_gb),
            ("snapshot_name", snapshot_name or self.state.snapshot_name),
            ("env", dict(self.state.base_env) or None),
            ("workdir", self.state.workdir),
            ("gateway_profile", self.state.gateway_profile),
            ("cache_key", self.state.cache_key),
        ):
            if value is not None:
                kwargs[key] = value
        return await create_sandbox(**kwargs)

    def _apply_sandbox_response(self, sandbox: Any) -> None:
        sandbox_id = getattr(sandbox, "id", None)
        sandbox_name = getattr(sandbox, "name", None)
        if isinstance(sandbox_id, str) and sandbox_id:
            self.state.sandbox_id = sandbox_id
        if isinstance(sandbox_name, str) and sandbox_name:
            self.state.sandbox_name = sandbox_name
            self.state.name = sandbox_name

    async def _delete_backend(self) -> None:
        await self._client.sandboxes.delete_sandbox(self.state.sandbox_name)

    async def _shutdown_backend(self) -> None:
        try:
            if self.state.pause_on_exit:
                await self._client.sandboxes.pause_sandbox(self.state.sandbox_name)
            else:
                await self._delete_backend()
        except Exception:
            logger.debug("Failed to shut down Islo sandbox", exc_info=True)

    async def _after_shutdown(self) -> None:
        await self._close_client()

    async def _after_start_failed(self) -> None:
        await self._close_client()

    async def _close_client(self) -> None:
        wrapper = getattr(self._client, "_client_wrapper", None)
        http_client = getattr(wrapper, "httpx_client", None)
        close = getattr(http_client, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass


class IsloSandboxClient(BaseSandboxClient[IsloSandboxClientOptions]):
    """Islo sandbox client managing sandbox lifecycle via the Islo SDK."""

    backend_id = "islo"
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        compute_url: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url
        self._compute_url = compute_url
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    def _new_client(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        compute_url: str | None,
    ) -> Any:
        AsyncIslo = _import_islo_sdk()
        kwargs: dict[str, object] = {
            "api_key": _resolve_islo_api_key(api_key),
            "base_url": _resolve_islo_base_url(base_url),
        }
        resolved_compute_url = _resolve_islo_compute_url(compute_url)
        if resolved_compute_url is not None:
            try:
                parameter_names = set(inspect.signature(AsyncIslo).parameters)
            except (TypeError, ValueError):
                parameter_names = set()
            if "compute_url" not in parameter_names:
                raise ConfigurationError(
                    message=(
                        "Islo compute_url requires an Islo SDK version that supports "
                        "AsyncIslo(compute_url=...)."
                    ),
                    error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                    op="start",
                    context={"backend": self.backend_id},
                )
            kwargs["compute_url"] = resolved_compute_url
        return AsyncIslo(**kwargs)

    def _coerce_timeouts(
        self, value: IsloSandboxTimeouts | dict[str, object] | None
    ) -> IsloSandboxTimeouts:
        if isinstance(value, IsloSandboxTimeouts):
            return value
        if value is None:
            return IsloSandboxTimeouts()
        return IsloSandboxTimeouts.model_validate(value)

    def _resolve_manifest(self, manifest: Manifest | None) -> Manifest:
        if manifest is None:
            return Manifest(root=DEFAULT_ISLO_WORKSPACE_ROOT)
        return manifest

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: IsloSandboxClientOptions,
    ) -> SandboxSession:
        if options.workspace_persistence not in (
            _WORKSPACE_PERSISTENCE_TAR,
            _WORKSPACE_PERSISTENCE_SNAPSHOT,
        ):
            raise ConfigurationError(
                message=(
                    "IsloSandboxClientOptions.workspace_persistence must be one of "
                    f"{_WORKSPACE_PERSISTENCE_TAR!r} or {_WORKSPACE_PERSISTENCE_SNAPSHOT!r}"
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id},
            )

        resolved_base_url = _resolve_islo_base_url(options.base_url or self._base_url)
        resolved_compute_url = _resolve_islo_compute_url(options.compute_url or self._compute_url)
        client = self._new_client(
            api_key=self._api_key,
            base_url=resolved_base_url,
            compute_url=resolved_compute_url,
        )
        timeouts = self._coerce_timeouts(options.timeouts)
        create_sandbox = client.sandboxes.create_sandbox
        create_kwargs: dict[str, object] = _minimal_init_kwargs(create_sandbox)
        for key, value in (
            ("name", options.name),
            ("image", options.image),
            ("vcpus", options.vcpus),
            ("memory_mb", options.memory_mb),
            ("disk_gb", options.disk_gb),
            ("snapshot_name", options.snapshot_name),
            ("env", dict(options.env or {}) or None),
            ("workdir", options.workdir),
            ("gateway_profile", options.gateway_profile),
            ("cache_key", options.cache_key),
        ):
            if value is not None:
                create_kwargs[key] = value

        try:
            sandbox = await create_sandbox(**create_kwargs)
        except Exception:
            await self._close_client(client)
            raise

        sandbox_id = getattr(sandbox, "id", None)
        sandbox_name = getattr(sandbox, "name", None)
        if not isinstance(sandbox_id, str) or not sandbox_id:
            await self._close_client(client)
            raise ConfigurationError(
                message="Islo create_sandbox returned an invalid id",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id},
            )
        if not isinstance(sandbox_name, str) or not sandbox_name:
            await self._close_client(client)
            raise ConfigurationError(
                message="Islo create_sandbox returned an invalid name",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id},
            )

        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        resolved_manifest = self._resolve_manifest(manifest)
        state = IsloSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sandbox_id=sandbox_id,
            sandbox_name=sandbox_name,
            base_url=resolved_base_url,
            compute_url=resolved_compute_url,
            name=sandbox_name,
            image=options.image,
            vcpus=options.vcpus,
            memory_mb=options.memory_mb,
            disk_gb=options.disk_gb,
            snapshot_name=options.snapshot_name,
            base_env=dict(options.env or {}),
            workdir=options.workdir,
            gateway_profile=options.gateway_profile,
            cache_key=options.cache_key,
            pause_on_exit=options.pause_on_exit,
            timeouts=timeouts,
            workspace_persistence=options.workspace_persistence,
        )
        inner = IsloSandboxSession.from_state(state, client=client)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, IsloSandboxSession):
            raise TypeError("IsloSandboxClient.delete expects an IsloSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def _reconnected_sandbox_is_usable(
        self,
        state: IsloSandboxSessionState,
        client: Any,
    ) -> bool:
        temp = IsloSandboxSession.from_state(state, client=client)
        try:
            await temp._run_islo_command(
                ["true"],
                timeout=state.timeouts.keepalive_s,
                workdir="/",
                envs=None,
            )
        except Exception as e:
            if _is_not_found_error(e):
                logger.debug("islo sandbox metadata was stale, will recreate: %s", e)
                return False
            logger.debug("islo sandbox reconnect probe failed, keeping reconnect path: %s", e)
        return True

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        if not isinstance(state, IsloSandboxSessionState):
            raise TypeError("IsloSandboxClient.resume expects an IsloSandboxSessionState")

        client = self._new_client(
            api_key=self._api_key,
            base_url=state.base_url or self._base_url,
            compute_url=state.compute_url or self._compute_url,
        )
        reconnected = False
        try:
            sandbox = await client.sandboxes.get_sandbox(state.sandbox_name)
            status = str(getattr(sandbox, "status", "")).lower()
            if status in {"paused", "stopped"}:
                sandbox = await client.sandboxes.resume_sandbox(state.sandbox_name)
            status = str(getattr(sandbox, "status", "")).lower()
            if status in {"running", "started"}:
                reconnected = await self._reconnected_sandbox_is_usable(state, client)
            else:
                raise RuntimeError(
                    f"islo sandbox is not ready to resume: status={status or '<missing>'}"
                )
        except Exception as e:
            if not _is_not_found_error(e):
                await self._close_client(client)
                raise
            logger.debug("islo sandbox metadata was stale, will recreate: %s", e)

        if not reconnected:
            try:
                temp = IsloSandboxSession.from_state(state, client=client)
                try:
                    sandbox = await temp._create_sandbox_from_state()
                except Exception as e:
                    if not _is_name_conflict_error(e):
                        raise
                    logger.debug(
                        "islo sandbox name is still reserved, recreating with generated name: %s",
                        e,
                    )
                    sandbox = await temp._create_sandbox_from_state(include_name=False)
                temp._apply_sandbox_response(sandbox)
                state.workspace_root_ready = False
            except Exception:
                await self._close_client(client)
                raise

        inner = IsloSandboxSession.from_state(state, client=client)
        inner._set_start_state_preserved(reconnected, system=reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return IsloSandboxSessionState.model_validate(payload)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> IsloSandboxClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _close_client(self, client: Any) -> None:
        wrapper = getattr(client, "_client_wrapper", None)
        http_client = getattr(wrapper, "httpx_client", None)
        close = getattr(http_client, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass


__all__ = [
    "DEFAULT_ISLO_WORKSPACE_ROOT",
    "IsloSandboxClient",
    "IsloSandboxClientOptions",
    "IsloSandboxSession",
    "IsloSandboxSessionState",
    "IsloSandboxTimeouts",
]

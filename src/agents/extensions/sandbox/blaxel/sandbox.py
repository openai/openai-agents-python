"""
Blaxel sandbox (https://blaxel.ai) implementation.

This module provides a Blaxel-backed sandbox client/session implementation backed by
``blaxel.core.sandbox.SandboxInstance``.

The ``blaxel`` dependency is optional, so package-level exports should guard imports of this
module. Within this module, Blaxel SDK imports are lazy so users without the extra can still
import the package.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import shlex
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ....sandbox.entries import Mount
from ....sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import SandboxSession, SandboxSessionState
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.pty_types import (
    PTY_PROCESSES_MAX,
    PTY_PROCESSES_WARNING,
    PtyExecUpdate,
    allocate_pty_process_id,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
    truncate_text_by_tokens,
)
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient
from ....sandbox.session.tar_workspace import shell_tar_exclude_args
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_contains_type,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

DEFAULT_BLAXEL_WORKSPACE_ROOT = "/workspace"
logger = logging.getLogger(__name__)


def _import_blaxel_sdk() -> Any:
    """Lazily import SandboxInstance from the Blaxel SDK, raising a clear error if missing."""
    try:
        from blaxel.core.sandbox import SandboxInstance

        return SandboxInstance
    except ImportError as e:
        raise ImportError(
            "BlaxelSandboxClient requires the optional `blaxel` dependency.\n"
            "Install the Blaxel extra before using this sandbox backend."
        ) from e


def _import_aiohttp() -> Any:
    """Lazily import aiohttp for WebSocket PTY support."""
    try:
        import aiohttp

        return aiohttp
    except ImportError as e:
        raise ImportError(
            "PTY support for BlaxelSandboxSession requires the `aiohttp` package.\n"
            "Install it with: pip install aiohttp"
        ) from e


def _has_aiohttp() -> bool:
    """Check whether aiohttp is available without raising."""
    try:
        import aiohttp  # noqa: F401

        return True
    except ImportError:
        return False


def _import_sandbox_api_error() -> type[BaseException] | None:
    """Best-effort import of ``SandboxAPIError`` from the Blaxel SDK.

    Returns the exception class or ``None`` if the SDK is not installed.
    ``SandboxAPIError`` carries a ``status_code`` attribute that lets us
    classify errors (e.g. 404 for not-found, 408/504 for timeouts).
    """
    try:
        from blaxel.core.sandbox import SandboxAPIError

        return cast(type[BaseException], SandboxAPIError)
    except Exception:
        return None


class BlaxelTimeouts(BaseModel):
    """Timeout configuration for Blaxel sandbox operations."""

    model_config = {"frozen": True}

    exec_timeout_s: float = Field(default=300.0, ge=1)
    cleanup_s: float = Field(default=30.0, ge=1)
    file_upload_s: float = Field(default=1800.0, ge=1)
    file_download_s: float = Field(default=1800.0, ge=1)
    workspace_tar_s: float = Field(default=300.0, ge=1)
    fast_op_s: float = Field(default=30.0, ge=1)


@dataclass(frozen=True)
class BlaxelSandboxClientOptions:
    """Client options for the Blaxel sandbox."""

    image: str | None = None
    memory: int | None = None
    region: str | None = None
    ports: tuple[dict[str, Any], ...] | None = None
    env_vars: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    ttl: str | None = None
    name: str | None = None
    pause_on_exit: bool = False
    timeouts: BlaxelTimeouts | dict[str, object] | None = None
    exposed_port_public: bool = True
    exposed_port_url_ttl_s: int = 3600


class BlaxelSandboxSessionState(SandboxSessionState):
    """Serializable state for a Blaxel-backed session."""

    type: Literal["blaxel"] = "blaxel"
    sandbox_name: str
    image: str | None = None
    memory: int | None = None
    region: str | None = None
    base_env_vars: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    ttl: str | None = None
    pause_on_exit: bool = False
    timeouts: BlaxelTimeouts = Field(default_factory=BlaxelTimeouts)
    sandbox_url: str | None = None
    exposed_port_public: bool = True
    exposed_port_url_ttl_s: int = 3600


# ---------------------------------------------------------------------------
# PTY session entry
# ---------------------------------------------------------------------------


@dataclass
class _BlaxelPtySessionEntry:
    ws_session_id: str
    ws: Any  # aiohttp.ClientWebSocketResponse
    http_session: Any  # aiohttp.ClientSession
    tty: bool = True
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    last_used: float = field(default_factory=time.monotonic)
    done: bool = False
    exit_code: int | None = None
    reader_task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------------------
# Sandbox session
# ---------------------------------------------------------------------------


class BlaxelSandboxSession(BaseSandboxSession):
    """Blaxel-backed sandbox session implementation."""

    state: BlaxelSandboxSessionState
    _sandbox: Any  # SandboxInstance
    _token: str | None
    _pty_lock: asyncio.Lock
    _pty_sessions: dict[int, _BlaxelPtySessionEntry]
    _reserved_pty_process_ids: set[int]

    def __init__(
        self,
        *,
        state: BlaxelSandboxSessionState,
        sandbox: Any,
        token: str | None = None,
    ) -> None:
        self.state = state
        self._sandbox = sandbox
        self._token = token
        self._pty_lock = asyncio.Lock()
        self._pty_sessions = {}
        self._reserved_pty_process_ids = set()

    @classmethod
    def from_state(
        cls,
        state: BlaxelSandboxSessionState,
        *,
        sandbox: Any,
        token: str | None = None,
    ) -> BlaxelSandboxSession:
        return cls(state=state, sandbox=sandbox, token=token)

    @property
    def sandbox_name(self) -> str:
        return self.state.sandbox_name

    # -- exposed ports -------------------------------------------------------

    def _assert_exposed_port_configured(self, port: int) -> None:
        # Blaxel previews can be created for any port on demand; no pre-declaration needed.
        pass

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        is_public = self.state.exposed_port_public
        try:
            preview = await self._sandbox.previews.create_if_not_exists(
                {
                    "metadata": {"name": f"port-{port}"},
                    "spec": {"port": port, "public": is_public},
                }
            )
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "blaxel", "detail": "preview_creation_failed"},
                cause=e,
            ) from e

        url = _extract_preview_url(preview)
        if not isinstance(url, str) or not url:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "blaxel", "detail": "invalid_preview_url", "url": url},
            )

        # For private previews, create a time-limited token.
        query = ""
        if not is_public:
            try:
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=self.state.exposed_port_url_ttl_s,
                )
                token = await preview.tokens.create(expires_at)
                token_value = getattr(token, "value", None) or getattr(token, "token", None)
                if isinstance(token_value, str) and token_value:
                    query = f"bl_preview_token={token_value}"
            except Exception as e:
                raise ExposedPortUnavailableError(
                    port=port,
                    exposed_ports=self.state.exposed_ports,
                    reason="backend_unavailable",
                    context={"backend": "blaxel", "detail": "preview_token_creation_failed"},
                    cause=e,
                ) from e

        try:
            split = urlsplit(url)
            host = split.hostname
            if host is None:
                raise ValueError("missing hostname")
            port_value = split.port or (443 if split.scheme == "https" else 80)
            return ExposedPortEndpoint(
                host=host,
                port=port_value,
                tls=split.scheme == "https",
                query=query,
            )
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "blaxel", "detail": "url_parse_failed", "url": url},
                cause=e,
            ) from e

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        # When resuming a paused sandbox, _skip_start is set by the client to
        # avoid reapplying the full manifest over files that may have changed
        # while the sandbox was paused.
        if getattr(self, "_skip_start", False):
            return

        # Ensure workspace root exists before BaseSandboxSession.start() materializes
        # the manifest.  Blaxel base images run as root and do not ship a pre-created
        # workspace directory.
        root = sandbox_path_str(self.state.manifest.root)
        try:
            await self._sandbox.process.exec(
                {
                    "command": f"mkdir -p {shlex.quote(root)}",
                    "working_dir": "/",
                    "wait_for_completion": True,
                    "timeout": 10000,
                }
            )
        except Exception as e:
            logger.debug("workspace root mkdir failed (will retry during materialization): %s", e)
        await super().start()

    async def stop(self) -> None:
        await super().stop()

    async def shutdown(self) -> None:
        await self.pty_terminate_all()
        try:
            if not self.state.pause_on_exit:
                await self._sandbox.delete()
            # When pause_on_exit is True the sandbox is kept alive.  Blaxel
            # automatically resumes it on the next connection.
        except Exception as e:
            logger.warning("sandbox delete failed during shutdown: %s", e)

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    # -- file operations -----------------------------------------------------

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            path = await self._check_mkdir_with_exec(path, parents=parents, user=user)
        else:
            path = await self._validate_path_access(path, for_write=True)
        if path == Path("/"):
            return
        try:
            await self._sandbox.fs.mkdir(sandbox_path_str(path))
        except Exception as e:
            raise WorkspaceArchiveWriteError(
                path=path,
                context={"reason": "mkdir_failed"},
                cause=e,
            ) from e

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            workspace_path = await self._check_read_with_exec(path, user=user)
        else:
            workspace_path = await self._validate_path_access(path)

        try:
            data: Any = await self._sandbox.fs.read_binary(sandbox_path_str(workspace_path))
            if isinstance(data, str):
                data = data.encode("utf-8")
            return io.BytesIO(bytes(data))
        except Exception as e:
            # Blaxel SDK raises ResponseError with status 404 for missing files.
            status = getattr(e, "status", None)
            if status is None and hasattr(e, "args") and e.args:
                first_arg = e.args[0]
                if isinstance(first_arg, dict):
                    status = first_arg.get("status")
            error_str = str(e).lower()
            if status == 404 or "not found" in error_str or "no such file" in error_str:
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
        if user is not None:
            await self._check_write_with_exec(path, user=user)

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=error_path, actual_type=type(payload).__name__)

        workspace_path = await self._validate_path_access(path, for_write=True)
        try:
            await self._sandbox.fs.write_binary(sandbox_path_str(workspace_path), bytes(payload))
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    # -- exec ----------------------------------------------------------------

    async def _resolved_envs(self) -> dict[str, str]:
        manifest_envs = await self.state.manifest.environment.resolve()
        return {**self.state.base_env_vars, **manifest_envs}

    def _coerce_exec_timeout(self, timeout_s: float | None) -> float:
        """Resolve the effective exec timeout in seconds."""
        if timeout_s is None:
            return float(self.state.timeouts.exec_timeout_s)
        if timeout_s <= 0:
            return 0.001
        return float(timeout_s)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        cmd_str = shlex.join(str(c) for c in command)
        cwd = self.state.manifest.root
        exec_timeout = self._coerce_exec_timeout(timeout)
        timeout_ms = int(max(1, math.ceil(exec_timeout)) * 1000)

        # Resolve manifest + base env vars and prepend them so the executed
        # process sees them.
        envs = await self._resolved_envs()
        if envs:
            env_prefix = " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in envs.items())
            cmd_str = f"env {env_prefix} {cmd_str}"

        try:
            result = await asyncio.wait_for(
                self._sandbox.process.exec(
                    {
                        "command": cmd_str,
                        "working_dir": cwd,
                        "wait_for_completion": True,
                        "timeout": timeout_ms,
                    }
                ),
                timeout=exec_timeout,
            )

            exit_code = int(getattr(result, "exit_code", 0) or 0)
            # Blaxel ProcessResponse uses .stdout / .stderr / .logs attributes. Prefer
            # split streams when available, and only fall back to logs/output for older SDKs.
            has_split_streams = hasattr(result, "stdout") or hasattr(result, "stderr")
            stdout = str(getattr(result, "stdout", "") or "")
            stderr = str(getattr(result, "stderr", "") or "")
            fallback = str(getattr(result, "logs", "") or getattr(result, "output", "") or "")
            stdout_bytes = stdout.encode("utf-8", errors="replace")
            stderr_bytes = stderr.encode("utf-8", errors="replace")

            if has_split_streams:
                return ExecResult(stdout=stdout_bytes, stderr=stderr_bytes, exit_code=exit_code)

            fallback_bytes = fallback.encode("utf-8", errors="replace")
            if exit_code == 0:
                return ExecResult(stdout=fallback_bytes, stderr=b"", exit_code=exit_code)
            return ExecResult(stdout=b"", stderr=fallback_bytes, exit_code=exit_code)
        except asyncio.TimeoutError as e:
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except (ExecTimeoutError, ExecTransportError):
            raise
        except Exception as e:
            api_error_cls = _import_sandbox_api_error()
            if api_error_cls is not None and isinstance(e, api_error_cls):
                status = getattr(e, "status_code", None)
                if status in (408, 504):
                    raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
            raise ExecTransportError(command=command, cause=e) from e

    # -- running check -------------------------------------------------------

    async def running(self) -> bool:
        try:
            await asyncio.wait_for(self._sandbox.fs.ls("/"), timeout=10.0)
            return True
        except Exception as e:
            logger.debug("sandbox health check failed: %s", e)
            return False

    # -- workspace persistence -----------------------------------------------

    def _tar_exclude_args(self) -> list[str]:
        return shell_tar_exclude_args(self._persist_workspace_skip_relpaths())

    @retry_async(
        retry_if=lambda exc, self: (
            exception_chain_contains_type(exc, (asyncio.TimeoutError,))
            or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
        )
    )
    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        tar_path = f"/tmp/bl-persist-{self.state.session_id.hex}.tar"
        excludes = " ".join(self._tar_exclude_args())
        tar_cmd = (
            f"tar {excludes} -C {shlex.quote(root.as_posix())} -cf {shlex.quote(tar_path)} ."
        ).strip()

        unmounted_mounts: list[tuple[Mount, Path]] = []
        unmount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in self.state.manifest.ephemeral_mount_targets():
            try:
                await mount_entry.mount_strategy.teardown_for_snapshot(
                    mount_entry, self, mount_path
                )
            except Exception as e:
                unmount_error = WorkspaceArchiveReadError(path=root, cause=e)
                break
            unmounted_mounts.append((mount_entry, mount_path))

        snapshot_error: WorkspaceArchiveReadError | None = None
        raw: bytes | None = None
        if unmount_error is None:
            try:
                result = await self._exec_internal(
                    "sh", "-c", tar_cmd, timeout=self.state.timeouts.workspace_tar_s
                )
                if result.exit_code != 0:
                    raise WorkspaceArchiveReadError(
                        path=root,
                        context={
                            "reason": "tar_failed",
                            "output": result.stderr.decode("utf-8", errors="replace"),
                        },
                    )
                raw_data: Any = await self._sandbox.fs.read_binary(tar_path)
                if isinstance(raw_data, str):
                    raw_data = raw_data.encode("utf-8")
                raw = bytes(raw_data)
            except WorkspaceArchiveReadError as e:
                snapshot_error = e
            except Exception as e:
                snapshot_error = WorkspaceArchiveReadError(path=root, cause=e)
            finally:
                try:
                    await self._exec_internal(
                        "rm", "-f", "--", tar_path, timeout=self.state.timeouts.cleanup_s
                    )
                except Exception as e:
                    logger.debug("persist cleanup rm failed (non-fatal): %s", e)

        remount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in reversed(unmounted_mounts):
            try:
                await mount_entry.mount_strategy.restore_after_snapshot(
                    mount_entry, self, mount_path
                )
            except Exception as e:
                if remount_error is None:
                    remount_error = WorkspaceArchiveReadError(path=root, cause=e)

        if remount_error is not None:
            raise remount_error
        if unmount_error is not None:
            raise unmount_error
        if snapshot_error is not None:
            raise snapshot_error

        assert raw is not None
        return io.BytesIO(raw)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        tar_path = f"/tmp/bl-hydrate-{self.state.session_id.hex}.tar"
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=Path(tar_path), actual_type=type(payload).__name__)

        try:
            validate_tar_bytes(bytes(payload))
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "unsafe_or_invalid_tar",
                    "member": e.member,
                    "detail": str(e),
                },
                cause=e,
            ) from e

        try:
            await self.mkdir(root, parents=True)
            await self._sandbox.fs.write_binary(tar_path, bytes(payload))
            result = await self._exec_internal(
                "sh",
                "-c",
                f"tar -C {shlex.quote(root.as_posix())} -xf {shlex.quote(tar_path)}",
                timeout=self.state.timeouts.workspace_tar_s,
            )
            if result.exit_code != 0:
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "reason": "tar_extract_failed",
                        "output": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e
        finally:
            try:
                await self._exec_internal(
                    "rm", "-f", "--", tar_path, timeout=self.state.timeouts.cleanup_s
                )
            except Exception as e:
                logger.debug("hydrate cleanup rm failed (non-fatal): %s", e)

    # -- PTY -----------------------------------------------------------------

    def supports_pty(self) -> bool:
        return self.state.sandbox_url is not None and self._token is not None and _has_aiohttp()

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        aiohttp = _import_aiohttp()
        sanitized = self._prepare_exec_command(*command, shell=shell, user=user)
        cmd_str = shlex.join(str(part) for part in sanitized)
        cwd = self.state.manifest.root
        exec_timeout = timeout if timeout is not None else self.state.timeouts.exec_timeout_s

        ws_session_id = f"pty-{uuid.uuid4().hex[:12]}"
        ws_url = _build_ws_url(
            sandbox_url=self.state.sandbox_url or "",
            token=self._token or "",
            session_id=ws_session_id,
            cwd=cwd,
        )

        entry = _BlaxelPtySessionEntry(
            ws_session_id=ws_session_id,
            ws=None,
            http_session=None,
            tty=True,
        )

        registered = False
        pruned: _BlaxelPtySessionEntry | None = None
        process_count = 0

        try:
            http_session = aiohttp.ClientSession()
            entry.http_session = http_session
            ws = await asyncio.wait_for(
                http_session.ws_connect(ws_url),
                timeout=exec_timeout,
            )
            entry.ws = ws

            # Start background reader.
            entry.reader_task = asyncio.create_task(self._pty_ws_reader(entry))

            # Send command.
            await asyncio.wait_for(
                ws.send_str(json.dumps({"type": "input", "data": cmd_str + "\n"})),
                timeout=self.state.timeouts.fast_op_s,
            )

            async with self._pty_lock:
                process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
                self._reserved_pty_process_ids.add(process_id)
                pruned = self._prune_pty_sessions_if_needed()
                self._pty_sessions[process_id] = entry
                process_count = len(self._pty_sessions)
                registered = True
        except asyncio.TimeoutError as e:
            if not registered:
                await self._terminate_pty_entry(entry)
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except Exception as e:
            if not registered:
                await self._terminate_pty_entry(entry)
            raise ExecTransportError(command=command, cause=e) from e

        if pruned is not None:
            await self._terminate_pty_entry(pruned)

        if process_count >= PTY_PROCESSES_WARNING:
            logger.warning(
                "PTY process count reached warning threshold: %s active sessions",
                process_count,
            )

        yield_time_ms = 10_000 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=clamp_pty_yield_time_ms(yield_time_ms),
            max_output_tokens=max_output_tokens,
        )
        return await self._finalize_pty_update(
            process_id=process_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        async with self._pty_lock:
            entry = self._resolve_pty_session_entry(
                pty_processes=self._pty_sessions,
                session_id=session_id,
            )

        if chars and entry.ws is not None:
            await asyncio.wait_for(
                entry.ws.send_str(json.dumps({"type": "input", "data": chars})),
                timeout=self.state.timeouts.fast_op_s,
            )
            await asyncio.sleep(0.1)

        yield_time_ms = 250 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=resolve_pty_write_yield_time_ms(
                yield_time_ms=yield_time_ms, input_empty=chars == ""
            ),
            max_output_tokens=max_output_tokens,
        )
        entry.last_used = time.monotonic()
        return await self._finalize_pty_update(
            process_id=session_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_terminate_all(self) -> None:
        async with self._pty_lock:
            entries = list(self._pty_sessions.values())
            self._pty_sessions.clear()
            self._reserved_pty_process_ids.clear()
        for entry in entries:
            await self._terminate_pty_entry(entry)

    # -- PTY internals -------------------------------------------------------

    async def _pty_ws_reader(self, entry: _BlaxelPtySessionEntry) -> None:
        """Background task that reads WebSocket messages into *entry.output_chunks*."""
        try:
            aiohttp = _import_aiohttp()
            async for msg in entry.ws:
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    try:
                        raw_text = (
                            msg.data
                            if isinstance(msg.data, str)
                            else msg.data.decode("utf-8", errors="replace")
                        )
                        data = json.loads(raw_text)
                        msg_type = data.get("type", "") or data.get("Type", "")
                        if msg_type == "output":
                            raw = (data.get("data", "") or data.get("Data", "")).encode(
                                "utf-8", errors="replace"
                            )
                            async with entry.output_lock:
                                entry.output_chunks.append(raw)
                            entry.output_notify.set()
                        elif msg_type == "error":
                            raw = (data.get("data", "") or data.get("Data", "")).encode(
                                "utf-8", errors="replace"
                            )
                            async with entry.output_lock:
                                entry.output_chunks.append(raw)
                            entry.done = True
                            entry.output_notify.set()
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.debug("PTY ws reader: ignoring malformed message")
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
        except Exception as e:
            logger.debug("PTY ws reader terminated with error: %s", e)
        finally:
            entry.done = True
            entry.output_notify.set()

    async def _collect_pty_output(
        self,
        *,
        entry: _BlaxelPtySessionEntry,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[bytes, int | None]:
        deadline = time.monotonic() + (yield_time_ms / 1000)
        output = bytearray()

        while True:
            async with entry.output_lock:
                while entry.output_chunks:
                    output.extend(entry.output_chunks.popleft())

            if time.monotonic() >= deadline:
                break
            if entry.done:
                async with entry.output_lock:
                    while entry.output_chunks:
                        output.extend(entry.output_chunks.popleft())
                break

            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break
            try:
                await asyncio.wait_for(entry.output_notify.wait(), timeout=remaining_s)
            except asyncio.TimeoutError:
                break
            entry.output_notify.clear()

        text = output.decode("utf-8", errors="replace")
        truncated, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
        return truncated.encode("utf-8", errors="replace"), original_token_count

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _BlaxelPtySessionEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        exit_code = entry.exit_code if entry.done else None
        live_process_id: int | None = process_id

        if entry.done:
            async with self._pty_lock:
                removed = self._pty_sessions.pop(process_id, None)
                self._reserved_pty_process_ids.discard(process_id)
            if removed is not None:
                await self._terminate_pty_entry(removed)
            live_process_id = None

        return PtyExecUpdate(
            process_id=live_process_id,
            output=output,
            exit_code=exit_code,
            original_token_count=original_token_count,
        )

    def _prune_pty_sessions_if_needed(self) -> _BlaxelPtySessionEntry | None:
        if len(self._pty_sessions) < PTY_PROCESSES_MAX:
            return None
        meta: list[tuple[int, float, bool]] = [
            (pid, e.last_used, e.done) for pid, e in self._pty_sessions.items()
        ]
        pid = process_id_to_prune_from_meta(meta)
        if pid is None:
            return None
        self._reserved_pty_process_ids.discard(pid)
        return self._pty_sessions.pop(pid, None)

    async def _terminate_pty_entry(self, entry: _BlaxelPtySessionEntry) -> None:
        try:
            if entry.reader_task is not None and not entry.reader_task.done():
                entry.reader_task.cancel()
                try:
                    await entry.reader_task
                except (asyncio.CancelledError, Exception):
                    pass
            if entry.ws is not None:
                try:
                    await entry.ws.close()
                except Exception as e:
                    logger.debug("PTY ws close error (non-fatal): %s", e)
            if entry.http_session is not None:
                try:
                    await entry.http_session.close()
                except Exception as e:
                    logger.debug("PTY http session close error (non-fatal): %s", e)
        except Exception as e:
            logger.debug("PTY entry termination error (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Sandbox client
# ---------------------------------------------------------------------------


class BlaxelSandboxClient(BaseSandboxClient["BlaxelSandboxClientOptions"]):
    """Blaxel sandbox client managing sandbox lifecycle via the Blaxel SDK."""

    backend_id = "blaxel"
    _instrumentation: Instrumentation
    _token: str | None

    def __init__(
        self,
        *,
        token: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        # Validate that the Blaxel SDK is importable.
        _import_blaxel_sdk()
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies
        self._token = token or os.environ.get("BL_API_KEY")

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: BlaxelSandboxClientOptions,
    ) -> SandboxSession:
        if manifest is None:
            manifest = Manifest(root=DEFAULT_BLAXEL_WORKSPACE_ROOT)

        timeouts_in = options.timeouts
        if isinstance(timeouts_in, BlaxelTimeouts):
            timeouts = timeouts_in
        elif timeouts_in is None:
            timeouts = BlaxelTimeouts()
        else:
            timeouts = BlaxelTimeouts.model_validate(timeouts_in)

        session_id = uuid.uuid4()
        sandbox_name = options.name or f"agents-{session_id.hex[:12]}"

        SandboxInstance = _import_blaxel_sdk()
        create_config = _build_create_config(
            name=sandbox_name,
            image=options.image,
            memory=options.memory,
            region=options.region,
            ports=options.ports,
            env_vars=options.env_vars,
            labels=options.labels,
            ttl=options.ttl,
            manifest=manifest,
        )
        blaxel_sandbox = await SandboxInstance.create_if_not_exists(create_config)

        sandbox_url = _get_sandbox_url(blaxel_sandbox)
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = BlaxelSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            sandbox_name=sandbox_name,
            image=options.image,
            memory=options.memory,
            region=options.region,
            base_env_vars=dict(options.env_vars or {}),
            labels=dict(options.labels or {}),
            ttl=options.ttl,
            pause_on_exit=options.pause_on_exit,
            timeouts=timeouts,
            sandbox_url=sandbox_url,
            exposed_port_public=options.exposed_port_public,
            exposed_port_url_ttl_s=options.exposed_port_url_ttl_s,
        )
        inner = BlaxelSandboxSession.from_state(state, sandbox=blaxel_sandbox, token=self._token)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def close(self) -> None:
        """No persistent HTTP client to close; provided for API symmetry."""

    async def __aenter__(self) -> BlaxelSandboxClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, BlaxelSandboxSession):
            raise TypeError("BlaxelSandboxClient.delete expects a BlaxelSandboxSession")
        try:
            await inner.shutdown()
        except Exception as e:
            logger.warning("shutdown error during delete (non-fatal): %s", e)
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        """Resume a sandbox from persisted state.

        When ``pause_on_exit`` is set, Blaxel automatically resumes the paused
        sandbox on connection -- this method simply reconnects by sandbox name
        via ``SandboxInstance.get()``.  If the sandbox is no longer available
        (e.g. it expired), a fresh one is created with the same configuration.
        """
        if not isinstance(state, BlaxelSandboxSessionState):
            raise TypeError("BlaxelSandboxClient.resume expects a BlaxelSandboxSessionState")

        SandboxInstance = _import_blaxel_sdk()
        blaxel_sandbox = None
        reconnected = False

        if state.pause_on_exit:
            try:
                blaxel_sandbox = await SandboxInstance.get(state.sandbox_name)
                reconnected = True
            except Exception as e:
                logger.debug("sandbox get() failed, will recreate: %s", e)

        if not reconnected or blaxel_sandbox is None:
            create_config = _build_create_config(
                name=state.sandbox_name,
                image=state.image,
                memory=state.memory,
                region=state.region,
                env_vars=state.base_env_vars or None,
                labels=state.labels or None,
                ttl=state.ttl,
            )
            blaxel_sandbox = await SandboxInstance.create_if_not_exists(create_config)

        sandbox_url = _get_sandbox_url(blaxel_sandbox)
        if sandbox_url:
            state.sandbox_url = sandbox_url

        inner = BlaxelSandboxSession.from_state(state, sandbox=blaxel_sandbox, token=self._token)
        if state.pause_on_exit and reconnected:
            inner._skip_start = True  # type: ignore[attr-defined]
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return BlaxelSandboxSessionState.model_validate(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_create_config(
    *,
    name: str,
    image: str | None = None,
    memory: int | None = None,
    region: str | None = None,
    ports: tuple[dict[str, Any], ...] | None = None,
    env_vars: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    ttl: str | None = None,
    manifest: Manifest | None = None,
) -> dict[str, Any]:
    """Build the dict config accepted by ``SandboxInstance.create_if_not_exists``."""
    config: dict[str, Any] = {"name": name}

    if image:
        config["image"] = image
    if memory is not None:
        config["memory"] = memory
    resolved_region = region or os.environ.get("BL_REGION") or "us-pdx-1"
    config["region"] = resolved_region
    if labels:
        config["labels"] = labels
    if ttl:
        config["ttl"] = ttl

    # Pass base env vars for sandbox creation.  The session will re-resolve
    # manifest environment variables at exec time.
    all_envs: dict[str, str] = {}
    if env_vars:
        all_envs.update(env_vars)
    if all_envs:
        config["envs"] = [{"name": k, "value": v} for k, v in all_envs.items()]

    if ports:
        config["ports"] = list(ports)

    return config


def _get_sandbox_url(sandbox_instance: Any) -> str | None:
    """Best-effort extract the sandbox URL from a SandboxInstance."""
    # Try sandbox_instance.sandbox.metadata.url (standard path).
    sandbox_model = getattr(sandbox_instance, "sandbox", None)
    if sandbox_model is not None:
        metadata = getattr(sandbox_model, "metadata", None)
        if metadata is not None:
            url = getattr(metadata, "url", None)
            if isinstance(url, str) and url:
                return url
    # Try direct .url attribute.
    url = getattr(sandbox_instance, "url", None)
    if isinstance(url, str) and url:
        return url
    return None


def _extract_preview_url(preview: Any) -> str | None:
    """Extract URL string from a preview object, trying several attribute paths.

    Blaxel SDK returns a ``SandboxPreview`` whose URL lives at ``preview.spec.url``.
    """
    # Try spec.url first (Blaxel SDK path).
    for nested in ("spec", "status"):
        obj = getattr(preview, nested, None)
        if obj is not None:
            val = getattr(obj, "url", None)
            if isinstance(val, str) and val:
                return val
    # Try direct attributes.
    for attr in ("url", "endpoint"):
        val = getattr(preview, attr, None)
        if isinstance(val, str) and val:
            return val
    # Try the nested .preview.spec.url path.
    inner = getattr(preview, "preview", None)
    if inner is not None:
        return _extract_preview_url(inner)
    return None


def _build_ws_url(
    *,
    sandbox_url: str,
    token: str,
    session_id: str,
    cwd: str,
    cols: int = 80,
    rows: int = 24,
) -> str:
    """Build the WebSocket URL for a Blaxel terminal session."""
    base = sandbox_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return (
        f"{ws_base}/terminal/ws"
        f"?token={token}"
        f"&cols={cols}"
        f"&rows={rows}"
        f"&sessionId={session_id}"
        f"&workingDir={cwd}"
    )


__all__ = [
    "DEFAULT_BLAXEL_WORKSPACE_ROOT",
    "BlaxelSandboxClient",
    "BlaxelSandboxClientOptions",
    "BlaxelSandboxSession",
    "BlaxelSandboxSessionState",
    "BlaxelTimeouts",
]

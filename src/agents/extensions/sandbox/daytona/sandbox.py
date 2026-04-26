"""
Daytona sandbox (https://daytona.io) implementation.

This module provides a Daytona-backed sandbox client/session implementation backed by
`daytona.Sandbox` via the AsyncDaytona client.

The `daytona` dependency is optional, so package-level exports should guard imports of this
module. Within this module, Daytona SDK imports are lazy so users without the extra can still
import the package.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import shlex
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ....sandbox.entries import Mount
from ....sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    InvalidManifestPathError as InvalidManifestPathError,
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
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
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
from ....sandbox.workspace_paths import (
    coerce_posix_path,
    posix_path_as_path,
    posix_path_for_error,
    sandbox_path_str,
)

DEFAULT_DAYTONA_WORKSPACE_ROOT = "/home/daytona/workspace"
logger = logging.getLogger(__name__)


def _import_daytona_sdk() -> tuple[Any, Any, Any, Any]:
    """Lazily import Daytona SDK classes, raising a clear error if missing."""
    try:
        from daytona import (
            AsyncDaytona,
            CreateSandboxFromImageParams,
            CreateSandboxFromSnapshotParams,
            DaytonaConfig,
        )

        return (
            AsyncDaytona,
            DaytonaConfig,
            CreateSandboxFromSnapshotParams,
            CreateSandboxFromImageParams,
        )
    except ImportError as e:
        raise ImportError(
            "DaytonaSandboxClient requires the optional `daytona` dependency.\n"
            "Install the Daytona extra before using this sandbox backend."
        ) from e


def _import_sandbox_state() -> Any:
    """Lazily import SandboxState enum from Daytona SDK, or None if unavailable."""
    try:
        from daytona import SandboxState

        return SandboxState
    except ImportError:
        return None


def _import_sdk_resources() -> Any:
    """Lazily import Resources from Daytona SDK."""
    try:
        from daytona import Resources

        return Resources
    except ImportError as e:
        raise ImportError(
            "DaytonaSandboxClient requires the optional `daytona` dependency.\n"
            "Install the Daytona extra before using this sandbox backend."
        ) from e


def _import_pty_size() -> Any:
    """Lazily import PtySize from Daytona SDK."""
    try:
        from daytona.common.pty import PtySize

        return PtySize
    except ImportError as e:
        raise ImportError(
            "DaytonaSandboxClient requires the optional `daytona` dependency.\n"
            "Install the Daytona extra before using this sandbox backend."
        ) from e


def _import_session_execute_request() -> Any:
    """Lazily import SessionExecuteRequest from Daytona SDK."""
    try:
        from daytona import SessionExecuteRequest

        return SessionExecuteRequest
    except ImportError as e:
        raise ImportError(
            "DaytonaSandboxClient requires the optional `daytona` dependency.\n"
            "Install the Daytona extra before using this sandbox backend."
        ) from e


def _import_daytona_exceptions() -> dict[str, type[BaseException]]:
    """Best-effort import Daytona exception classes for fine-grained error mapping."""
    try:
        from daytona import (
            DaytonaError,
            DaytonaNotFoundError,
            DaytonaRateLimitError,
            DaytonaTimeoutError,
        )
    except Exception:
        return {}
    return {
        "base": DaytonaError,
        "timeout": DaytonaTimeoutError,
        "not_found": DaytonaNotFoundError,
        "rate_limit": DaytonaRateLimitError,
    }


def _retryable_persist_workspace_error_types() -> tuple[type[BaseException], ...]:
    excs = _import_daytona_exceptions()
    retryable: list[type[BaseException]] = [asyncio.TimeoutError]
    timeout_exc = excs.get("timeout")
    if timeout_exc is not None:
        retryable.append(timeout_exc)
    return tuple(retryable)


class DaytonaSandboxResources(BaseModel):
    """Resource configuration for a Daytona sandbox."""

    model_config = {"frozen": True}

    cpu: int | None = None
    memory: int | None = None
    disk: int | None = None


class DaytonaSandboxTimeouts(BaseModel):
    """Timeout configuration for Daytona sandbox operations."""

    exec_timeout_unbounded_s: int = Field(default=24 * 60 * 60, ge=1)
    keepalive_s: int = Field(default=10, ge=1)
    cleanup_s: int = Field(default=30, ge=1)
    fast_op_s: int = Field(default=30, ge=1)
    file_upload_s: int = Field(default=1800, ge=1)
    file_download_s: int = Field(default=1800, ge=1)
    workspace_tar_s: int = Field(default=300, ge=1)


class DaytonaSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Daytona sandbox."""

    type: Literal["daytona"] = "daytona"
    sandbox_snapshot_name: str | None = None
    image: str | None = None
    resources: DaytonaSandboxResources | None = None
    env_vars: dict[str, str] | None = None
    pause_on_exit: bool = False
    create_timeout: int = 60
    start_timeout: int = 60
    name: str | None = None
    auto_stop_interval: int = 0
    timeouts: DaytonaSandboxTimeouts | dict[str, object] | None = None
    exposed_ports: tuple[int, ...] = ()
    # This TTL applies to new connection setup only: Daytona checks signed preview URL expiry during
    # the initial HTTP request / websocket upgrade handshake. In live testing, an already-open
    # websocket stayed connected after the URL expired, but any reconnect or new handshake needed a
    # freshly resolved URL.
    exposed_port_url_ttl_s: int = 3600

    def __init__(
        self,
        sandbox_snapshot_name: str | None = None,
        image: str | None = None,
        resources: DaytonaSandboxResources | None = None,
        env_vars: dict[str, str] | None = None,
        pause_on_exit: bool = False,
        create_timeout: int = 60,
        start_timeout: int = 60,
        name: str | None = None,
        auto_stop_interval: int = 0,
        timeouts: DaytonaSandboxTimeouts | dict[str, object] | None = None,
        exposed_ports: tuple[int, ...] = (),
        exposed_port_url_ttl_s: int = 3600,
        *,
        type: Literal["daytona"] = "daytona",
    ) -> None:
        super().__init__(
            type=type,
            sandbox_snapshot_name=sandbox_snapshot_name,
            image=image,
            resources=resources,
            env_vars=env_vars,
            pause_on_exit=pause_on_exit,
            create_timeout=create_timeout,
            start_timeout=start_timeout,
            name=name,
            auto_stop_interval=auto_stop_interval,
            timeouts=timeouts,
            exposed_ports=exposed_ports,
            exposed_port_url_ttl_s=exposed_port_url_ttl_s,
        )


class DaytonaSandboxSessionState(SandboxSessionState):
    """Serializable state for a Daytona-backed session."""

    type: Literal["daytona"] = "daytona"
    sandbox_id: str
    sandbox_snapshot_name: str | None = None
    image: str | None = None
    base_env_vars: dict[str, str] = Field(default_factory=dict)
    pause_on_exit: bool = False
    create_timeout: int = 60
    start_timeout: int = 60
    name: str | None = None
    resources: DaytonaSandboxResources | None = None
    auto_stop_interval: int = 0
    timeouts: DaytonaSandboxTimeouts = Field(default_factory=DaytonaSandboxTimeouts)
    exposed_port_url_ttl_s: int = 3600


@dataclass
class _DaytonaPtySessionEntry:
    daytona_session_id: str
    pty_handle: Any
    tty: bool = True
    cmd_id: str | None = None
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    last_used: float = field(default_factory=time.monotonic)
    done: bool = False
    exit_code: int | None = None


class DaytonaSandboxSession(BaseSandboxSession):
    """Daytona-backed sandbox session implementation."""

    state: DaytonaSandboxSessionState
    _sandbox: Any
    _pty_lock: asyncio.Lock
    _pty_sessions: dict[int, _DaytonaPtySessionEntry]
    _reserved_pty_process_ids: set[int]

    def __init__(self, *, state: DaytonaSandboxSessionState, sandbox: Any) -> None:
        self.state = state
        self._sandbox = sandbox
        self._pty_lock = asyncio.Lock()
        self._pty_sessions = {}
        self._reserved_pty_process_ids = set()

    @classmethod
    def from_state(
        cls,
        state: DaytonaSandboxSessionState,
        *,
        sandbox: Any,
    ) -> DaytonaSandboxSession:
        return cls(state=state, sandbox=sandbox)

    @property
    def sandbox_id(self) -> str:
        return self.state.sandbox_id

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            preview = await self._sandbox.create_signed_preview_url(
                port,
                expires_in_seconds=self.state.exposed_port_url_ttl_s,
            )
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "daytona", "detail": "create_signed_preview_url_failed"},
                cause=e,
            ) from e

        url = getattr(preview, "url", None)
        if not isinstance(url, str) or not url:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "daytona", "detail": "invalid_preview_url", "url": url},
            )

        try:
            split = urlsplit(url)
            host = split.hostname
            if host is None:
                raise ValueError("missing hostname")
            port_value = split.port or (443 if split.scheme == "https" else 80)
            return ExposedPortEndpoint(host=host, port=port_value, tls=split.scheme == "https")
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "daytona", "detail": "invalid_preview_url", "url": url},
                cause=e,
            ) from e

    async def _shutdown_backend(self) -> None:
        try:
            if self.state.pause_on_exit:
                await self._sandbox.stop()
            else:
                await self._sandbox.delete()
        except Exception:
            pass

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    async def _prepare_workspace_root(self) -> None:
        """Create the workspace root before SDK exec calls use it as cwd."""
        root = sandbox_path_str(self.state.manifest.root)
        error_root = posix_path_for_error(root)
        try:
            envs = await self._resolved_envs()
            result = await self._sandbox.process.exec(
                f"mkdir -p -- {shlex.quote(root)}",
                env=envs or None,
                timeout=self.state.timeouts.fast_op_s,
            )
        except Exception as e:
            raise WorkspaceStartError(path=error_root, cause=e) from e

        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            raise WorkspaceStartError(
                path=error_root,
                context={
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": exit_code,
                    "output": str(getattr(result, "result", "") or ""),
                },
            )

    async def _prepare_backend_workspace(self) -> None:
        await self._prepare_workspace_root()

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
            await self._sandbox.fs.create_folder(sandbox_path_str(path), "755")
        except Exception as e:
            raise WorkspaceArchiveWriteError(
                path=path,
                context={"reason": "mkdir_failed"},
                cause=e,
            ) from e

    async def _resolved_envs(self) -> dict[str, str]:
        manifest_envs = await self.state.manifest.environment.resolve()
        return {**self.state.base_env_vars, **manifest_envs}

    def _coerce_exec_timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return float(self.state.timeouts.exec_timeout_unbounded_s)
        if timeout_s <= 0:
            return 0.001
        return float(timeout_s)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        cmd_str = shlex.join(str(c) for c in command)
        envs = await self._resolved_envs()
        cwd = sandbox_path_str(self.state.manifest.root)
        env_args = (
            " ".join(shlex.quote(f"{key}={value}") for key, value in envs.items()) if envs else ""
        )
        env_wrapper = f"env -- {env_args} " if env_args else ""
        session_cmd = f"cd {shlex.quote(cwd)} && {env_wrapper}{cmd_str}"
        daytona_session_id = f"sandbox-{uuid.uuid4().hex[:12]}"

        caller_timeout = self._coerce_exec_timeout(timeout)
        deadline = time.monotonic() + caller_timeout
        SessionExecuteRequest = _import_session_execute_request()
        daytona_exc = _import_daytona_exceptions()
        timeout_exc = daytona_exc.get("timeout")

        def _remaining_timeout() -> float:
            return max(0.0, deadline - time.monotonic())

        try:
            await asyncio.wait_for(
                self._sandbox.process.create_session(daytona_session_id),
                timeout=_remaining_timeout(),
            )
            command_timeout = _remaining_timeout()
            sdk_timeout = max(1, math.ceil(command_timeout + 1.0))
            result = await asyncio.wait_for(
                self._sandbox.process.execute_session_command(
                    daytona_session_id,
                    SessionExecuteRequest(command=session_cmd, run_async=False),
                    timeout=sdk_timeout,
                ),
                timeout=caller_timeout,
            )
            exit_code = int(result.exit_code or 0)
            stdout = getattr(result, "stdout", None)
            stderr = getattr(result, "stderr", None)
            if stdout is None and stderr is None:
                output = getattr(result, "output", "") or ""
                if exit_code == 0:
                    stdout = output
                    stderr = ""
                else:
                    stdout = ""
                    stderr = output
            return ExecResult(
                stdout=(stdout or "").encode("utf-8", errors="replace"),
                stderr=(stderr or "").encode("utf-8", errors="replace"),
                exit_code=exit_code,
            )
        except asyncio.TimeoutError as e:
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except Exception as e:
            if timeout_exc is not None and isinstance(e, timeout_exc):
                raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
            raise ExecTransportError(command=command, cause=e) from e
        finally:
            try:
                await asyncio.wait_for(
                    self._sandbox.process.delete_session(daytona_session_id),
                    timeout=self.state.timeouts.cleanup_s,
                )
            except Exception:
                pass

    def supports_pty(self) -> bool:
        return True

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
        PtySize = _import_pty_size()
        sanitized = self._prepare_exec_command(*command, shell=shell, user=user)
        cmd_str = shlex.join(str(part) for part in sanitized)
        envs = await self._resolved_envs()
        cwd = sandbox_path_str(self.state.manifest.root)
        exec_timeout = self._coerce_exec_timeout(timeout)
        daytona_exc = _import_daytona_exceptions()
        timeout_exc = daytona_exc.get("timeout")

        daytona_session_id = f"sandbox-{uuid.uuid4().hex[:12]}"
        entry = _DaytonaPtySessionEntry(
            daytona_session_id=daytona_session_id,
            pty_handle=None,
            tty=tty,
        )

        async def _on_data(chunk: bytes | str) -> None:
            raw = (
                chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else bytes(chunk)
            )
            async with entry.output_lock:
                entry.output_chunks.append(raw)
            entry.output_notify.set()

        pruned: _DaytonaPtySessionEntry | None = None
        registered = False
        try:
            if tty:
                pty_handle = await asyncio.wait_for(
                    self._sandbox.process.create_pty_session(
                        id=daytona_session_id,
                        on_data=_on_data,
                        cwd=cwd,
                        envs=envs or None,
                        pty_size=PtySize(cols=80, rows=24),
                    ),
                    timeout=exec_timeout,
                )
                entry.pty_handle = pty_handle
                asyncio.create_task(self._run_pty_waiter(entry))
                await asyncio.wait_for(pty_handle.wait_for_connection(), timeout=exec_timeout)
                await asyncio.wait_for(
                    pty_handle.send_input(cmd_str + "\n"),
                    timeout=self.state.timeouts.fast_op_s,
                )
            else:
                SessionExecuteRequest = _import_session_execute_request()
                env_args = (
                    " ".join(shlex.quote(f"{key}={value}") for key, value in envs.items())
                    if envs
                    else ""
                )
                env_wrapper = f"env -- {env_args} " if env_args else ""
                session_cmd = f"cd {shlex.quote(cwd)} && {env_wrapper}{cmd_str}"
                await asyncio.wait_for(
                    self._sandbox.process.create_session(daytona_session_id),
                    timeout=exec_timeout,
                )
                resp = await asyncio.wait_for(
                    self._sandbox.process.execute_session_command(
                        daytona_session_id,
                        SessionExecuteRequest(command=session_cmd, run_async=True),
                    ),
                    timeout=exec_timeout,
                )
                entry.cmd_id = resp.cmd_id
                asyncio.create_task(
                    self._run_session_reader(
                        entry,
                        daytona_session_id,
                        resp.cmd_id,
                        _on_data,
                    )
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
                cleanup_task = asyncio.ensure_future(self._terminate_pty_entry(entry))
                try:
                    await asyncio.shield(cleanup_task)
                except BaseException:
                    await asyncio.shield(cleanup_task)
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except Exception as e:
            if not registered:
                cleanup_task = asyncio.ensure_future(self._terminate_pty_entry(entry))
                try:
                    await asyncio.shield(cleanup_task)
                except BaseException:
                    await asyncio.shield(cleanup_task)
            if timeout_exc is not None and isinstance(e, timeout_exc):
                raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
            raise ExecTransportError(command=command, cause=e) from e
        except BaseException:
            if not registered:
                cleanup_task = asyncio.ensure_future(self._terminate_pty_entry(entry))
                try:
                    await asyncio.shield(cleanup_task)
                except BaseException:
                    await asyncio.shield(cleanup_task)
            raise

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

    async def _run_pty_waiter(self, entry: _DaytonaPtySessionEntry) -> None:
        try:
            await entry.pty_handle.wait()
            ec = getattr(entry.pty_handle, "exit_code", None)
            if ec is not None:
                entry.exit_code = int(ec)
        except Exception:
            pass
        finally:
            entry.done = True
            entry.output_notify.set()

    async def _run_session_reader(
        self,
        entry: _DaytonaPtySessionEntry,
        session_id: str,
        cmd_id: str,
        on_data: Any,
    ) -> None:
        logs_failed = False
        try:
            await self._sandbox.process.get_session_command_logs_async(
                session_id,
                cmd_id,
                on_data,
                on_data,
            )
        except Exception:
            logs_failed = True
        finally:
            try:
                cmd = await self._sandbox.process.get_session_command(session_id, cmd_id)
                if cmd.exit_code is not None:
                    entry.exit_code = int(cmd.exit_code)
                    entry.done = True
            except Exception:
                pass
            if not logs_failed:
                entry.done = True
            entry.output_notify.set()

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

        if chars:
            if not entry.tty:
                raise RuntimeError("stdin is not available for this process")
            await asyncio.wait_for(
                entry.pty_handle.send_input(chars),
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

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _DaytonaPtySessionEntry,
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

    async def pty_terminate_all(self) -> None:
        async with self._pty_lock:
            entries = list(self._pty_sessions.values())
            self._pty_sessions.clear()
            self._reserved_pty_process_ids.clear()
        for entry in entries:
            await self._terminate_pty_entry(entry)

    async def _collect_pty_output(
        self,
        *,
        entry: _DaytonaPtySessionEntry,
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

    def _prune_pty_sessions_if_needed(self) -> _DaytonaPtySessionEntry | None:
        if len(self._pty_sessions) < PTY_PROCESSES_MAX:
            return None
        meta: list[tuple[int, float, bool]] = [
            (pid, entry.last_used, entry.done) for pid, entry in self._pty_sessions.items()
        ]
        pid = process_id_to_prune_from_meta(meta)
        if pid is None:
            return None
        self._reserved_pty_process_ids.discard(pid)
        return self._pty_sessions.pop(pid, None)

    async def _terminate_pty_entry(self, entry: _DaytonaPtySessionEntry) -> None:
        try:
            if entry.tty:
                await self._sandbox.process.kill_pty_session(entry.daytona_session_id)
            else:
                await self._sandbox.process.delete_session(entry.daytona_session_id)
        except Exception:
            pass

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            workspace_path = await self._check_read_with_exec(path, user=user)
        else:
            workspace_path = await self._validate_path_access(path)

        daytona_exc = _import_daytona_exceptions()
        not_found_exc = daytona_exc.get("not_found")

        try:
            data: bytes = await self._sandbox.fs.download_file(
                sandbox_path_str(workspace_path),
                self.state.timeouts.file_download_s,
            )
            return io.BytesIO(data)
        except Exception as e:
            if not_found_exc is not None and isinstance(e, not_found_exc):
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
            await self._sandbox.fs.upload_file(
                bytes(payload),
                sandbox_path_str(workspace_path),
                timeout=self.state.timeouts.file_upload_s,
            )
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        try:
            await asyncio.wait_for(
                self._sandbox.refresh_data(),
                timeout=self.state.timeouts.keepalive_s,
            )
            SandboxState = _import_sandbox_state()
            if SandboxState is None:
                return False
            return bool(getattr(self._sandbox, "state", None) == SandboxState.STARTED)
        except Exception:
            return False

    def _tar_exclude_args(self) -> list[str]:
        return shell_tar_exclude_args(self._persist_workspace_skip_relpaths())

    @retry_async(
        retry_if=lambda exc, self, tar_cmd, tar_path: (
            exception_chain_contains_type(exc, _retryable_persist_workspace_error_types())
            or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
        )
    )
    async def _run_persist_workspace_command(self, tar_cmd: str, tar_path: str) -> bytes:
        try:
            envs = await self._resolved_envs()
            result = await self._sandbox.process.exec(
                tar_cmd,
                env=envs or None,
                timeout=self.state.timeouts.workspace_tar_s,
            )
            if result.exit_code != 0:
                raise WorkspaceArchiveReadError(
                    path=self._workspace_root_path(),
                    context={"reason": "tar_failed", "output": result.result or ""},
                )
            return cast(
                bytes,
                await self._sandbox.fs.download_file(
                    tar_path,
                    self.state.timeouts.file_download_s,
                ),
            )
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:
            raise WorkspaceArchiveReadError(path=self._workspace_root_path(), cause=e) from e

    async def persist_workspace(self) -> io.IOBase:
        def _error_context_summary(error: WorkspaceArchiveReadError) -> dict[str, str]:
            summary = {"message": error.message}
            if error.cause is not None:
                summary["cause_type"] = type(error.cause).__name__
                summary["cause"] = str(error.cause)
            return summary

        root = self._workspace_root_path()
        tar_path = f"/tmp/sandbox-persist-{self.state.session_id.hex}.tar"
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
                raw = await self._run_persist_workspace_command(tar_cmd, tar_path)
            except WorkspaceArchiveReadError as e:
                snapshot_error = e
            finally:
                try:
                    await self._sandbox.process.exec(
                        f"rm -f -- {shlex.quote(tar_path)}",
                        timeout=self.state.timeouts.cleanup_s,
                    )
                except Exception:
                    pass

        remount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in reversed(unmounted_mounts):
            try:
                await mount_entry.mount_strategy.restore_after_snapshot(
                    mount_entry, self, mount_path
                )
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
                        "additional_remount_errors",
                        [],
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

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        tar_path = f"/tmp/sandbox-hydrate-{self.state.session_id.hex}.tar"
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
            envs = await self._resolved_envs()
            await self._sandbox.fs.upload_file(
                bytes(payload),
                tar_path,
                timeout=self.state.timeouts.file_upload_s,
            )
            result = await self._sandbox.process.exec(
                f"tar -C {shlex.quote(root.as_posix())} -xf {shlex.quote(tar_path)}",
                env=envs or None,
                timeout=self.state.timeouts.workspace_tar_s,
            )
            if result.exit_code != 0:
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={"reason": "tar_extract_failed", "output": result.result or ""},
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e
        finally:
            try:
                envs = await self._resolved_envs()
                await self._sandbox.process.exec(
                    f"rm -f -- {shlex.quote(tar_path)}",
                    env=envs or None,
                    timeout=self.state.timeouts.cleanup_s,
                )
            except Exception:
                pass


class DaytonaSandboxClient(BaseSandboxClient[DaytonaSandboxClientOptions]):
    """Daytona sandbox client managing sandbox lifecycle via AsyncDaytona."""

    backend_id = "daytona"
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        AsyncDaytona, DaytonaConfig, _, _ = _import_daytona_sdk()
        config = DaytonaConfig(api_key=api_key, api_url=api_url) if (api_key or api_url) else None
        self._daytona = AsyncDaytona(config)
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def _build_create_params(
        self,
        *,
        sandbox_snapshot_name: str | None,
        image: str | None,
        env_vars: dict[str, str] | None,
        manifest: Manifest,
        name: str | None = None,
        resources: DaytonaSandboxResources | None = None,
        auto_stop_interval: int | None = None,
    ) -> Any:
        _, _, CreateSandboxFromSnapshotParams, CreateSandboxFromImageParams = _import_daytona_sdk()
        base_envs = dict(env_vars or {})
        creation_envs = base_envs or None

        if sandbox_snapshot_name:
            return CreateSandboxFromSnapshotParams(
                snapshot=sandbox_snapshot_name,
                env_vars=creation_envs,
                name=name,
                auto_stop_interval=auto_stop_interval,
            )

        if image:
            sandbox_resources = None
            if resources is not None and any(
                v is not None for v in (resources.cpu, resources.memory, resources.disk)
            ):
                Resources = _import_sdk_resources()
                sandbox_resources = Resources(
                    cpu=resources.cpu,
                    memory=resources.memory,
                    disk=resources.disk,
                )
            return CreateSandboxFromImageParams(
                image=image,
                env_vars=creation_envs,
                name=name,
                resources=sandbox_resources,
                auto_stop_interval=auto_stop_interval,
            )

        return CreateSandboxFromSnapshotParams(
            env_vars=creation_envs,
            name=name,
            auto_stop_interval=auto_stop_interval,
        )

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: DaytonaSandboxClientOptions,
    ) -> SandboxSession:
        if manifest is None:
            manifest = Manifest(root=DEFAULT_DAYTONA_WORKSPACE_ROOT)

        timeouts_in = options.timeouts
        if isinstance(timeouts_in, DaytonaSandboxTimeouts):
            timeouts = timeouts_in
        elif timeouts_in is None:
            timeouts = DaytonaSandboxTimeouts()
        else:
            timeouts = DaytonaSandboxTimeouts.model_validate(timeouts_in)

        session_id = uuid.uuid4()
        sandbox_name = options.name or str(session_id)

        params = await self._build_create_params(
            sandbox_snapshot_name=options.sandbox_snapshot_name,
            image=options.image,
            env_vars=options.env_vars,
            manifest=manifest,
            name=sandbox_name,
            resources=options.resources,
            auto_stop_interval=options.auto_stop_interval,
        )
        daytona_sandbox = await self._daytona.create(params, timeout=options.create_timeout)

        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = DaytonaSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            sandbox_id=daytona_sandbox.id,
            sandbox_snapshot_name=options.sandbox_snapshot_name,
            image=options.image,
            base_env_vars=dict(options.env_vars or {}),
            pause_on_exit=options.pause_on_exit,
            create_timeout=options.create_timeout,
            start_timeout=options.start_timeout,
            name=sandbox_name,
            resources=options.resources,
            auto_stop_interval=options.auto_stop_interval,
            timeouts=timeouts,
            exposed_ports=options.exposed_ports,
            exposed_port_url_ttl_s=options.exposed_port_url_ttl_s,
        )
        inner = DaytonaSandboxSession.from_state(state, sandbox=daytona_sandbox)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def close(self) -> None:
        """Close the underlying AsyncDaytona HTTP client session."""
        await self._daytona.close()

    async def __aenter__(self) -> DaytonaSandboxClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, DaytonaSandboxSession):
            raise TypeError("DaytonaSandboxClient.delete expects a DaytonaSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        if not isinstance(state, DaytonaSandboxSessionState):
            raise TypeError("DaytonaSandboxClient.resume expects a DaytonaSandboxSessionState")

        daytona_sandbox = None
        reconnected = False
        try:
            daytona_sandbox = await self._daytona.get(state.sandbox_id)
            SandboxState = _import_sandbox_state()
            if getattr(daytona_sandbox, "state", None) != SandboxState.STARTED:
                await daytona_sandbox.start(timeout=state.start_timeout)
            reconnected = True
        except Exception as e:
            logger.debug("daytona sandbox get() failed, will recreate: %s", e)

        if not reconnected or daytona_sandbox is None:
            params = await self._build_create_params(
                sandbox_snapshot_name=state.sandbox_snapshot_name,
                image=state.image,
                env_vars=state.base_env_vars,
                manifest=state.manifest,
                name=state.name,
                resources=state.resources,
                auto_stop_interval=state.auto_stop_interval,
            )
            daytona_sandbox = await self._daytona.create(params, timeout=state.create_timeout)
            state.sandbox_id = daytona_sandbox.id
            state.workspace_root_ready = False

        inner = DaytonaSandboxSession.from_state(state, sandbox=daytona_sandbox)
        inner._set_start_state_preserved(reconnected, system=reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return DaytonaSandboxSessionState.model_validate(payload)


__all__ = [
    "DEFAULT_DAYTONA_WORKSPACE_ROOT",
    "DaytonaSandboxResources",
    "DaytonaSandboxClient",
    "DaytonaSandboxClientOptions",
    "DaytonaSandboxSession",
    "DaytonaSandboxSessionState",
    "DaytonaSandboxTimeouts",
]

"""
E2B sandbox (https://e2b.dev) implementation.

Create an E2B account and export `E2B_API_KEY` to configure E2B locally.

This module provides an E2B-backed sandbox client/session implementation backed by
the E2B SDK sandbox classes.

Note: The `e2b` and `e2b-code-interpreter` dependencies are intended to be optional
(installed via extras), so package-level exports should guard imports of this module.
Within this module, E2B SDK imports are lazy so users without the extra can still
import the package.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import io
import json
import logging
import shlex
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, NoReturn, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ....sandbox.entries import Mount
from ....sandbox.errors import (
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
    iter_exception_chain,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import posix_path_for_error, sandbox_path_str

WorkspacePersistenceMode = Literal["tar", "snapshot"]
E2BTimeoutAction = Literal["kill", "pause"]

_WORKSPACE_PERSISTENCE_TAR: WorkspacePersistenceMode = "tar"
_WORKSPACE_PERSISTENCE_SNAPSHOT: WorkspacePersistenceMode = "snapshot"

# Magic prefix for native E2B snapshot payloads that cannot be represented as tar bytes.
_E2B_SANDBOX_SNAPSHOT_MAGIC = b"E2B_SANDBOX_SNAPSHOT_V1\n"
logger = logging.getLogger(__name__)


def _raise_e2b_exec_error(
    exc: BaseException,
    *,
    command: Sequence[str | Path],
    timeout: float | None,
    timeout_exc: type[BaseException] | None,
) -> NoReturn:
    """Classify an E2B exception and raise the appropriate ExecFailureError."""
    # Build context from the exception chain.
    ctx: dict[str, object] = {}
    msg = str(exc).strip()
    ctx["provider_error"] = msg if msg else type(exc).__name__
    for attr in ("stdout", "stderr"):
        val = next(
            (
                str(v).strip()
                for c in iter_exception_chain(exc)
                if (v := getattr(c, attr, None)) and str(v).strip()
            ),
            None,
        )
        if val:
            ctx[attr] = val

    chain = list(iter_exception_chain(exc))

    # Sandbox gone — always a transport error.
    if any("sandbox" in str(c).lower() and "not found" in str(c).lower() for c in chain):
        ctx.setdefault("reason", "sandbox_not_found")
        raise ExecTransportError(command=command, context=ctx, cause=exc) from exc

    # E2B timeout or httpcore read timeout.
    is_timeout = timeout_exc is not None and exception_chain_contains_type(exc, (timeout_exc,))
    if not is_timeout and any(
        type(c).__name__ == "ReadTimeout" and type(c).__module__.startswith("httpcore")
        for c in chain
    ):
        ctx.setdefault("reason", "stream_read_timeout")
        is_timeout = True

    if is_timeout:
        raise ExecTimeoutError(
            command=command,
            timeout_s=timeout,
            context=ctx,
            cause=exc,
        ) from exc

    raise ExecTransportError(command=command, context=ctx, cause=exc) from exc


def _encode_e2b_snapshot_ref(*, snapshot_id: str) -> bytes:
    body = json.dumps({"snapshot_id": snapshot_id}, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return _E2B_SANDBOX_SNAPSHOT_MAGIC + body


def _decode_e2b_snapshot_ref(raw: bytes) -> str | None:
    if not raw.startswith(_E2B_SANDBOX_SNAPSHOT_MAGIC):
        return None
    body = raw[len(_E2B_SANDBOX_SNAPSHOT_MAGIC) :]
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    snapshot_id = obj.get("snapshot_id") if isinstance(obj, dict) else None
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


class _E2BFilesAPI:
    async def write(
        self,
        path: str,
        data: bytes,
        request_timeout: float | None = None,
    ) -> object:
        raise NotImplementedError

    async def remove(self, path: str, request_timeout: float | None = None) -> object:
        raise NotImplementedError

    async def make_dir(self, path: str, request_timeout: float | None = None) -> object:
        raise NotImplementedError

    async def read(self, path: str, format: str = "bytes") -> object:
        raise NotImplementedError


class _E2BCommandsAPI:
    async def run(
        self,
        command: str,
        background: bool | None = None,
        envs: dict[str, str] | None = None,
        user: str | User | None = None,
        cwd: str | None = None,
        on_stdout: object | None = None,
        on_stderr: object | None = None,
        stdin: bool | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> object:
        raise NotImplementedError


class _E2BPtyAPI:
    async def create(
        self,
        *,
        size: object,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        on_data: object | None = None,
    ) -> object:
        raise NotImplementedError

    async def send_stdin(
        self,
        pid: object,
        data: bytes,
        request_timeout: float | None = None,
    ) -> object:
        raise NotImplementedError


class _E2BSandboxAPI:
    sandbox_id: object
    files: _E2BFilesAPI
    commands: _E2BCommandsAPI
    pty: _E2BPtyAPI
    connection_config: object

    async def pause(self) -> object:
        raise NotImplementedError

    async def kill(self) -> object:
        raise NotImplementedError

    async def is_running(self, request_timeout: float | None = None) -> object:
        raise NotImplementedError

    def get_host(self, port: int) -> str:
        raise NotImplementedError

    async def create_snapshot(self, **opts: object) -> object:
        raise NotImplementedError


class _E2BSandboxFactoryAPI:
    async def create(
        self,
        *,
        template: str | None = None,
        timeout: int | None = None,
        metadata: dict[str, str] | None = None,
        envs: dict[str, str] | None = None,
        secure: bool = True,
        allow_internet_access: bool = True,
        network: dict[str, object] | None = None,
        lifecycle: dict[str, object] | None = None,
        mcp: dict[str, dict[str, str]] | None = None,
    ) -> object:
        raise NotImplementedError

    async def _cls_connect(
        self,
        *,
        sandbox_id: str,
        timeout: int | None = None,
    ) -> object:
        raise NotImplementedError

    async def _cls_connect_sandbox(
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
    """Supported E2B sandbox interfaces."""

    CODE_INTERPRETER = "e2b_code_interpreter"
    E2B = "e2b"


def _coerce_sandbox_type(value: E2BSandboxType | str | None) -> E2BSandboxType:
    if value is None:
        raise ValueError(
            "E2BSandboxClientOptions.sandbox_type is required. "
            "Use one of: e2b_code_interpreter, e2b."
        )
    if isinstance(value, E2BSandboxType):
        return value
    try:
        return E2BSandboxType(value)
    except ValueError as e:
        raise ValueError(
            "Invalid E2BSandboxClientOptions.sandbox_type. Use one of: e2b_code_interpreter, e2b."
        ) from e


def _import_sandbox_class(sandbox_type: E2BSandboxType) -> _E2BSandboxFactoryAPI:
    if sandbox_type is E2BSandboxType.CODE_INTERPRETER:
        module_name = "e2b_code_interpreter"
        missing_msg = (
            "E2BSandboxClient requires the optional `e2b-code-interpreter` dependency.\n"
            "Install the E2B extra before using this sandbox backend."
        )
    else:
        module_name = "e2b"
        missing_msg = (
            "E2BSandboxClient requires the optional `e2b` dependency.\n"
            "Install the E2B extra before using this sandbox backend."
        )

    try:
        module = __import__(module_name, fromlist=["AsyncSandbox"])
        Sandbox = module.AsyncSandbox
    except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
        if module_name == "e2b":
            try:
                module = __import__("e2b.sandbox", fromlist=["AsyncSandbox"])
                Sandbox = module.AsyncSandbox
            except Exception:
                raise ImportError(missing_msg) from e
        else:
            raise ImportError(missing_msg) from e

    return cast(_E2BSandboxFactoryAPI, Sandbox)


def _as_sandbox_api(sandbox: object) -> _E2BSandboxAPI:
    return cast(_E2BSandboxAPI, sandbox)


def _sandbox_id(sandbox: object) -> object:
    return _as_sandbox_api(sandbox).sandbox_id


async def _sandbox_write_file(
    sandbox: object,
    path: str,
    data: bytes,
    *,
    request_timeout: float | None = None,
) -> object:
    return await _as_sandbox_api(sandbox).files.write(
        path,
        data,
        request_timeout=request_timeout,
    )


async def _sandbox_remove_file(
    sandbox: object,
    path: str,
    *,
    request_timeout: float | None = None,
) -> object:
    return await _as_sandbox_api(sandbox).files.remove(path, request_timeout=request_timeout)


async def _sandbox_make_dir(
    sandbox: object,
    path: str,
    *,
    request_timeout: float | None = None,
) -> object:
    return await _as_sandbox_api(sandbox).files.make_dir(path, request_timeout=request_timeout)


async def _sandbox_read_file(sandbox: object, path: str, *, format: str = "bytes") -> object:
    return await _as_sandbox_api(sandbox).files.read(path, format=format)


async def _sandbox_run_command(
    sandbox: object,
    command: str,
    *,
    timeout: float | None = None,
    cwd: str | None = None,
    envs: dict[str, str] | None = None,
    user: str | None = None,
) -> object:
    return await _as_sandbox_api(sandbox).commands.run(
        command,
        timeout=timeout,
        cwd=cwd,
        envs=envs,
        user=user,
    )


async def _sandbox_pause(sandbox: object) -> object:
    return await _as_sandbox_api(sandbox).pause()


async def _sandbox_kill(sandbox: object) -> object:
    return await _as_sandbox_api(sandbox).kill()


async def _sandbox_is_running(sandbox: object, *, request_timeout: float | None = None) -> object:
    return await _as_sandbox_api(sandbox).is_running(request_timeout=request_timeout)


def _sandbox_get_host(sandbox: object, port: int) -> str:
    return _as_sandbox_api(sandbox).get_host(port)


async def _sandbox_create_snapshot(sandbox: object) -> object:
    return await _as_sandbox_api(sandbox).create_snapshot()


async def _sandbox_create(
    sandbox_class: _E2BSandboxFactoryAPI,
    *,
    template: str | None = None,
    timeout: int | None = None,
    metadata: dict[str, str] | None = None,
    envs: dict[str, str] | None = None,
    secure: bool = True,
    allow_internet_access: bool = True,
    network: dict[str, object] | None = None,
    lifecycle: dict[str, object] | None = None,
    mcp: dict[str, dict[str, str]] | None = None,
) -> object:
    create_callable = cast(Callable[..., Awaitable[object]], sandbox_class.create)
    try:
        create_params: Mapping[str, inspect.Parameter] | None = inspect.signature(
            sandbox_class.create
        ).parameters
    except (TypeError, ValueError):
        create_params = None
    accepts_var_kwargs = bool(
        create_params
        and any(param.kind == inspect.Parameter.VAR_KEYWORD for param in create_params.values())
    )
    create_kwargs: dict[str, object] = {
        "template": template,
        "timeout": timeout,
        "metadata": metadata,
        "envs": envs,
        "secure": secure,
        "allow_internet_access": allow_internet_access,
        "network": network,
    }
    if mcp is not None:
        create_kwargs["mcp"] = mcp

    if lifecycle is not None and (
        accepts_var_kwargs or (create_params is not None and "lifecycle" in create_params)
    ):
        create_kwargs["lifecycle"] = lifecycle

    if create_params is not None and not accepts_var_kwargs:
        create_kwargs = {key: value for key, value in create_kwargs.items() if key in create_params}

    return await create_callable(**create_kwargs)


def _e2b_lifecycle(
    on_timeout: E2BTimeoutAction,
    *,
    auto_resume: bool,
) -> dict[str, object]:
    lifecycle: dict[str, object] = {"on_timeout": on_timeout}
    if on_timeout == "pause":
        lifecycle["auto_resume"] = auto_resume
    return lifecycle


async def _sandbox_connect(
    sandbox_class: _E2BSandboxFactoryAPI,
    *,
    sandbox_id: str,
    timeout: int | None = None,
) -> object:
    # In the Python SDK, `Sandbox._cls_connect(...)` returns the low-level API model, while the
    # public classmethod variant `Sandbox.connect(...)` / private `_cls_connect_sandbox(...)`
    # returns the full sandbox wrapper with `.files`, `.commands`, etc.
    connect = getattr(sandbox_class, "connect", None)
    if callable(connect):
        try:
            return await connect(sandbox_id=sandbox_id, timeout=timeout)
        except TypeError:
            pass

    connect_sandbox = getattr(sandbox_class, "_cls_connect_sandbox", None)
    if callable(connect_sandbox):
        return await connect_sandbox(sandbox_id=sandbox_id, timeout=timeout)

    return await sandbox_class._cls_connect(sandbox_id=sandbox_id, timeout=timeout)


def _import_e2b_exceptions() -> Mapping[str, type[BaseException]]:
    """Best-effort import of E2B exception classes for classification."""

    try:
        from e2b.exceptions import (
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
        from e2b.sandbox.commands.command_handle import (
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


class E2BSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the E2B sandbox."""

    type: Literal["e2b"] = "e2b"
    sandbox_type: E2BSandboxType | str
    template: str | None = None
    timeout: int | None = None
    metadata: dict[str, str] | None = None
    envs: dict[str, str] | None = None
    secure: bool = True
    allow_internet_access: bool = True
    timeouts: E2BSandboxTimeouts | dict[str, object] | None = None
    pause_on_exit: bool = False
    exposed_ports: tuple[int, ...] = ()
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    on_timeout: E2BTimeoutAction = "pause"
    auto_resume: bool = True
    mcp: dict[str, dict[str, str]] | None = None

    def __init__(
        self,
        sandbox_type: E2BSandboxType | str,
        template: str | None = None,
        timeout: int | None = None,
        metadata: dict[str, str] | None = None,
        envs: dict[str, str] | None = None,
        secure: bool = True,
        allow_internet_access: bool = True,
        timeouts: E2BSandboxTimeouts | dict[str, object] | None = None,
        pause_on_exit: bool = False,
        exposed_ports: tuple[int, ...] = (),
        workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR,
        on_timeout: E2BTimeoutAction = "pause",
        auto_resume: bool = True,
        mcp: dict[str, dict[str, str]] | None = None,
        *,
        type: Literal["e2b"] = "e2b",
    ) -> None:
        super().__init__(
            type=type,
            sandbox_type=sandbox_type,
            template=template,
            timeout=timeout,
            metadata=metadata,
            envs=envs,
            secure=secure,
            allow_internet_access=allow_internet_access,
            timeouts=timeouts,
            pause_on_exit=pause_on_exit,
            exposed_ports=exposed_ports,
            workspace_persistence=workspace_persistence,
            on_timeout=on_timeout,
            auto_resume=auto_resume,
            mcp=mcp,
        )


class E2BSandboxSessionState(SandboxSessionState):
    type: Literal["e2b"] = "e2b"
    sandbox_id: str
    sandbox_type: E2BSandboxType = Field(default=E2BSandboxType.E2B)
    template: str | None = None
    sandbox_timeout: int | None = None
    metadata: dict[str, str] | None = None
    base_envs: dict[str, str] = Field(default_factory=dict)
    secure: bool = True
    allow_internet_access: bool = True
    timeouts: E2BSandboxTimeouts = Field(default_factory=E2BSandboxTimeouts)
    pause_on_exit: bool = False
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    on_timeout: E2BTimeoutAction = "pause"
    auto_resume: bool = True
    mcp: dict[str, dict[str, str]] | None = None


@dataclass
class _E2BPtyProcessEntry:
    handle: object
    tty: bool
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    last_used: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class _E2BPtySize:
    rows: int
    cols: int


class E2BSandboxSession(BaseSandboxSession):
    """E2B-backed sandbox session implementation."""

    state: E2BSandboxSessionState
    _sandbox: _E2BSandboxAPI
    _workspace_root_ready: bool
    _pty_lock: asyncio.Lock
    _pty_processes: dict[int, _E2BPtyProcessEntry]
    _reserved_pty_process_ids: set[int]

    def __init__(
        self,
        *,
        state: E2BSandboxSessionState,
        sandbox: object,
    ) -> None:
        self.state = state
        self._sandbox = _as_sandbox_api(sandbox)
        self._workspace_root_ready = state.workspace_root_ready
        self._pty_lock = asyncio.Lock()
        self._pty_processes = {}
        self._reserved_pty_process_ids = set()

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

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            host = _sandbox_get_host(self._sandbox, port)
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "e2b", "detail": "get_host_failed"},
                cause=e,
            ) from e

        endpoint = _e2b_endpoint_from_host(host)
        if endpoint is None:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "e2b", "detail": "invalid_host", "host": host},
            )
        return endpoint

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
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
        if path.as_posix() == "/":
            return
        try:
            await _sandbox_make_dir(
                self._sandbox,
                sandbox_path_str(path),
                request_timeout=self.state.timeouts.fast_op_s,
            )
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            raise WorkspaceArchiveWriteError(path=path, context={"reason": reason}, cause=e) from e

    async def _ensure_workspace_root(self) -> None:
        """Ensure the workspace root exists before materialization starts."""
        await self._ensure_dir(self._workspace_root_path(), reason="root_make_failed")

    async def _prepare_workspace_root_for_exec(self) -> None:
        """Create the workspace root through the command API before using it as `cwd`."""
        root = self._workspace_root_path().as_posix()
        envs = await self._resolved_envs()
        result = await _sandbox_run_command(
            self._sandbox,
            f"mkdir -p -- {shlex.quote(root)}",
            timeout=self.state.timeouts.fast_op_s,
            cwd="/",
            envs=envs,
        )
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context={
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": exit_code,
                    "stderr": str(getattr(result, "stderr", "") or ""),
                },
            )
        self._workspace_root_ready = True

    def _mark_workspace_root_ready_from_probe(self) -> None:
        super()._mark_workspace_root_ready_from_probe()
        self._workspace_root_ready = True

    async def _prepare_backend_workspace(self) -> None:
        try:
            if self._workspace_state_preserved_on_start():
                # Reconnected sandboxes may have durable workspace contents; the base start flow
                # probes before this provider creates the root for future exec calls.
                if not self._workspace_root_ready:
                    await self._prepare_workspace_root_for_exec()
            else:
                # Fresh or recreated sandboxes need the workspace root created before snapshot
                # hydration or full manifest materialization can write into it.
                await self._ensure_workspace_root()
                await self._prepare_workspace_root_for_exec()
        except WorkspaceStartError:
            raise
        except Exception as e:
            raise WorkspaceStartError(path=self._workspace_root_path(), cause=e) from e

    async def _after_start(self) -> None:
        # Native E2B snapshot hydration can replace the sandbox and sandbox id; reinstall runtime
        # helpers only when the helper cache now points at a different backend.
        if self._runtime_helper_cache_key != self._current_runtime_helper_cache_key():
            await self._ensure_runtime_helpers()

    async def _shutdown_backend(self) -> None:
        # Best-effort kill of the remote sandbox.
        try:
            if self.state.pause_on_exit:
                await _sandbox_pause(self._sandbox)
            else:
                await _sandbox_kill(self._sandbox)
        except Exception as e:
            if self.state.pause_on_exit:
                logger.warning(
                    "Failed to pause E2B sandbox on shutdown; falling back to kill.",
                    extra={
                        "sandbox_id": self.state.sandbox_id,
                        "pause_on_exit": self.state.pause_on_exit,
                    },
                    exc_info=e,
                )
                try:
                    await _sandbox_kill(self._sandbox)
                except Exception as kill_exc:
                    logger.warning(
                        "Failed to kill E2B sandbox after pause fallback failure.",
                        extra={
                            "sandbox_id": self.state.sandbox_id,
                            "pause_on_exit": self.state.pause_on_exit,
                        },
                        exc_info=kill_exc,
                    )
            else:
                logger.warning(
                    "Failed to kill E2B sandbox on shutdown.",
                    extra={
                        "sandbox_id": self.state.sandbox_id,
                        "pause_on_exit": self.state.pause_on_exit,
                    },
                    exc_info=e,
                )

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
            result = await _sandbox_run_command(
                self._sandbox,
                cmd_str,
                timeout=exec_timeout,
                cwd=cwd,
                envs=envs,
                user=user,
            )
            return ExecResult(
                stdout=str(getattr(result, "stdout", "") or "").encode("utf-8", errors="replace"),
                stderr=str(getattr(result, "stderr", "") or "").encode("utf-8", errors="replace"),
                exit_code=int(getattr(result, "exit_code", 0) or 0),
            )
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            if command_exit_exc is not None and isinstance(e, command_exit_exc):
                exit_code = int(getattr(e, "exit_code", 1) or 1)
                stdout = str(getattr(e, "stdout", "") or "")
                stderr = str(getattr(e, "stderr", "") or "")
                return ExecResult(
                    stdout=stdout.encode("utf-8", errors="replace"),
                    stderr=stderr.encode("utf-8", errors="replace"),
                    exit_code=exit_code,
                )

            _raise_e2b_exec_error(
                e,
                command=command,
                timeout=timeout,
                timeout_exc=timeout_exc,
            )

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
        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=user)
        command_text = shlex.join(str(part) for part in sanitized_command)
        envs = await self._resolved_envs()
        cwd = self.state.manifest.root if self._workspace_root_ready else None
        exec_timeout = self._coerce_exec_timeout(timeout)
        e2b_exc = _import_e2b_exceptions()
        timeout_exc = e2b_exc.get("timeout")

        entry = _E2BPtyProcessEntry(handle=None, tty=tty)

        async def _append_output(payload: bytes | bytearray | str | object) -> None:
            if isinstance(payload, bytes):
                chunk = payload
            elif isinstance(payload, bytearray):
                chunk = bytes(payload)
            elif isinstance(payload, str):
                chunk = payload.encode("utf-8", errors="replace")
            else:
                chunk = str(payload).encode("utf-8", errors="replace")

            async with entry.output_lock:
                entry.output_chunks.append(chunk)
            entry.output_notify.set()

        registered = False
        pruned_entry: _E2BPtyProcessEntry | None = None
        process_id = 0
        process_count = 0
        try:
            if tty:
                handle = await self._sandbox.pty.create(
                    size=_E2BPtySize(rows=24, cols=80),
                    cwd=cwd,
                    envs=envs,
                    timeout=exec_timeout,
                    on_data=_append_output,
                )
                entry.handle = handle
                await self._sandbox.pty.send_stdin(
                    cast(Any, handle).pid,
                    f"{command_text}\n".encode(),
                    request_timeout=self.state.timeouts.fast_op_s,
                )
            else:
                handle = await self._sandbox.commands.run(
                    command_text,
                    background=True,
                    cwd=cwd,
                    envs=envs,
                    timeout=exec_timeout,
                    stdin=False,
                    on_stdout=_append_output,
                    on_stderr=_append_output,
                )
                entry.handle = handle
            async with self._pty_lock:
                process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
                self._reserved_pty_process_ids.add(process_id)
                pruned_entry = self._prune_pty_processes_if_needed()
                self._pty_processes[process_id] = entry
                process_count = len(self._pty_processes)
                registered = True
        except asyncio.CancelledError:
            if not registered and entry.handle is not None:
                await self._terminate_pty_entry(entry)
            raise
        except Exception as e:
            if not registered and entry.handle is not None:
                await self._terminate_pty_entry(entry)
            if isinstance(e, ExecTransportError):
                raise
            _raise_e2b_exec_error(
                e,
                command=command,
                timeout=timeout,
                timeout_exc=timeout_exc,
            )

        if pruned_entry is not None:
            await self._terminate_pty_entry(pruned_entry)

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
                pty_processes=self._pty_processes,
                session_id=session_id,
            )

        if chars:
            if not entry.tty:
                raise RuntimeError("stdin is not available for this process")
            await self._sandbox.pty.send_stdin(
                cast(Any, entry.handle).pid,
                chars.encode("utf-8"),
                request_timeout=self.state.timeouts.fast_op_s,
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
            entries = list(self._pty_processes.values())
            self._pty_processes.clear()
            self._reserved_pty_process_ids.clear()

        for entry in entries:
            await self._terminate_pty_entry(entry)

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            await self._check_read_with_exec(path, user=user)

        workspace_path = await self._validate_path_access(path)

        e2b_exc = _import_e2b_exceptions()
        not_found_exc = e2b_exc.get("not_found")

        try:
            content = await _sandbox_read_file(
                self._sandbox, sandbox_path_str(workspace_path), format="bytes"
            )
            if isinstance(content, bytes | bytearray):
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

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            await self._check_write_with_exec(path, user=user)

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=path, actual_type=type(payload).__name__)

        workspace_path = await self._validate_path_access(path, for_write=True)

        try:
            await _sandbox_write_file(
                self._sandbox,
                sandbox_path_str(workspace_path),
                bytes(payload),
                request_timeout=self.state.timeouts.file_upload_s,
            )
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        if not self._workspace_root_ready:
            return False
        try:
            return bool(
                await _sandbox_is_running(
                    self._sandbox,
                    request_timeout=self.state.timeouts.keepalive_s,
                )
            )
        except Exception:
            return False

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

        if user is None and not parents:
            parent = path.parent
            test = await self.exec("test", "-d", str(parent), shell=False)
            if not test.ok():
                raise ExecNonZeroError(test, command=("test", "-d", str(parent)))
        await self._ensure_dir(path, reason="mkdir_failed")

    async def _collect_pty_output(
        self,
        *,
        entry: _E2BPtyProcessEntry,
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

            if self._entry_exit_code(entry) is not None:
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
        truncated_text, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
        return truncated_text.encode("utf-8", errors="replace"), original_token_count

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _E2BPtyProcessEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        exit_code = self._entry_exit_code(entry)
        live_process_id: int | None = process_id

        if exit_code is not None:
            async with self._pty_lock:
                removed = self._pty_processes.pop(process_id, None)
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

    def _prune_pty_processes_if_needed(self) -> _E2BPtyProcessEntry | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None

        meta: list[tuple[int, float, bool]] = [
            (process_id, entry.last_used, self._entry_exit_code(entry) is not None)
            for process_id, entry in self._pty_processes.items()
        ]
        process_id = process_id_to_prune_from_meta(meta)
        if process_id is None:
            return None

        self._reserved_pty_process_ids.discard(process_id)
        return self._pty_processes.pop(process_id, None)

    def _entry_exit_code(self, entry: _E2BPtyProcessEntry) -> int | None:
        value = getattr(entry.handle, "exit_code", None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _terminate_pty_entry(self, entry: _E2BPtyProcessEntry) -> None:
        kill = getattr(entry.handle, "kill", None)
        if callable(kill):
            try:
                await kill()
            except Exception:
                pass

    def _tar_exclude_args(self) -> list[str]:
        return shell_tar_exclude_args(self._persist_workspace_skip_relpaths())

    @retry_async(
        retry_if=lambda exc, self, tar_cmd: (
            exception_chain_contains_type(exc, _retryable_persist_workspace_error_types())
            or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
        )
    )
    async def _run_persist_workspace_command(self, tar_cmd: str) -> str:
        error_root = posix_path_for_error(self._workspace_root_path())
        try:
            envs = await self._resolved_envs()
            result = await _sandbox_run_command(
                self._sandbox,
                tar_cmd,
                timeout=self.state.timeouts.snapshot_tar_s,
                cwd="/",
                envs=envs,
            )
            exit_code = int(getattr(result, "exit_code", 0) or 0)
            if exit_code != 0:
                raise WorkspaceArchiveReadError(
                    path=error_root,
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
            raise WorkspaceArchiveReadError(path=error_root, cause=e) from e

    async def persist_workspace(self) -> io.IOBase:
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT:
            return await self._persist_workspace_via_snapshot()
        return await self._persist_workspace_via_tar()

    async def _persist_workspace_via_snapshot(self) -> io.IOBase:
        """
        Persist with E2B's native sandbox snapshot API.

        Fall back to tar when there are plain non-mount skip paths, because native snapshots
        capture the whole sandbox and the E2B API does not provide path-level excludes.
        """

        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        if not hasattr(self._sandbox, "create_snapshot"):
            return await self._persist_workspace_via_tar()
        if self._native_snapshot_requires_tar_fallback():
            return await self._persist_workspace_via_tar()

        skip = self._persist_workspace_skip_relpaths()
        mount_targets = self.state.manifest.ephemeral_mount_targets()
        mount_skip_rel_paths: set[Path] = set()
        for _mount_entry, mount_path in mount_targets:
            try:
                mount_skip_rel_paths.add(mount_path.relative_to(root))
            except ValueError:
                continue
        if skip - mount_skip_rel_paths:
            return await self._persist_workspace_via_tar()

        unmounted_mounts: list[tuple[Mount, Path]] = []
        unmount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in mount_targets:
            try:
                await mount_entry.mount_strategy.teardown_for_snapshot(
                    mount_entry, self, mount_path
                )
            except Exception as e:
                unmount_error = WorkspaceArchiveReadError(path=error_root, cause=e)
                break
            unmounted_mounts.append((mount_entry, mount_path))

        snapshot_error: WorkspaceArchiveReadError | None = None
        snapshot_id: str | None = None
        if unmount_error is None:
            try:
                snap = await asyncio.wait_for(
                    _sandbox_create_snapshot(self._sandbox),
                    timeout=self.state.timeouts.snapshot_tar_s,
                )
                snapshot_id = getattr(snap, "snapshot_id", None)
                if not isinstance(snapshot_id, str) or not snapshot_id:
                    raise WorkspaceArchiveReadError(
                        path=error_root,
                        context={
                            "reason": "native_snapshot_unexpected_return",
                            "type": type(snap).__name__,
                        },
                    )
            except WorkspaceArchiveReadError as e:
                snapshot_error = e
            except Exception as e:
                snapshot_error = WorkspaceArchiveReadError(
                    path=error_root, context={"reason": "native_snapshot_failed"}, cause=e
                )

        remount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in reversed(unmounted_mounts):
            try:
                await mount_entry.mount_strategy.restore_after_snapshot(
                    mount_entry, self, mount_path
                )
            except Exception as e:
                current_error = WorkspaceArchiveReadError(path=error_root, cause=e)
                if remount_error is None:
                    remount_error = current_error
                else:
                    additional_remount_errors = remount_error.context.setdefault(
                        "additional_remount_errors", []
                    )
                    assert isinstance(additional_remount_errors, list)
                    additional_remount_errors.append(
                        {
                            "message": current_error.message,
                            "cause_type": type(e).__name__,
                            "cause": str(e),
                        }
                    )

        if remount_error is not None:
            if snapshot_error is not None:
                remount_error.context["snapshot_error_before_remount_corruption"] = {
                    "message": snapshot_error.message
                }
            raise remount_error
        if unmount_error is not None:
            raise unmount_error
        if snapshot_error is not None:
            raise snapshot_error

        assert snapshot_id is not None
        return io.BytesIO(_encode_e2b_snapshot_ref(snapshot_id=snapshot_id))

    async def _persist_workspace_via_tar(self) -> io.IOBase:
        def _error_context_summary(error: WorkspaceArchiveReadError) -> dict[str, str]:
            summary = {"message": error.message}
            if error.cause is not None:
                summary["cause_type"] = type(error.cause).__name__
                summary["cause"] = str(error.cause)
            return summary

        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        excludes = " ".join(self._tar_exclude_args())
        tar_cmd = f"tar {excludes} -C {shlex.quote(root.as_posix())} -cf - . | base64 -w0"
        unmounted_mounts: list[tuple[Mount, Path]] = []
        unmount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in self.state.manifest.ephemeral_mount_targets():
            try:
                await mount_entry.mount_strategy.teardown_for_snapshot(
                    mount_entry, self, mount_path
                )
            except Exception as e:
                unmount_error = WorkspaceArchiveReadError(path=error_root, cause=e)
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
                        path=error_root,
                        context={"reason": "snapshot_invalid_base64"},
                        cause=e,
                    ) from e
            except WorkspaceArchiveReadError as e:
                snapshot_error = e

        remount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in reversed(unmounted_mounts):
            try:
                await mount_entry.mount_strategy.restore_after_snapshot(
                    mount_entry, self, mount_path
                )
            except Exception as e:
                current_error = WorkspaceArchiveReadError(path=error_root, cause=e)
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

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        tar_path = f"/tmp/sandbox-hydrate-{self.state.session_id.hex}.tar"

        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=Path(tar_path), actual_type=type(raw).__name__)

        snapshot_id = _decode_e2b_snapshot_ref(bytes(raw))
        if snapshot_id is not None:
            try:
                try:
                    await _sandbox_kill(self._sandbox)
                except Exception:
                    pass

                sandbox_type = _coerce_sandbox_type(self.state.sandbox_type)
                SandboxClass = _import_sandbox_class(sandbox_type)
                base_envs = dict(self.state.base_envs)
                manifest_envs = await self.state.manifest.environment.resolve()
                envs = {**base_envs, **manifest_envs} or None
                network_config = _e2b_network_config(self.state.exposed_ports)

                sandbox = await _sandbox_create(
                    SandboxClass,
                    template=snapshot_id,
                    timeout=self.state.sandbox_timeout,
                    metadata=self.state.metadata,
                    envs=envs,
                    secure=self.state.secure,
                    allow_internet_access=self.state.allow_internet_access,
                    network=network_config,
                    lifecycle=_e2b_lifecycle(
                        self.state.on_timeout, auto_resume=self.state.auto_resume
                    ),
                    mcp=self.state.mcp,
                )
                self._sandbox = _as_sandbox_api(sandbox)
                self.state.sandbox_id = str(_sandbox_id(sandbox))
                self._workspace_root_ready = True
                return
            except Exception as e:
                raise WorkspaceArchiveWriteError(
                    path=error_root,
                    context={
                        "reason": "native_snapshot_restore_failed",
                        "snapshot_id": snapshot_id,
                    },
                    cause=e,
                ) from e

        try:
            validate_tar_bytes(bytes(raw))
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=error_root,
                context={
                    "reason": "unsafe_or_invalid_tar",
                    "member": e.member,
                    "detail": str(e),
                },
                cause=e,
            ) from e

        try:
            await self._ensure_workspace_root()
            envs = await self._resolved_envs()
            await _sandbox_write_file(
                self._sandbox,
                tar_path,
                bytes(raw),
                request_timeout=self.state.timeouts.file_upload_s,
            )
            result = await _sandbox_run_command(
                self._sandbox,
                f"tar -C {shlex.quote(root.as_posix())} -xf {shlex.quote(tar_path)}",
                timeout=self.state.timeouts.snapshot_tar_s,
                cwd="/",
                envs=envs,
            )
            exit_code = int(getattr(result, "exit_code", 0) or 0)
            if exit_code != 0:
                raise WorkspaceArchiveWriteError(
                    path=error_root,
                    context={
                        "reason": "hydrate_nonzero_exit",
                        "exit_code": exit_code,
                        "stderr": str(getattr(result, "stderr", "") or ""),
                    },
                )
            self._workspace_root_ready = True
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:  # pragma: no cover - exercised via unit tests with fakes
            raise WorkspaceArchiveWriteError(path=error_root, cause=e) from e
        finally:
            try:
                envs = await self._resolved_envs()
                await _sandbox_run_command(
                    self._sandbox,
                    f"rm -f -- {shlex.quote(tar_path)}",
                    timeout=self.state.timeouts.cleanup_s,
                    cwd="/",
                    envs=envs,
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
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: E2BSandboxClientOptions,
    ) -> SandboxSession:
        if options is None:
            raise ValueError("E2BSandboxClient.create requires options")
        manifest = manifest or Manifest()

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
        network_config = _e2b_network_config(options.exposed_ports)

        workspace_persistence = options.workspace_persistence
        if workspace_persistence not in (
            _WORKSPACE_PERSISTENCE_TAR,
            _WORKSPACE_PERSISTENCE_SNAPSHOT,
        ):
            raise ValueError(
                "E2BSandboxClient.create requires workspace_persistence to be one of "
                f"{_WORKSPACE_PERSISTENCE_TAR!r} or {_WORKSPACE_PERSISTENCE_SNAPSHOT!r}"
            )

        SandboxClass = _import_sandbox_class(sandbox_type)
        sandbox = await _sandbox_create(
            SandboxClass,
            template=options.template,
            timeout=options.timeout,
            metadata=options.metadata,
            envs=envs,
            secure=options.secure,
            allow_internet_access=options.allow_internet_access,
            network=network_config,
            lifecycle=_e2b_lifecycle(options.on_timeout, auto_resume=options.auto_resume),
            mcp=options.mcp,
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
            workspace_persistence=workspace_persistence,
            on_timeout=options.on_timeout,
            auto_resume=options.auto_resume,
            mcp=options.mcp,
            exposed_ports=options.exposed_ports,
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
    ) -> SandboxSession:
        if not isinstance(state, E2BSandboxSessionState):
            raise TypeError("E2BSandboxClient.resume expects an E2BSandboxSessionState")

        sandbox_type = _coerce_sandbox_type(state.sandbox_type)
        SandboxClass = _import_sandbox_class(sandbox_type)

        base_envs = dict(state.base_envs)
        manifest_envs = await state.manifest.environment.resolve()
        envs = {**base_envs, **manifest_envs} or None
        network_config = _e2b_network_config(state.exposed_ports)
        preserves_timeout_paused_state = state.on_timeout == "pause"

        sandbox: object
        reconnected = False
        try:
            # `_cls_connect` is the current async entrypoint for re-attaching to a sandbox id.
            sandbox = await _sandbox_connect(
                SandboxClass,
                sandbox_id=state.sandbox_id,
                timeout=state.sandbox_timeout,
            )
            if not state.pause_on_exit and not preserves_timeout_paused_state:
                is_running = await _sandbox_is_running(
                    sandbox, request_timeout=state.timeouts.keepalive_s
                )
                if not is_running:
                    raise RuntimeError("sandbox_not_running")
            reconnected = True
        except Exception:
            sandbox = await _sandbox_create(
                SandboxClass,
                template=state.template,
                timeout=state.sandbox_timeout,
                metadata=state.metadata,
                envs=envs,
                secure=state.secure,
                allow_internet_access=state.allow_internet_access,
                network=network_config,
                lifecycle=_e2b_lifecycle(state.on_timeout, auto_resume=state.auto_resume),
                mcp=state.mcp,
            )
            state.sandbox_id = str(_sandbox_id(sandbox))
            state.workspace_root_ready = False

        inner = E2BSandboxSession.from_state(state, sandbox=sandbox)
        inner._set_start_state_preserved(reconnected, system=reconnected)
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


def _e2b_network_config(exposed_ports: tuple[int, ...]) -> dict[str, object] | None:
    if not exposed_ports:
        return None
    return {"allow_public_traffic": True}


def _e2b_endpoint_from_host(host: str) -> ExposedPortEndpoint | None:
    if not host:
        return None

    split = urlsplit(f"//{host}")
    hostname = split.hostname
    if hostname is None:
        return None

    explicit_port = split.port
    if explicit_port is not None:
        return ExposedPortEndpoint(host=hostname, port=explicit_port, tls=False)

    return ExposedPortEndpoint(host=hostname, port=443, tls=True)

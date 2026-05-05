"""
Modal sandbox (https://modal.com) implementation.

Run `python -m modal setup` to configure Modal locally.

This module provides a Modal-backed sandbox client/session implementation backed by
`modal.Sandbox`.

Note: The `modal` dependency is intended to be optional (installed via an extra),
so package-level exports should guard imports of this module. Within this module,
we import Modal normally so IDEs can resolve and navigate Modal types.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import logging
import math
import os
import shlex
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

import modal
from modal.config import config as modal_config
from modal.container_process import ContainerProcess

from ....sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from ....sandbox.entries import Mount
from ....sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    MountConfigError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceStopError,
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
from .mounts import ModalCloudBucketMountStrategy

_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_IMAGE_TAG = DEFAULT_PYTHON_SANDBOX_IMAGE
_DEFAULT_IMAGE_BUILDER_VERSION = "2025.06"
_DEFAULT_SNAPSHOT_FILESYSTEM_TIMEOUT_S = 60.0
_MODAL_STDIN_CHUNK_SIZE = 8 * 1024 * 1024
_PTY_POLL_INTERVAL_S = 0.05

WorkspacePersistenceMode = Literal["tar", "snapshot_filesystem", "snapshot_directory"]

_WORKSPACE_PERSISTENCE_TAR: WorkspacePersistenceMode = "tar"
_WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM: WorkspacePersistenceMode = "snapshot_filesystem"
_WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY: WorkspacePersistenceMode = "snapshot_directory"

# Magic prefixes for snapshot payloads that cannot be represented as tar bytes.
_MODAL_SANDBOX_FS_SNAPSHOT_MAGIC = b"MODAL_SANDBOX_FS_SNAPSHOT_V1\n"
_MODAL_SANDBOX_DIR_SNAPSHOT_MAGIC = b"MODAL_SANDBOX_DIR_SNAPSHOT_V1\n"

logger = logging.getLogger(__name__)
R = TypeVar("R")


@asynccontextmanager
async def _override_modal_image_builder_version(
    image_builder_version: str | None,
) -> AsyncIterator[None]:
    """Apply a process-local Modal image builder version for the duration of a build."""

    if image_builder_version is None:
        yield
        return

    previous_value = os.environ.get("MODAL_IMAGE_BUILDER_VERSION")
    modal_config.override_locally("image_builder_version", image_builder_version)
    try:
        yield
    finally:
        if previous_value is None:
            os.environ.pop("MODAL_IMAGE_BUILDER_VERSION", None)
        else:
            os.environ["MODAL_IMAGE_BUILDER_VERSION"] = previous_value


def _maybe_set_sandbox_cmd(
    image: modal.Image,
    *,
    use_sleep_cmd: bool,
) -> modal.Image:
    if not use_sleep_cmd:
        return image
    return image.cmd(["sleep", "infinity"])


async def _write_process_stdin(proc: ContainerProcess[bytes], data: bytes | bytearray) -> None:
    """
    Stream stdin to Modal in bounded chunks so command-router backed writers do not overflow.
    """

    view = memoryview(data)
    for start in range(0, len(view), _MODAL_STDIN_CHUNK_SIZE):
        proc.stdin.write(view[start : start + _MODAL_STDIN_CHUNK_SIZE])
        await proc.stdin.drain.aio()
    proc.stdin.write_eof()
    await proc.stdin.drain.aio()


class ModalSandboxClientOptions(BaseSandboxClientOptions):
    type: Literal["modal"] = "modal"
    app_name: str
    sandbox_create_timeout_s: float | None = None
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    snapshot_filesystem_timeout_s: float | None = None
    snapshot_filesystem_restore_timeout_s: float | None = None
    exposed_ports: tuple[int, ...] = ()
    gpu: str | None = None  # Modal GPU type, e.g. "A100" or "H100:8"
    timeout: int = 300  # Lifetime of a sandbox from creation in seconds, defaults to 5 minutes
    use_sleep_cmd: bool = True
    image_builder_version: str | None = _DEFAULT_IMAGE_BUILDER_VERSION
    idle_timeout: int | None = None

    def __init__(
        self,
        app_name: str,
        sandbox_create_timeout_s: float | None = None,
        workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR,
        snapshot_filesystem_timeout_s: float | None = None,
        snapshot_filesystem_restore_timeout_s: float | None = None,
        exposed_ports: tuple[int, ...] = (),
        gpu: str | None = None,
        timeout: int = 300,  # 5 minutes
        use_sleep_cmd: bool = True,
        image_builder_version: str | None = _DEFAULT_IMAGE_BUILDER_VERSION,
        idle_timeout: int | None = None,
        *,
        type: Literal["modal"] = "modal",
    ) -> None:
        super().__init__(
            type=type,
            app_name=app_name,
            sandbox_create_timeout_s=sandbox_create_timeout_s,
            workspace_persistence=workspace_persistence,
            snapshot_filesystem_timeout_s=snapshot_filesystem_timeout_s,
            snapshot_filesystem_restore_timeout_s=snapshot_filesystem_restore_timeout_s,
            exposed_ports=exposed_ports,
            gpu=gpu,
            timeout=timeout,
            use_sleep_cmd=use_sleep_cmd,
            image_builder_version=image_builder_version,
            idle_timeout=idle_timeout,
        )


def _encode_modal_snapshot_ref(
    *,
    snapshot_id: str,
    workspace_persistence: WorkspacePersistenceMode,
) -> bytes:
    # Small JSON envelope so we can round-trip a non-tar snapshot reference
    # through Snapshot.persist().
    body = json.dumps(
        {"snapshot_id": snapshot_id, "workspace_persistence": workspace_persistence},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY:
        return _MODAL_SANDBOX_DIR_SNAPSHOT_MAGIC + body
    return _MODAL_SANDBOX_FS_SNAPSHOT_MAGIC + body


def _encode_snapshot_filesystem_ref(*, snapshot_id: str) -> bytes:
    return _encode_modal_snapshot_ref(
        snapshot_id=snapshot_id,
        workspace_persistence=_WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM,
    )


def _encode_snapshot_directory_ref(*, snapshot_id: str) -> bytes:
    return _encode_modal_snapshot_ref(
        snapshot_id=snapshot_id,
        workspace_persistence=_WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY,
    )


def _decode_modal_snapshot_ref(raw: bytes) -> tuple[WorkspacePersistenceMode, str] | None:
    if raw.startswith(_MODAL_SANDBOX_DIR_SNAPSHOT_MAGIC):
        prefix = _MODAL_SANDBOX_DIR_SNAPSHOT_MAGIC
        default_persistence = _WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY
    elif raw.startswith(_MODAL_SANDBOX_FS_SNAPSHOT_MAGIC):
        prefix = _MODAL_SANDBOX_FS_SNAPSHOT_MAGIC
        default_persistence = _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM
    else:
        return None
    body = raw[len(prefix) :]
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    snapshot_id = obj.get("snapshot_id")
    workspace_persistence = obj.get("workspace_persistence", default_persistence)
    if workspace_persistence not in (
        _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM,
        _WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY,
    ):
        return None
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return None
    return cast(WorkspacePersistenceMode, workspace_persistence), snapshot_id


@dataclass(frozen=True)
class ModalImageSelector:
    """
    A single "image selector" type to avoid juggling image/image_id/image_tag separately.
    """

    kind: Literal["image", "id", "tag"]
    value: modal.Image | str

    @classmethod
    def from_image(cls, image: modal.Image) -> ModalImageSelector:
        return cls(kind="image", value=image)

    @classmethod
    def from_id(cls, image_id: str) -> ModalImageSelector:
        return cls(kind="id", value=image_id)

    @classmethod
    def from_tag(cls, image_tag: str) -> ModalImageSelector:
        return cls(kind="tag", value=image_tag)


@dataclass(frozen=True)
class ModalSandboxSelector:
    """
    A single "sandbox selector" type to avoid juggling sandbox/sandbox_id separately.
    """

    kind: Literal["sandbox", "id"]
    value: modal.Sandbox | str

    @classmethod
    def from_sandbox(cls, sandbox: modal.Sandbox) -> ModalSandboxSelector:
        return cls(kind="sandbox", value=sandbox)

    @classmethod
    def from_id(cls, sandbox_id: str) -> ModalSandboxSelector:
        return cls(kind="id", value=sandbox_id)


class ModalSandboxSessionState(SandboxSessionState):
    """
    Serializable state for a Modal-backed session.

    We store only values that can be safely persisted and later used by `resume()`.
    """

    type: Literal["modal"] = "modal"
    app_name: str
    # Optional Modal image object id (enables reconstructing a custom image via Image.from_id()).
    image_id: str | None = None
    # Registry image tag (e.g. "debian:bookworm" or "ghcr.io/org/img:tag").
    # Used when `image_id` isn't available and no in-memory image override was provided.
    image_tag: str | None = None
    # Timeout for creating a sandbox (Modal calls are synchronous from the user's perspective
    # and can block; we wrap them in a thread with asyncio timeout).
    sandbox_create_timeout_s: float = _DEFAULT_TIMEOUT_S
    sandbox_id: str | None = None
    # Workspace persistence mode:
    # - "tar": create a tar stream in the sandbox via `tar cf - ...` and pull bytes back via stdout.
    # - "snapshot_filesystem": use Modal's `Sandbox.snapshot_filesystem()`
    #   (if available) and persist a snapshot reference.
    # - "snapshot_directory": use Modal's `Sandbox.snapshot_directory()` on the workspace root
    #   and reattach it during resume.
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    # Async timeouts for snapshot_filesystem-based persistence and restore.
    snapshot_filesystem_timeout_s: float = _DEFAULT_SNAPSHOT_FILESYSTEM_TIMEOUT_S
    snapshot_filesystem_restore_timeout_s: float = _DEFAULT_SNAPSHOT_FILESYSTEM_TIMEOUT_S
    gpu: str | None = None  # Modal GPU type, e.g. "A100" or "H100:8"
    # Maximum lifetime of the sandbox in seconds
    timeout: int = 300  # 5 minutes
    use_sleep_cmd: bool = True
    image_builder_version: str | None = _DEFAULT_IMAGE_BUILDER_VERSION
    idle_timeout: int | None = None


@dataclass
class _ModalPtyProcessEntry:
    process: ContainerProcess[bytes]
    tty: bool
    last_used: float = field(default_factory=time.monotonic)
    stdout_iter: AsyncIterator[object] | None = None
    stderr_iter: AsyncIterator[object] | None = None
    stdout_read_task: asyncio.Task[object] | None = None
    stderr_read_task: asyncio.Task[object] | None = None


class ModalSandboxSession(BaseSandboxSession):
    """
    SandboxSession implementation backed by a Modal Sandbox.
    """

    state: ModalSandboxSessionState

    _sandbox: modal.Sandbox | None
    _image: modal.Image | None
    _running: bool
    _pty_lock: asyncio.Lock
    _pty_processes: dict[int, _ModalPtyProcessEntry]
    _reserved_pty_process_ids: set[int]
    _modal_snapshot_ephemeral_backup: bytes | None
    _modal_snapshot_ephemeral_backup_path: Path | None

    def __init__(
        self,
        *,
        state: ModalSandboxSessionState,
        # Optional in-memory handles. These are not guaranteed to be resumable; state holds ids.
        image: modal.Image | None = None,
        sandbox: modal.Sandbox | None = None,
    ) -> None:
        self.state = state
        self._image = None
        if image is not None:
            self._image = _maybe_set_sandbox_cmd(
                image,
                use_sleep_cmd=self.state.use_sleep_cmd,
            )
        self._sandbox = sandbox
        if self._image is not None:
            self.state.image_id = getattr(self._image, "object_id", self.state.image_id)
        if sandbox is not None:
            self.state.sandbox_id = getattr(sandbox, "object_id", self.state.sandbox_id)
        self._running = False
        self._pty_lock = asyncio.Lock()
        self._pty_processes = {}
        self._reserved_pty_process_ids = set()
        self._modal_snapshot_ephemeral_backup = None
        self._modal_snapshot_ephemeral_backup_path = None

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.sandbox_id

    @classmethod
    def from_state(
        cls,
        state: ModalSandboxSessionState,
        *,
        image: modal.Image | None = None,
        sandbox: modal.Sandbox | None = None,
    ) -> ModalSandboxSession:
        return cls(state=state, image=image, sandbox=sandbox)

    async def _call_modal(
        self,
        fn: Callable[..., R],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> R:
        """
        Prefer Modal's async interface (`fn.aio(...)`) when available.

        Falls back to running the blocking call in a thread to preserve compatibility
        with SDK surfaces that do not expose `.aio`.
        """

        aio_fn = getattr(fn, "aio", None)
        if callable(aio_fn):
            coro = cast(Awaitable[R], aio_fn(*args, **kwargs))
        else:
            loop = asyncio.get_running_loop()
            bound = functools.partial(fn, *args, **kwargs)
            coro = loop.run_in_executor(None, bound)
        if call_timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=call_timeout)

    async def _ensure_backend_started(self) -> None:
        await self._ensure_sandbox()

    async def _prepare_backend_workspace(self) -> None:
        # Ensure workspace root exists before the base workspace flow needs it.
        root = self._workspace_path_policy().sandbox_root().as_posix()
        await self.exec("mkdir", "-p", "--", root, shell=False)

    async def _after_start(self) -> None:
        self._running = True

    async def _after_start_failed(self) -> None:
        self._running = False

    def _wrap_start_error(self, error: Exception) -> Exception:
        if isinstance(error, WorkspaceStartError):
            return error
        return WorkspaceStartError(path=self._workspace_root_path(), cause=error)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        await self._ensure_sandbox()
        assert self._sandbox is not None

        try:
            tunnels = await asyncio.wait_for(self._sandbox.tunnels.aio(), timeout=10.0)
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "modal", "detail": "tunnels_lookup_failed"},
                cause=e,
            ) from e

        if not isinstance(tunnels, dict):
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "modal", "detail": "invalid_tunnels_response"},
            )

        tunnel = tunnels.get(port)
        host = getattr(tunnel, "host", None)
        host_port = getattr(tunnel, "port", None)
        if not isinstance(host, str) or not host or not isinstance(host_port, int):
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "modal", "detail": "port_not_exposed"},
            )
        return ExposedPortEndpoint(host=host, port=host_port, tls=True)

    def _wrap_stop_error(self, error: Exception) -> Exception:
        if isinstance(error, WorkspaceStopError):
            return error
        return WorkspaceStopError(path=self._workspace_root_path(), cause=error)

    async def _shutdown_backend(self) -> None:
        try:
            sandbox = self._sandbox
            if sandbox is not None:
                await self._call_modal(
                    sandbox.terminate,
                    call_timeout=_DEFAULT_TIMEOUT_S,
                )
            elif self.state.sandbox_id:
                sid = self.state.sandbox_id
                assert sid is not None
                sb = await self._call_modal(
                    modal.Sandbox.from_id,
                    sid,
                    call_timeout=_DEFAULT_TIMEOUT_S,
                )
                await self._call_modal(
                    sb.terminate,
                    call_timeout=_DEFAULT_TIMEOUT_S,
                )
        except Exception:
            pass
        finally:
            self.state.sandbox_id = None
            self.state.workspace_root_ready = False
            self._sandbox = None
            self._running = False

    async def _ensure_sandbox(self) -> bool:
        if self._sandbox is not None:
            return False

        # If resuming, try to rehydrate the sandbox handle from the persisted id.
        sid = self.state.sandbox_id
        if sid:
            try:
                sb = await self._call_modal(
                    modal.Sandbox.from_id,
                    sid,
                    call_timeout=self.state.sandbox_create_timeout_s,
                )

                # `poll()` returns an exit code when the sandbox is terminated, else None.
                poll_result = await self._call_modal(sb.poll, call_timeout=_DEFAULT_TIMEOUT_S)
                is_running = poll_result is None
                if is_running:
                    self._sandbox = sb
                    self._running = True
                    return True
            except Exception:
                pass

            # Resumed sandbox handle is dead or invalid; clear and create a fresh one.
            self._sandbox = None
            self.state.sandbox_id = None

        app = await self._call_modal(
            modal.App.lookup,
            self.state.app_name,
            create_if_missing=True,
            call_timeout=10.0,
        )
        if not self._image:
            image_id = self.state.image_id
            if image_id:
                self._image = modal.Image.from_id(image_id)
            else:
                tag = self.state.image_tag
                if not isinstance(tag, str) or not tag:
                    tag = _DEFAULT_IMAGE_TAG
                    # Record the default for better debuggability/resume.
                    self.state.image_tag = tag
                self._image = await self._call_modal(
                    modal.Image.from_registry,
                    tag,
                    call_timeout=_DEFAULT_TIMEOUT_S,
                )
            self._image = _maybe_set_sandbox_cmd(
                self._image,
                use_sleep_cmd=self.state.use_sleep_cmd,
            )

        manifest_envs = cast(dict[str, str | None], await self.state.manifest.environment.resolve())
        volumes = self._modal_cloud_bucket_mounts_for_manifest()
        create_coro = modal.Sandbox.create.aio(
            app=app,
            image=self._image,
            workdir=self.state.manifest.root,
            env=manifest_envs,
            encrypted_ports=self.state.exposed_ports,
            volumes=volumes,
            gpu=self.state.gpu,
            timeout=self.state.timeout,
            idle_timeout=self.state.idle_timeout,
        )
        async with _override_modal_image_builder_version(self.state.image_builder_version):
            if self.state.sandbox_create_timeout_s is None:
                self._sandbox = await create_coro
            else:
                self._sandbox = await asyncio.wait_for(
                    create_coro, timeout=self.state.sandbox_create_timeout_s
                )

        # Persist sandbox id for future resume.
        assert self._sandbox is not None
        self.state.sandbox_id = self._sandbox.object_id
        self.state.workspace_root_ready = False

        assert self._image is not None
        self.state.image_id = self._image.object_id
        return False

    async def snapshot_filesystem(self) -> str:
        """Snapshot the current sandbox filesystem and return the resulting Modal image ID.

        The returned ID can be passed as ``image_id`` when creating a new sandbox to boot
        from this filesystem state.  The image ID is also stored in ``state.image_id`` for future
        resume.
        """
        await self._ensure_sandbox()
        assert self._sandbox is not None
        snap_coro = self._sandbox.snapshot_filesystem.aio()
        if self.state.snapshot_filesystem_timeout_s is None:
            snap = await snap_coro
        else:
            snap = await asyncio.wait_for(
                snap_coro, timeout=self.state.snapshot_filesystem_timeout_s
            )
        image_id: str | None
        if isinstance(snap, str):
            image_id = snap
        else:
            image_id = getattr(snap, "object_id", None) or getattr(snap, "id", None)
        if not isinstance(image_id, str) or not image_id:
            raise RuntimeError(
                f"snapshot_filesystem returned unexpected type: {type(snap).__name__}"
            )
        self.state.image_id = image_id
        self._image = modal.Image.from_id(image_id)
        return image_id

    async def _exec_internal(
        self, *command: str | Path, timeout: float | None = None
    ) -> ExecResult:
        await self._ensure_sandbox()
        assert self._sandbox is not None

        modal_timeout: int | None = None
        if timeout is not None:
            # Modal's Sandbox.exec timeout is integer seconds; use ceil so the command
            # is guaranteed to be terminated server-side at or before our timeout window
            # (modulo 1s granularity).
            modal_timeout = int(max(_DEFAULT_TIMEOUT_S, math.ceil(timeout)))

        async def _run_async() -> ExecResult:
            assert self._sandbox is not None
            argv: tuple[str, ...] = tuple(str(part) for part in command)
            proc = await self._sandbox.exec.aio(*argv, text=False, timeout=modal_timeout)
            # Drain full output; Modal buffers process output server-side.
            stdout = await proc.stdout.read.aio()
            stderr = await proc.stderr.read.aio()
            exit_code = await proc.wait.aio()
            return ExecResult(stdout=stdout or b"", stderr=stderr or b"", exit_code=exit_code or 0)

        try:
            run_coro = _run_async()
            if timeout is None:
                return await run_coro
            return await asyncio.wait_for(run_coro, timeout=timeout)
        except asyncio.TimeoutError as e:
            sandbox = self._sandbox
            if sandbox is not None:
                try:
                    await self._call_modal(sandbox.terminate, call_timeout=_DEFAULT_TIMEOUT_S)
                except Exception:
                    pass
            self._sandbox = None
            self.state.sandbox_id = None
            self._running = False
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except ExecTimeoutError:
            raise
        except Exception as e:
            raise ExecTransportError(command=command, cause=e) from e

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
        await self._ensure_sandbox()
        assert self._sandbox is not None

        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=user)
        argv: tuple[str, ...] = tuple(str(part) for part in sanitized_command)
        modal_timeout: int | None = None
        if timeout is not None:
            modal_timeout = int(max(_DEFAULT_TIMEOUT_S, math.ceil(timeout)))

        entry: _ModalPtyProcessEntry | None = None
        registered = False
        pruned_entry: _ModalPtyProcessEntry | None = None
        process_id = 0
        process_count = 0
        try:
            process = cast(
                Any,
                await self._call_modal(
                    self._sandbox.exec,
                    *argv,
                    text=False,
                    timeout=modal_timeout,
                    pty=tty,
                ),
            )
            entry = _ModalPtyProcessEntry(process=process, tty=tty)

            async with self._pty_lock:
                process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
                self._reserved_pty_process_ids.add(process_id)
                pruned_entry = await self._prune_pty_processes_if_needed()
                self._pty_processes[process_id] = entry
                registered = True
                process_count = len(self._pty_processes)
        except asyncio.TimeoutError as e:
            if entry is not None and not registered:
                await self._terminate_pty_entry(entry)
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except asyncio.CancelledError:
            if entry is not None and not registered:
                await self._terminate_pty_entry(entry)
            raise
        except Exception as e:
            if entry is not None and not registered:
                await self._terminate_pty_entry(entry)
            raise ExecTransportError(command=command, cause=e) from e

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
            await self._write_pty_stdin(entry.process, chars.encode("utf-8"))
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

    async def _write_pty_stdin(self, process: ContainerProcess[bytes], payload: bytes) -> None:
        stdin = process.stdin
        write = getattr(stdin, "write", None)
        if not callable(write):
            raise RuntimeError("stdin is not writable for this process")
        await self._call_modal(write, payload, call_timeout=5.0)

        drain = getattr(stdin, "drain", None)
        if callable(drain):
            await self._call_modal(drain, call_timeout=5.0)

    async def _collect_pty_output(
        self,
        *,
        entry: _ModalPtyProcessEntry,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[bytes, int | None]:
        deadline = time.monotonic() + (yield_time_ms / 1000)
        chunks = bytearray()

        while True:
            stdout_chunk = await self._read_modal_stream(entry=entry, stream_name="stdout")
            stderr_chunk = await self._read_modal_stream(entry=entry, stream_name="stderr")
            if stdout_chunk:
                chunks.extend(stdout_chunk)
            if stderr_chunk:
                chunks.extend(stderr_chunk)

            if time.monotonic() >= deadline:
                break

            exit_code = await self._peek_exit_code(entry.process)
            if exit_code is not None:
                stdout_chunks = await self._drain_modal_stream(entry=entry, stream_name="stdout")
                stderr_chunks = await self._drain_modal_stream(entry=entry, stream_name="stderr")
                chunks.extend(stdout_chunks)
                chunks.extend(stderr_chunks)
                break

            if not stdout_chunk and not stderr_chunk:
                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0:
                    break
                await asyncio.sleep(min(_PTY_POLL_INTERVAL_S, remaining_s))

        text = chunks.decode("utf-8", errors="replace")
        truncated_text, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
        return truncated_text.encode("utf-8", errors="replace"), original_token_count

    async def _drain_modal_stream(
        self,
        *,
        entry: _ModalPtyProcessEntry,
        stream_name: Literal["stdout", "stderr"],
    ) -> bytes:
        chunks = bytearray()
        while True:
            chunk = await self._read_modal_stream(
                entry=entry,
                stream_name=stream_name,
                await_pending=True,
            )
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    async def _read_modal_stream(
        self,
        *,
        entry: _ModalPtyProcessEntry,
        stream_name: Literal["stdout", "stderr"],
        await_pending: bool = False,
    ) -> bytes:
        stream = entry.process.stdout if stream_name == "stdout" else entry.process.stderr
        if stream is None:
            return b""

        iter_attr = "stdout_iter" if stream_name == "stdout" else "stderr_iter"
        task_attr = "stdout_read_task" if stream_name == "stdout" else "stderr_read_task"
        stream_iter = getattr(entry, iter_attr)
        if stream_iter is None:
            aiter_method = getattr(stream, "__aiter__", None)
            if callable(aiter_method):
                try:
                    stream_iter = aiter_method()
                except Exception:
                    stream_iter = None
                else:
                    setattr(entry, iter_attr, stream_iter)

        task = getattr(entry, task_attr)
        if task is None and stream_iter is not None:
            task = asyncio.create_task(stream_iter.__anext__())
            setattr(entry, task_attr, task)

        if task is not None:
            wait_timeout = 0.2 if await_pending else 0
            done, _pending = await asyncio.wait({task}, timeout=wait_timeout)
            if not done:
                return b""

            setattr(entry, task_attr, None)
            try:
                value = task.result()
            except StopAsyncIteration:
                setattr(entry, iter_attr, None)
                return b""
            except Exception:
                setattr(entry, iter_attr, None)
                return b""

            return self._coerce_modal_stream_chunk(value)

        read = getattr(stream, "read", None)
        if not callable(read):
            return b""

        try:
            value = await self._call_modal(read, 16_384, call_timeout=0.2)
        except TypeError:
            return b""
        except Exception:
            return b""

        return self._coerce_modal_stream_chunk(value)

    def _coerce_modal_stream_chunk(self, value: object) -> bytes:
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8", errors="replace")
        return str(value).encode("utf-8", errors="replace")

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _ModalPtyProcessEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        exit_code = await self._peek_exit_code(entry.process)
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

    async def _prune_pty_processes_if_needed(self) -> _ModalPtyProcessEntry | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None

        meta: list[tuple[int, float, bool]] = []
        for process_id, entry in self._pty_processes.items():
            exit_code = await self._peek_exit_code(entry.process)
            meta.append((process_id, entry.last_used, exit_code is not None))
        process_id_to_prune = process_id_to_prune_from_meta(meta)
        if process_id_to_prune is None:
            return None

        self._reserved_pty_process_ids.discard(process_id_to_prune)
        return self._pty_processes.pop(process_id_to_prune, None)

    async def _peek_exit_code(self, process: ContainerProcess[bytes]) -> int | None:
        try:
            value = await self._call_modal(process.poll, call_timeout=0.2)
        except Exception:
            return None

        if value is None:
            return None
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _terminate_pty_entry(self, entry: _ModalPtyProcessEntry) -> None:
        process = entry.process
        for task in (entry.stdout_read_task, entry.stderr_read_task):
            if task is not None and not task.done():
                task.cancel()

        try:
            terminated = False
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                await self._call_modal(terminate, call_timeout=5.0)
                terminated = True

            if not terminated:
                stdin = getattr(process, "stdin", None)
            else:
                stdin = None
            if stdin is not None:
                write_eof = getattr(stdin, "write_eof", None)
                if callable(write_eof):
                    await self._call_modal(write_eof, call_timeout=5.0)
        except Exception:
            pass
        finally:
            await asyncio.gather(
                *(
                    task
                    for task in (entry.stdout_read_task, entry.stderr_read_task)
                    if task is not None
                ),
                return_exceptions=True,
            )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            await self._check_read_with_exec(path, user=user)

        # Read by `cat` so the payload is returned as bytes.
        workspace_path = await self._validate_path_access(path)
        cmd = ["sh", "-lc", f"cat -- {shlex.quote(sandbox_path_str(workspace_path))}"]
        try:
            out = await self.exec(*cmd, shell=False)
        except ExecTimeoutError as e:
            raise WorkspaceArchiveReadError(path=workspace_path, cause=e) from e
        except ExecTransportError as e:
            raise WorkspaceArchiveReadError(path=workspace_path, cause=e) from e

        if not out.ok():
            raise WorkspaceReadNotFoundError(
                path=path, context={"stderr": out.stderr.decode("utf-8", "replace")}
            )

        return io.BytesIO(out.stdout)

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

        await self._ensure_sandbox()
        assert self._sandbox is not None

        workspace_path = await self._validate_path_access(path, for_write=True)

        async def _run_write() -> None:
            assert self._sandbox is not None
            # Ensure parent directory exists.
            parent = sandbox_path_str(workspace_path.parent)
            mkdir_proc = await self._sandbox.exec.aio("mkdir", "-p", "--", parent, text=False)
            await mkdir_proc.wait.aio()

            # Stream bytes into `cat > file` to avoid quoting/binary issues.
            cmd = ["sh", "-lc", f"cat > {shlex.quote(sandbox_path_str(workspace_path))}"]
            proc = await self._sandbox.exec.aio(*cmd, text=False)
            await _write_process_stdin(proc, payload)
            exit_code = await proc.wait.aio()
            if exit_code != 0:
                stderr = await proc.stderr.read.aio()
                raise WorkspaceArchiveWriteError(
                    path=workspace_path,
                    context={
                        "reason": "write_nonzero_exit",
                        "exit_code": exit_code,
                        "stderr": stderr.decode("utf-8", "replace"),
                    },
                )

        try:
            await asyncio.wait_for(_run_write(), timeout=30.0)
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        if not self._running or self._sandbox is None:
            return False

        try:
            assert self._sandbox is not None
            poll_result = await asyncio.wait_for(self._sandbox.poll.aio(), timeout=5.0)
            return poll_result is None
        except Exception:
            return False

    async def persist_workspace(self) -> io.IOBase:
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM:
            return await self._persist_workspace_via_snapshot_filesystem()
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY:
            return await self._persist_workspace_via_snapshot_directory()
        return await self._persist_workspace_via_tar()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM:
            return await self._hydrate_workspace_via_snapshot_filesystem(data)
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY:
            return await self._hydrate_workspace_via_snapshot_directory(data)
        return await self._hydrate_workspace_via_tar(data)

    async def _persist_workspace_via_snapshot_filesystem(self) -> io.IOBase:
        """
        Persist the workspace using Modal's snapshot_filesystem API when available.

        Modal's snapshot_filesystem is expected to return a snapshot reference
        (a Modal Image handle). We serialize a small reference envelope that
        `_hydrate_workspace_via_snapshot_filesystem` can interpret.
        """

        await self._ensure_sandbox()
        assert self._sandbox is not None
        if not hasattr(self._sandbox, "snapshot_filesystem"):
            return await self._persist_workspace_via_tar()
        if self._native_snapshot_requires_tar_fallback():
            return await self._persist_workspace_via_tar()
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        plain_skip = self._modal_snapshot_plain_skip_relpaths(root)
        skip_abs = [root / rel for rel in sorted(plain_skip, key=lambda p: p.as_posix())]
        self._modal_snapshot_ephemeral_backup = None
        self._modal_snapshot_ephemeral_backup_path = None

        async def restore_ephemeral_paths() -> WorkspaceArchiveReadError | None:
            backup = self._modal_snapshot_ephemeral_backup
            if not backup:
                return None

            try:
                assert self._sandbox is not None
                proc = await self._sandbox.exec.aio(
                    "tar", "xf", "-", "-C", root.as_posix(), text=False
                )
                await _write_process_stdin(proc, bytes(backup))
                exit_code = await proc.wait.aio()
                if exit_code != 0:
                    stderr = await proc.stderr.read.aio()
                    return WorkspaceArchiveReadError(
                        path=error_root,
                        context={
                            "reason": "snapshot_filesystem_ephemeral_restore_failed",
                            "exit_code": exit_code,
                            "stderr": stderr.decode("utf-8", "replace"),
                        },
                    )
            except Exception as exc:
                if isinstance(exc, WorkspaceArchiveReadError):
                    return exc
                return WorkspaceArchiveReadError(
                    path=error_root,
                    context={"reason": "snapshot_filesystem_ephemeral_restore_failed"},
                    cause=exc,
                )
            return None

        if skip_abs:
            rel_args = " ".join(shlex.quote(p.relative_to(root).as_posix()) for p in skip_abs)
            cmd = (
                f"cd -- {shlex.quote(root.as_posix())} && "
                f"(tar cf - -- {rel_args} 2>/dev/null || true)"
            )
            out = await self.exec("sh", "-lc", cmd, shell=False)
            self._modal_snapshot_ephemeral_backup = out.stdout or b""

            rm_cmd = ["rm", "-rf", "--", *[p.as_posix() for p in skip_abs]]
            rm_out = await self.exec(*rm_cmd, shell=False)
            if not rm_out.ok():
                cleanup_restore_error = await restore_ephemeral_paths()
                if cleanup_restore_error is not None:
                    logger.warning(
                        "Failed to restore Modal ephemeral paths after cleanup failure: %s",
                        cleanup_restore_error,
                    )
                raise WorkspaceArchiveReadError(
                    path=error_root,
                    context={
                        "reason": "snapshot_filesystem_ephemeral_remove_failed",
                        "exit_code": rm_out.exit_code,
                        "stderr": rm_out.stderr.decode("utf-8", "replace"),
                    },
                )

        try:
            snapshot_sandbox = await self._refresh_sandbox_handle_for_snapshot()
            snap_coro = snapshot_sandbox.snapshot_filesystem.aio()
            if self.state.snapshot_filesystem_timeout_s is None:
                snap = await snap_coro
            else:
                snap = await asyncio.wait_for(
                    snap_coro, timeout=self.state.snapshot_filesystem_timeout_s
                )
        except Exception as e:
            restore_error = await restore_ephemeral_paths()
            if restore_error is not None:
                logger.warning(
                    "Failed to restore Modal ephemeral paths after snapshot failure: %s",
                    restore_error,
                )
            raise WorkspaceArchiveReadError(
                path=error_root, context={"reason": "snapshot_filesystem_failed"}, cause=e
            ) from e

        snapshot_id, snapshot_error = self._extract_modal_snapshot_id(
            snap=snap, root=root, snapshot_kind="snapshot_filesystem"
        )

        restore_error = await restore_ephemeral_paths()
        if restore_error is not None:
            raise restore_error

        if snapshot_error is not None:
            raise snapshot_error

        assert snapshot_id is not None
        return io.BytesIO(_encode_snapshot_filesystem_ref(snapshot_id=snapshot_id))

    async def _persist_workspace_via_snapshot_directory(self) -> io.IOBase:
        """
        Persist the workspace using Modal's snapshot_directory API when available.
        """

        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        await self._ensure_sandbox()
        assert self._sandbox is not None
        if not hasattr(self._sandbox, "snapshot_directory"):
            return await self._persist_workspace_via_tar()
        if self._native_snapshot_requires_tar_fallback():
            return await self._persist_workspace_via_tar()
        plain_skip = self._modal_snapshot_plain_skip_relpaths(root)
        skip_abs = [root / rel for rel in sorted(plain_skip, key=lambda p: p.as_posix())]
        self._modal_snapshot_ephemeral_backup = None
        self._modal_snapshot_ephemeral_backup_path = None
        detached_mounts: list[tuple[Mount, Path]] = []

        async def restore_ephemeral_paths() -> WorkspaceArchiveReadError | None:
            backup_path = self._modal_snapshot_ephemeral_backup_path
            if backup_path is None:
                return None

            restore_cmd = (
                f"if [ ! -f {shlex.quote(backup_path.as_posix())} ]; then "
                f"echo missing ephemeral backup archive >&2; "
                f"exit 1; "
                f"fi; "
                f"tar xf {shlex.quote(backup_path.as_posix())} -C "
                f"{shlex.quote(root.as_posix())} && "
                f"rm -f -- {shlex.quote(backup_path.as_posix())}"
            )
            out = await self.exec("sh", "-lc", restore_cmd, shell=False)
            if not out.ok():
                return WorkspaceArchiveReadError(
                    path=error_root,
                    context={
                        "reason": "snapshot_directory_ephemeral_restore_failed",
                        "exit_code": out.exit_code,
                        "stderr": out.stderr.decode("utf-8", "replace"),
                    },
                )
            return None

        async def restore_detached_mounts() -> WorkspaceArchiveReadError | None:
            remount_error: WorkspaceArchiveReadError | None = None
            for mount_entry, mount_path in reversed(detached_mounts):
                try:
                    await mount_entry.mount_strategy.restore_after_snapshot(
                        mount_entry,
                        self,
                        mount_path,
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
            return remount_error

        snapshot_error: WorkspaceArchiveReadError | None = None
        snapshot_id: str | None = None
        try:
            if skip_abs:
                backup_path = posix_path_as_path(
                    coerce_posix_path(
                        "/tmp/openai-agents/session-state"
                        f"/{self.state.session_id.hex}/modal-snapshot-directory-ephemeral.tar"
                    )
                )
                rel_args = " ".join(shlex.quote(p.relative_to(root).as_posix()) for p in skip_abs)
                backup_cmd = (
                    f"mkdir -p -- {shlex.quote(backup_path.parent.as_posix())} && "
                    f"cd -- {shlex.quote(root.as_posix())} && "
                    "{ "
                    f"for rel in {rel_args}; do "
                    'if [ -e "$rel" ]; then printf \'%s\\n\' "$rel"; fi; '
                    "done; "
                    "} | "
                    f"tar cf {shlex.quote(backup_path.as_posix())} -T - 2>/dev/null && "
                    f"test -f {shlex.quote(backup_path.as_posix())}"
                )
                backup_out = await self.exec("sh", "-lc", backup_cmd, shell=False)
                if not backup_out.ok():
                    raise WorkspaceArchiveReadError(
                        path=error_root,
                        context={
                            "reason": "snapshot_directory_ephemeral_backup_failed",
                            "exit_code": backup_out.exit_code,
                            "stderr": backup_out.stderr.decode("utf-8", "replace"),
                        },
                    )
                self._modal_snapshot_ephemeral_backup_path = backup_path

                rm_cmd = ["rm", "-rf", "--", *[sandbox_path_str(p) for p in skip_abs]]
                rm_out = await self.exec(*rm_cmd, shell=False)
                if not rm_out.ok():
                    raise WorkspaceArchiveReadError(
                        path=error_root,
                        context={
                            "reason": "snapshot_directory_ephemeral_remove_failed",
                            "exit_code": rm_out.exit_code,
                            "stderr": rm_out.stderr.decode("utf-8", "replace"),
                        },
                    )

            for mount_entry, mount_path in self._snapshot_directory_mount_targets_to_restore(root):
                await mount_entry.mount_strategy.teardown_for_snapshot(
                    mount_entry,
                    self,
                    mount_path,
                )
                detached_mounts.append((mount_entry, mount_path))

            snapshot_sandbox = await self._refresh_sandbox_handle_for_snapshot()
            snap_coro = snapshot_sandbox.snapshot_directory.aio(root.as_posix())
            if self.state.snapshot_filesystem_timeout_s is None:
                snap = await snap_coro
            else:
                snap = await asyncio.wait_for(
                    snap_coro, timeout=self.state.snapshot_filesystem_timeout_s
                )
            snapshot_id, snapshot_error = self._extract_modal_snapshot_id(
                snap=snap, root=root, snapshot_kind="snapshot_directory"
            )
        except WorkspaceArchiveReadError as e:
            snapshot_error = e
        except Exception as e:
            snapshot_error = WorkspaceArchiveReadError(
                path=error_root, context={"reason": "snapshot_directory_failed"}, cause=e
            )
        finally:
            remount_error = await restore_detached_mounts()
            restore_error = await restore_ephemeral_paths()
            cleanup_error = remount_error
            if restore_error is not None:
                if cleanup_error is None:
                    cleanup_error = restore_error
                else:
                    additional_restore_errors = cleanup_error.context.setdefault(
                        "additional_restore_errors", []
                    )
                    assert isinstance(additional_restore_errors, list)
                    additional_restore_errors.append(
                        {
                            "message": restore_error.message,
                            "cause_type": (
                                type(restore_error.cause).__name__
                                if restore_error.cause is not None
                                else None
                            ),
                            "cause": str(restore_error.cause) if restore_error.cause else None,
                        }
                    )

            if cleanup_error is not None:
                if snapshot_error is not None:
                    cleanup_error.context["snapshot_error_before_restore_corruption"] = {
                        "message": snapshot_error.message
                    }
                raise cleanup_error

        if snapshot_error is not None:
            raise snapshot_error

        assert snapshot_id is not None
        return io.BytesIO(_encode_snapshot_directory_ref(snapshot_id=snapshot_id))

    def _extract_modal_snapshot_id(
        self,
        *,
        snap: object,
        root: Path,
        snapshot_kind: Literal["snapshot_filesystem", "snapshot_directory"],
    ) -> tuple[str | None, WorkspaceArchiveReadError | None]:
        if isinstance(snap, bytes | bytearray):
            return None, WorkspaceArchiveReadError(
                path=posix_path_for_error(root),
                context={
                    "reason": f"{snapshot_kind}_unexpected_bytes",
                    "type": type(snap).__name__,
                },
            )
        if not hasattr(snap, "object_id") and not isinstance(snap, str):
            return None, WorkspaceArchiveReadError(
                path=posix_path_for_error(root),
                context={
                    "reason": f"{snapshot_kind}_unexpected_return",
                    "type": type(snap).__name__,
                },
            )
        if isinstance(snap, str):
            return snap, None
        snapshot_id = getattr(snap, "object_id", None)
        if snapshot_id is not None and not isinstance(snapshot_id, str):
            snapshot_id = None
        if not snapshot_id:
            return None, WorkspaceArchiveReadError(
                path=posix_path_for_error(root),
                context={
                    "reason": f"{snapshot_kind}_unexpected_return",
                    "type": type(snap).__name__,
                },
            )
        return snapshot_id, None

    async def _refresh_sandbox_handle_for_snapshot(self) -> modal.Sandbox:
        await self._ensure_sandbox()
        assert self._sandbox is not None

        sandbox_module = type(self._sandbox).__module__
        if not sandbox_module.startswith("modal"):
            return self._sandbox

        sandbox_id = self.state.sandbox_id or getattr(self._sandbox, "object_id", None)
        if not sandbox_id:
            return self._sandbox

        try:
            refreshed = await self._call_modal(
                modal.Sandbox.from_id,
                sandbox_id,
                call_timeout=_DEFAULT_TIMEOUT_S,
            )
        except Exception:
            return self._sandbox

        self._sandbox = refreshed
        return refreshed

    def _modal_snapshot_plain_skip_relpaths(self, root: Path) -> set[Path]:
        plain_skip = set(self.state.manifest.ephemeral_entry_paths())
        if self._runtime_persist_workspace_skip_relpaths:
            plain_skip.update(self._runtime_persist_workspace_skip_relpaths)

        mount_skip_rel_paths: set[Path] = set()
        for rel_path, artifact in self.state.manifest.iter_entries():
            if isinstance(artifact, Mount) and artifact.ephemeral:
                mount_skip_rel_paths.add(rel_path)
        for _mount_entry, mount_path in self.state.manifest.ephemeral_mount_targets():
            try:
                mount_skip_rel_paths.add(mount_path.relative_to(root))
            except ValueError:
                continue
        return plain_skip - mount_skip_rel_paths

    def _modal_tar_skip_relpaths(self, root: Path) -> set[Path]:
        """Return Modal tar-capture skip paths, including resolved mount targets."""

        skip = self._persist_workspace_skip_relpaths()
        for _mount_entry, mount_path in self.state.manifest.mount_targets():
            try:
                skip.add(mount_path.relative_to(root))
            except ValueError:
                continue
        return skip

    @retry_async(
        retry_if=lambda exc, self: (
            exception_chain_contains_type(exc, (ExecTransportError,))
            or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
        )
    )
    async def _persist_workspace_via_tar(self) -> io.IOBase:
        # Existing tar implementation extracted so snapshot_filesystem mode can fall back cleanly.
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        skip = self._modal_tar_skip_relpaths(root)

        excludes: list[str] = []
        for rel in sorted(skip, key=lambda p: p.as_posix()):
            excludes.extend(["--exclude", f"./{rel.as_posix().lstrip('./')}"])

        cmd: list[str] = [
            "tar",
            "cf",
            "-",
            *excludes,
            "-C",
            root.as_posix(),
            ".",
        ]

        try:
            out = await self.exec(*cmd, shell=False)
            if not out.ok():
                raise WorkspaceArchiveReadError(
                    path=error_root,
                    context={
                        "reason": "tar_nonzero_exit",
                        "exit_code": out.exit_code,
                        "stderr": out.stderr.decode("utf-8", "replace"),
                    },
                )
            return io.BytesIO(out.stdout)
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:
            raise WorkspaceArchiveReadError(path=error_root, cause=e) from e

    async def _hydrate_workspace_via_snapshot_filesystem(self, data: io.IOBase) -> None:
        """
        Hydrate using Modal's snapshot_filesystem restore API when the
        persisted payload is a snapshot ref. Otherwise, fall back to tar
        extraction (to support SDKs that return tar bytes).
        """
        root = self._workspace_root_path()
        raw, snapshot_id = self._read_modal_snapshot_id_from_archive(
            data=data.read(),
            expected_persistence=_WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM,
            invalid_reason="snapshot_filesystem_invalid_snapshot_id",
        )
        if snapshot_id is None:
            return await self._hydrate_workspace_via_tar(io.BytesIO(raw))
        await self._restore_snapshot_filesystem_image(snapshot_id=snapshot_id, root=root)

    async def _hydrate_workspace_via_snapshot_directory(self, data: io.IOBase) -> None:
        """
        Hydrate using Modal's snapshot_directory restore API when the
        persisted payload is a snapshot ref. Otherwise, fall back to tar extraction.
        """

        root = self._workspace_root_path()
        raw, snapshot_id = self._read_modal_snapshot_id_from_archive(
            data=data.read(),
            expected_persistence=_WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY,
            invalid_reason="snapshot_directory_invalid_snapshot_id",
        )
        if snapshot_id is None:
            return await self._hydrate_workspace_via_tar(io.BytesIO(raw))
        await self._restore_snapshot_directory_image(snapshot_id=snapshot_id, root=root)

    def _read_modal_snapshot_id_from_archive(
        self,
        *,
        data: object,
        expected_persistence: WorkspacePersistenceMode,
        invalid_reason: str,
    ) -> tuple[bytes, str | None]:
        root = self._workspace_root_path()
        raw = data
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceArchiveWriteError(path=root, context={"reason": "non_bytes_payload"})
        raw_bytes = bytes(raw)

        snapshot_ref = _decode_modal_snapshot_ref(raw_bytes)
        if snapshot_ref is None:
            return raw_bytes, None
        workspace_persistence, snapshot_id = snapshot_ref
        if workspace_persistence != expected_persistence:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={"reason": invalid_reason, "workspace_persistence": workspace_persistence},
            )
        if not snapshot_id:
            raise WorkspaceArchiveWriteError(path=root, context={"reason": invalid_reason})
        return raw_bytes, snapshot_id

    async def _restore_snapshot_filesystem_image(self, *, snapshot_id: str, root: Path) -> None:
        prior = self._sandbox
        if prior is not None:
            try:
                await self._call_modal(prior.terminate, call_timeout=_DEFAULT_TIMEOUT_S)
            except Exception:
                pass
            finally:
                self._sandbox = None
                self.state.sandbox_id = None

        manifest_envs = cast(dict[str, str | None], await self.state.manifest.environment.resolve())

        async def _run_restore() -> None:
            image = modal.Image.from_id(snapshot_id)
            app = await modal.App.lookup.aio(self.state.app_name, create_if_missing=True)
            sb = await modal.Sandbox.create.aio(
                app=app,
                image=image,
                workdir=self.state.manifest.root,
                env=manifest_envs,
                encrypted_ports=self.state.exposed_ports,
                volumes=self._modal_cloud_bucket_mounts_for_manifest(),
                gpu=self.state.gpu,
                timeout=self.state.timeout,
                idle_timeout=self.state.idle_timeout,
            )
            try:
                mkdir_proc = await sb.exec.aio("mkdir", "-p", "--", root.as_posix(), text=False)
                await mkdir_proc.wait.aio()
            except Exception:
                pass
            self._image = image
            self.state.image_id = snapshot_id
            self._sandbox = sb
            self.state.sandbox_id = sb.object_id

        try:
            await asyncio.wait_for(
                _run_restore(), timeout=self.state.snapshot_filesystem_restore_timeout_s
            )
        except Exception as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "snapshot_filesystem_restore_failed",
                    "snapshot_id": snapshot_id,
                },
                cause=e,
            ) from e

    async def _restore_snapshot_directory_image(self, *, snapshot_id: str, root: Path) -> None:
        await self._ensure_sandbox()
        assert self._sandbox is not None
        sandbox = self._sandbox

        async def _run_restore() -> None:
            image = modal.Image.from_id(snapshot_id)
            await self._call_modal(
                sandbox.mount_image,
                root.as_posix(),
                image,
                call_timeout=self.state.snapshot_filesystem_restore_timeout_s,
            )
            for mount_entry, mount_path in reversed(
                self._snapshot_directory_mount_targets_to_restore(root)
            ):
                await mount_entry.mount_strategy.restore_after_snapshot(
                    mount_entry,
                    self,
                    mount_path,
                )

        try:
            await asyncio.wait_for(
                _run_restore(), timeout=self.state.snapshot_filesystem_restore_timeout_s
            )
        except Exception as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "snapshot_directory_restore_failed",
                    "snapshot_id": snapshot_id,
                },
                cause=e,
            ) from e

    def _snapshot_directory_mount_targets_to_restore(self, root: Path) -> list[tuple[Mount, Path]]:
        mount_targets: list[tuple[Mount, Path]] = []
        for mount_entry, mount_path in self.state.manifest.mount_targets():
            if mount_entry.ephemeral:
                continue
            if isinstance(mount_entry.mount_strategy, ModalCloudBucketMountStrategy):
                continue
            if mount_path != root and root not in mount_path.parents:
                continue
            mount_targets.append((mount_entry, mount_path))
        return mount_targets

    async def _hydrate_workspace_via_tar(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()

        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceArchiveWriteError(path=root, context={"reason": "non_bytes_tar_payload"})

        try:
            validate_tar_bytes(
                bytes(raw),
                skip_rel_paths=self.state.manifest.ephemeral_persistence_paths(),
            )
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root, context={"reason": e.reason, "member": e.member}, cause=e
            ) from e

        await self._ensure_sandbox()
        assert self._sandbox is not None

        async def _run_extract() -> None:
            assert self._sandbox is not None
            mkdir_proc = await self._sandbox.exec.aio(
                "mkdir", "-p", "--", root.as_posix(), text=False
            )
            await mkdir_proc.wait.aio()
            proc = await self._sandbox.exec.aio("tar", "xf", "-", "-C", root.as_posix(), text=False)
            await _write_process_stdin(proc, raw)
            exit_code = await proc.wait.aio()
            if exit_code != 0:
                stderr = await proc.stderr.read.aio()
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "reason": "tar_extract_nonzero_exit",
                        "exit_code": exit_code,
                        "stderr": stderr.decode("utf-8", "replace"),
                    },
                )

        try:
            await asyncio.wait_for(_run_extract(), timeout=60.0)
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e

    def _modal_cloud_bucket_mounts_for_manifest(
        self,
    ) -> dict[str | os.PathLike[Any], modal.Volume | modal.CloudBucketMount]:
        volumes: dict[str | os.PathLike[Any], modal.Volume | modal.CloudBucketMount] = {}
        for mount_entry, mount_path in self.state.manifest.mount_targets():
            strategy = mount_entry.mount_strategy
            if not isinstance(strategy, ModalCloudBucketMountStrategy):
                continue
            config = strategy._build_modal_cloud_bucket_mount_config(mount_entry)
            secret = None
            if config.secret_name is not None:
                secret = modal.Secret.from_name(
                    config.secret_name,
                    environment_name=config.secret_environment_name,
                )
            elif config.credentials is not None:
                secret = modal.Secret.from_dict(cast(dict[str, str | None], config.credentials))
            volumes[mount_path.as_posix()] = modal.CloudBucketMount(
                bucket_name=config.bucket_name,
                bucket_endpoint_url=config.bucket_endpoint_url,
                key_prefix=config.key_prefix,
                secret=secret,
                read_only=config.read_only,
            )
        return volumes


class ModalSandboxClient(BaseSandboxClient[ModalSandboxClientOptions]):
    backend_id = "modal"
    _default_image: ModalImageSelector | None
    _default_sandbox: ModalSandboxSelector | None
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        image: ModalImageSelector | None = None,
        sandbox: ModalSandboxSelector | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._default_image = image
        self._default_sandbox = sandbox
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    def _validate_manifest_for_workspace_persistence(
        self,
        *,
        manifest: Manifest,
        workspace_persistence: WorkspacePersistenceMode,
    ) -> None:
        if workspace_persistence != _WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY:
            return

        root = posix_path_as_path(coerce_posix_path(manifest.root))
        for mount_entry, mount_path in manifest.mount_targets():
            if not isinstance(mount_entry.mount_strategy, ModalCloudBucketMountStrategy):
                continue
            if mount_path == root or root in mount_path.parents:
                raise MountConfigError(
                    message=(
                        "snapshot_directory is not supported when a Modal cloud bucket mount "
                        "lives at or under the workspace root"
                    ),
                    context={
                        "workspace_root": root.as_posix(),
                        "mount_path": mount_path.as_posix(),
                        "workspace_persistence": workspace_persistence,
                    },
                )

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: ModalSandboxClientOptions,
    ) -> SandboxSession:
        """
        Create a new Modal-backed session.

        Expected options:
        - app_name: str (required)
        - sandbox_create_timeout_s: float | None (async timeout for sandbox creation call)
        - workspace_persistence: Literal["tar", "snapshot_filesystem", "snapshot_directory"]
          (optional)
        - snapshot_filesystem_timeout_s: float | None
          (async timeout for snapshot_filesystem call)
        - snapshot_filesystem_restore_timeout_s: float | None
          (async timeout for snapshot restore call)
        - timeout: int (maximum sandbox lifetime in seconds, default 300)
        - idle_timeout: int | None (maximum sandbox inactivity in seconds, default None)
        - image_builder_version: str | None (Modal image builder version, default "2025.06")
        """

        if options is None:
            raise ValueError("ModalSandboxClient.create requires options with app_name")
        manifest = manifest or Manifest()
        app_name = options.app_name
        if not app_name:
            raise ValueError("ModalSandboxClient.create requires a valid app_name")

        image_sel = self._default_image

        sandbox_sel = self._default_sandbox

        sandbox_create_timeout_s = options.sandbox_create_timeout_s
        if sandbox_create_timeout_s is not None and not isinstance(
            sandbox_create_timeout_s, int | float
        ):
            raise ValueError(
                "ModalSandboxClient.create requires sandbox_create_timeout_s to be a number"
            )

        workspace_persistence = options.workspace_persistence
        if workspace_persistence not in (
            _WORKSPACE_PERSISTENCE_TAR,
            _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM,
            _WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY,
        ):
            raise ValueError(
                "ModalSandboxClient.create requires workspace_persistence to be one of "
                f"{_WORKSPACE_PERSISTENCE_TAR!r}, "
                f"{_WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM!r}, or "
                f"{_WORKSPACE_PERSISTENCE_SNAPSHOT_DIRECTORY!r}"
            )
        snapshot_filesystem_timeout_s = options.snapshot_filesystem_timeout_s
        if snapshot_filesystem_timeout_s is not None and not isinstance(
            snapshot_filesystem_timeout_s, int | float
        ):
            raise ValueError(
                "ModalSandboxClient.create requires snapshot_filesystem_timeout_s to be a number"
            )

        snapshot_filesystem_restore_timeout_s = options.snapshot_filesystem_restore_timeout_s
        if snapshot_filesystem_restore_timeout_s is not None and not isinstance(
            snapshot_filesystem_restore_timeout_s, int | float
        ):
            raise ValueError(
                "ModalSandboxClient.create requires "
                "snapshot_filesystem_restore_timeout_s to be a number"
            )
        image_builder_version = options.image_builder_version
        if "image_builder_version" not in options.model_fields_set or image_builder_version == "":
            image_builder_version = _DEFAULT_IMAGE_BUILDER_VERSION
        elif image_builder_version is not None and not isinstance(image_builder_version, str):
            raise ValueError(
                "ModalSandboxClient.create requires image_builder_version to be a string or None"
            )

        self._validate_manifest_for_workspace_persistence(
            manifest=manifest,
            workspace_persistence=workspace_persistence,
        )

        session_id = uuid.uuid4()
        state_image_id: str | None = None
        state_image_tag: str | None = None
        session_image: modal.Image | None = None
        if image_sel is not None:
            if image_sel.kind == "image":
                if not isinstance(image_sel.value, modal.Image):
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires image to be a modal.Image"
                    )
                session_image = image_sel.value
                state_image_id = getattr(session_image, "object_id", None)
            elif image_sel.kind == "id":
                if not isinstance(image_sel.value, str) or not image_sel.value:
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires image_id to be a non-empty string"
                    )
                state_image_id = image_sel.value
            else:
                if not isinstance(image_sel.value, str) or not image_sel.value:
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires image_tag to be a non-empty string"
                    )
                state_image_tag = image_sel.value

        state_sandbox_id: str | None = None
        session_sandbox: modal.Sandbox | None = None
        if sandbox_sel is not None:
            if sandbox_sel.kind == "sandbox":
                if not isinstance(sandbox_sel.value, modal.Sandbox):
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires sandbox to be a modal.Sandbox"
                    )
                session_sandbox = sandbox_sel.value
                state_sandbox_id = getattr(session_sandbox, "object_id", None)
            else:
                if not isinstance(sandbox_sel.value, str) or not sandbox_sel.value:
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires sandbox_id to be a non-empty string"
                    )
                state_sandbox_id = sandbox_sel.value

        snapshot_id = str(session_id)
        snapshot_instance = resolve_snapshot(snapshot, snapshot_id)
        state = ModalSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            app_name=app_name,
            image_tag=state_image_tag,
            image_id=state_image_id,
            sandbox_id=state_sandbox_id,
            workspace_persistence=workspace_persistence,
            exposed_ports=options.exposed_ports,
            gpu=options.gpu,
            timeout=options.timeout,
            use_sleep_cmd=options.use_sleep_cmd,
            image_builder_version=image_builder_version,
            idle_timeout=options.idle_timeout,
        )
        if sandbox_create_timeout_s is not None:
            state.sandbox_create_timeout_s = float(sandbox_create_timeout_s)
        if snapshot_filesystem_timeout_s is not None:
            state.snapshot_filesystem_timeout_s = float(snapshot_filesystem_timeout_s)
        if snapshot_filesystem_restore_timeout_s is not None:
            state.snapshot_filesystem_restore_timeout_s = float(
                snapshot_filesystem_restore_timeout_s
            )

        # Pass the in-memory handles through to the session (they may not be resumable).
        inner = ModalSandboxSession.from_state(
            state,
            image=session_image,
            sandbox=session_sandbox,
        )
        await inner._ensure_sandbox()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        """
        Best-effort cleanup of Modal sandbox resources.
        """

        inner = session._inner
        if not isinstance(inner, ModalSandboxSession):
            raise TypeError("ModalSandboxClient.delete expects a ModalSandboxSession")

        # Prefer the live handle if present.
        sandbox = getattr(inner, "_sandbox", None)
        try:
            if sandbox is not None:
                await inner._call_modal(sandbox.terminate, call_timeout=_DEFAULT_TIMEOUT_S)
                return session
        except Exception:
            return session

        # Otherwise, best-effort terminate via sandbox_id.
        sid = inner.state.sandbox_id
        if sid:
            try:
                sb = await inner._call_modal(
                    modal.Sandbox.from_id,
                    sid,
                    call_timeout=_DEFAULT_TIMEOUT_S,
                )
                await inner._call_modal(sb.terminate, call_timeout=_DEFAULT_TIMEOUT_S)
            except Exception:
                pass

        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        if not isinstance(state, ModalSandboxSessionState):
            raise TypeError("ModalSandboxClient.resume expects a ModalSandboxSessionState")
        inner = ModalSandboxSession.from_state(state)
        reconnected = await inner._ensure_sandbox()
        if reconnected:
            inner._set_start_state_preserved(True)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return ModalSandboxSessionState.model_validate(payload)

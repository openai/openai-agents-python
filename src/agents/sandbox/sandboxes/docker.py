import asyncio
import errno
import hashlib
import io
import logging
import re
import socket
import tarfile
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

import docker.errors  # type: ignore[import-untyped]
import docker.utils.socket as docker_socket  # type: ignore[import-untyped]
from docker import DockerClient as DockerSDKClient
from docker.api.container import DEFAULT_DATA_CHUNK_SIZE  # type: ignore[import-untyped]
from docker.models.containers import Container  # type: ignore[import-untyped]
from docker.types import DriverConfig, Mount as DockerSDKMount  # type: ignore[import-untyped]
from docker.utils import parse_repository_tag

from ..entries import (
    Mount,
    resolve_workspace_path,
)
from ..entries.mounts import (
    FuseMountPattern,
    InContainerMountStrategy,
    MountpointMountPattern,
    RcloneMountPattern,
    S3FilesMountPattern,
)
from ..errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
)
from ..manifest import Manifest
from ..session import SandboxSession, SandboxSessionState
from ..session.base_sandbox_session import BaseSandboxSession
from ..session.dependencies import Dependencies
from ..session.manager import Instrumentation
from ..session.pty_types import (
    PTY_PROCESSES_MAX,
    PTY_PROCESSES_WARNING,
    PtyExecUpdate,
    allocate_pty_process_id,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
    truncate_text_by_tokens,
)
from ..session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ..session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ..session.workspace_payloads import coerce_write_payload
from ..snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ..types import ExecResult, ExposedPortEndpoint, User
from ..util.iterator_io import IteratorIO
from ..util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_has_status_code,
    retry_async,
)
from ..util.tar_utils import UnsafeTarMemberError, strip_tar_member_prefix, validate_tarfile
from ..workspace_paths import (
    coerce_posix_path,
    posix_path_as_path,
    posix_path_for_error,
    sandbox_path_str,
)

_DOCKER_EXECUTOR: Final = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="agents-docker-sandbox",
)

logger = logging.getLogger(__name__)

_PREPARE_USER_PTY_PID_SCRIPT = (
    'pid_path="$1"\n'
    'pid_user="$2"\n'
    'pid_parent="$(dirname "$pid_path")"\n'
    'mkdir -p "$pid_parent" && '
    'chmod 0711 "$pid_parent" && '
    ': > "$pid_path" && '
    'chown "$pid_user" "$pid_path" && '
    'chmod 0600 "$pid_path"\n'
)


class DockerSandboxSessionState(SandboxSessionState):
    type: Literal["docker"] = "docker"
    image: str
    container_id: str


class DockerSandboxClientOptions(BaseSandboxClientOptions):
    type: Literal["docker"] = "docker"
    image: str
    exposed_ports: tuple[int, ...] = ()

    def __init__(
        self,
        image: str,
        exposed_ports: tuple[int, ...] = (),
        *,
        type: Literal["docker"] = "docker",
    ) -> None:
        super().__init__(
            type=type,
            image=image,
            exposed_ports=exposed_ports,
        )


@dataclass
class _DockerPtyProcessEntry:
    exec_id: str
    sock: object
    raw_sock: object
    pid_path: Path
    tty: bool
    last_used: float = field(default_factory=time.monotonic)
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    output_closed: asyncio.Event = field(default_factory=asyncio.Event)
    reader_thread: threading.Thread | None = None
    wait_task: asyncio.Task[None] | None = None
    exit_code: int | None = None


@dataclass
class _DockerExecSocket:
    sock: object
    raw_sock: object
    response: object | None = None

    def close(self) -> None:
        try:
            cast(Any, self.sock).close()
        finally:
            if self.response is not None:
                try:
                    cast(Any, self.response).close()
                except Exception:
                    pass


class DockerSandboxSession(BaseSandboxSession):
    _docker_client: DockerSDKClient
    _container: Container
    _workspace_root_ready: bool
    _resume_workspace_probe_pending: bool
    _pty_lock: asyncio.Lock
    _pty_processes: dict[int, _DockerPtyProcessEntry]
    _reserved_pty_process_ids: set[int]

    state: DockerSandboxSessionState
    _ARCHIVE_STAGING_DIR: Path = posix_path_as_path(
        coerce_posix_path("/tmp/sandbox-docker-archive")
    )

    def __init__(
        self,
        *,
        docker_client: DockerSDKClient,
        container: Container,
        state: DockerSandboxSessionState,
    ) -> None:
        self._docker_client = docker_client
        self._container = container
        self.state = state
        self._workspace_root_ready = state.workspace_root_ready
        self._resume_workspace_probe_pending = False
        self._pty_lock = asyncio.Lock()
        self._pty_processes = {}
        self._reserved_pty_process_ids = set()

    @classmethod
    def from_state(
        cls,
        state: DockerSandboxSessionState,
        *,
        container: Container,
        docker_client: DockerSDKClient,
    ) -> "DockerSandboxSession":
        return cls(docker_client=docker_client, container=container, state=state)

    def supports_docker_volume_mounts(self) -> bool:
        """Docker attaches volume-driver mounts when creating the container."""

        return True

    def supports_pty(self) -> bool:
        return True

    @property
    def container_id(self) -> str:
        return self.state.container_id

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            self._container.reload()
        except docker.errors.APIError as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "docker", "detail": "container_reload_failed"},
                cause=e,
            ) from e

        attrs = getattr(self._container, "attrs", {}) or {}
        ports = attrs.get("NetworkSettings", {}).get("Ports", {})
        port_key = _docker_port_key(port)
        bindings = ports.get(port_key)
        if not isinstance(bindings, list) or not bindings:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "docker", "detail": "port_not_published", "port_key": port_key},
            )

        binding = bindings[0]
        if not isinstance(binding, dict):
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={
                    "backend": "docker",
                    "detail": "invalid_port_binding",
                    "port_key": port_key,
                },
            )

        host_ip = binding.get("HostIp")
        host_port = binding.get("HostPort")
        if not isinstance(host_ip, str) or not host_ip:
            host_ip = "127.0.0.1"
        if not isinstance(host_port, str) or not host_port.isdigit():
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "docker", "detail": "invalid_host_port", "port_key": port_key},
            )

        return ExposedPortEndpoint(host=host_ip, port=int(host_port), tls=False)

    def _archive_stage_path(self, *, name_hint: str) -> Path:
        # Unique name avoids clashes across concurrent reads/writes.
        return self._ARCHIVE_STAGING_DIR / f"{uuid.uuid4().hex}_{name_hint}"

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.container_id

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    @staticmethod
    def _path_has_nested_skip(path: Path, *, skip_rel_paths: set[Path]) -> bool:
        return any(path in skip_path.parents for skip_path in skip_rel_paths)

    async def _copy_workspace_tree_pruned(
        self,
        *,
        src_dir: Path,
        dst_dir: Path,
        rel_dir: Path,
        skip_rel_paths: set[Path],
    ) -> None:
        for entry in await self.ls(src_dir):
            src_child = Path(entry.path)
            rel_child = rel_dir / src_child.name
            if rel_child in skip_rel_paths:
                continue

            dst_child = dst_dir / src_child.name
            if entry.is_dir() and self._path_has_nested_skip(
                rel_child,
                skip_rel_paths=skip_rel_paths,
            ):
                await self._exec_checked(
                    "mkdir",
                    "-p",
                    sandbox_path_str(dst_child),
                    error_cls=WorkspaceArchiveReadError,
                    error_path=src_child,
                )
                await self._copy_workspace_tree_pruned(
                    src_dir=src_child,
                    dst_dir=dst_child,
                    rel_dir=rel_child,
                    skip_rel_paths=skip_rel_paths,
                )
                continue

            await self._exec_checked(
                "cp",
                "-R",
                "--",
                sandbox_path_str(src_child),
                sandbox_path_str(dst_child),
                error_cls=WorkspaceArchiveReadError,
                error_path=src_child,
            )

    async def _stage_workspace_copy(
        self,
        *,
        skip_rel_paths: set[Path],
    ) -> tuple[Path, Path]:
        root = self._workspace_root_path()
        root_name = root.name or "workspace"
        staging_parent = self._archive_stage_path(name_hint="workspace")
        staging_workspace = staging_parent / root_name
        skip_workspace_root = any(
            mount_path == root
            for _mount, mount_path in self.state.manifest.ephemeral_mount_targets()
        )

        await self._exec_checked(
            "mkdir",
            "-p",
            sandbox_path_str(staging_parent),
            error_cls=WorkspaceArchiveReadError,
            error_path=root,
        )
        if skip_workspace_root:
            # A mount on `/workspace` has no non-empty relative path to put in the prune set, so
            # skip the copy entirely and preserve only an empty workspace root in the archive.
            await self._exec_checked(
                "mkdir",
                "-p",
                sandbox_path_str(staging_workspace),
                error_cls=WorkspaceArchiveReadError,
                error_path=root,
            )
        elif skip_rel_paths:
            await self._exec_checked(
                "mkdir",
                "-p",
                sandbox_path_str(staging_workspace),
                error_cls=WorkspaceArchiveReadError,
                error_path=root,
            )
            await self._copy_workspace_tree_pruned(
                src_dir=root,
                dst_dir=staging_workspace,
                rel_dir=Path(),
                skip_rel_paths=skip_rel_paths,
            )
        else:
            await self._exec_checked(
                "cp",
                "-R",
                "--",
                root.as_posix(),
                sandbox_path_str(staging_workspace),
                error_cls=WorkspaceArchiveReadError,
                error_path=root,
            )
        return staging_parent, staging_workspace

    async def _rm_best_effort(self, path: Path) -> None:
        try:
            await self.exec("rm", "-rf", "--", sandbox_path_str(path), shell=False)
        except Exception:
            pass

    async def _exec_checked(
        self,
        *cmd: str | Path,
        error_cls: type[WorkspaceArchiveReadError] | type[WorkspaceArchiveWriteError],
        error_path: Path,
    ) -> ExecResult:
        res = await self.exec(*cmd, shell=False)
        if not res.ok():
            raise error_cls(
                path=error_path,
                context={
                    "command": [str(c) for c in cmd],
                    "stdout": res.stdout.decode("utf-8", errors="replace"),
                    "stderr": res.stderr.decode("utf-8", errors="replace"),
                },
            )
        return res

    async def _ensure_backend_started(self) -> None:
        self._container.reload()
        if not await self.running():
            self._container.start()

    async def _after_start(self) -> None:
        self._workspace_root_ready = True
        self._resume_workspace_probe_pending = False

    def _mark_workspace_root_ready_from_probe(self) -> None:
        super()._mark_workspace_root_ready_from_probe()
        self._workspace_root_ready = True

    async def _exec_run(
        self,
        *,
        cmd: list[str],
        workdir: str | None,
        user: str | None,
        timeout: float | None,
        command_for_errors: tuple[str | Path, ...],
        kill_on_timeout: bool,
    ) -> ExecResult:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            _DOCKER_EXECUTOR,
            lambda: self._container.exec_run(
                cmd=cmd,
                demux=True,
                workdir=workdir,
                user=user or "",
            ),
        )
        try:
            exec_result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as e:
            if kill_on_timeout:
                # Best-effort: kill processes matching the command line.
                # If this fails, the caller still gets a timeout error.
                try:
                    pattern = " ".join(str(c) for c in command_for_errors).replace("'", "'\\''")
                    self._container.exec_run(
                        cmd=[
                            "sh",
                            "-lc",
                            f"pkill -f -- '{pattern}' >/dev/null 2>&1 || true",
                        ],
                        demux=True,
                        user=user or "",
                    )
                except Exception:
                    pass
            raise ExecTimeoutError(command=command_for_errors, timeout_s=timeout, cause=e) from e
        except Exception as e:
            raise ExecTransportError(command=command_for_errors, cause=e) from e

        stdout, stderr = exec_result.output
        stdout_bytes = stdout or b""
        stderr_bytes = stderr or b""
        exit_code = exec_result.exit_code
        if exit_code is None:
            raise ExecTransportError(
                command=command_for_errors,
                context={
                    "reason": "missing_exit_code",
                    "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                    "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                    "workdir": workdir,
                    "retry_safe": True,
                },
            )
        return ExecResult(
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            exit_code=exit_code,
        )

    async def _recover_workspace_root_ready(self, *, timeout: float | None) -> None:
        if self._workspace_root_ready or not self._resume_workspace_probe_pending:
            return

        root = self.state.manifest.root
        probe_command = ("test", "-d", root)
        try:
            result = await self._exec_run(
                cmd=[str(c) for c in probe_command],
                workdir=None,
                user=None,
                timeout=timeout,
                command_for_errors=probe_command,
                kill_on_timeout=False,
            )
        except (ExecTimeoutError, ExecTransportError):
            return
        finally:
            self._resume_workspace_probe_pending = False

        if result.ok():
            self._mark_workspace_root_ready_from_probe()

    @staticmethod
    def _coerce_exec_user(user: str | User | None) -> str | None:
        if isinstance(user, User):
            return user.name
        return user

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
    ) -> ExecResult:
        if user is None:
            return await super().exec(*command, timeout=timeout, shell=shell, user=None)

        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=None)
        return await self._exec_internal_for_user(
            *sanitized_command,
            timeout=timeout,
            user=self._coerce_exec_user(user),
        )

    async def _exec_internal(
        self, *command: str | Path, timeout: float | None = None
    ) -> ExecResult:
        return await self._exec_internal_for_user(*command, timeout=timeout, user=None)

    async def _exec_internal_for_user(
        self,
        *command: str | Path,
        timeout: float | None = None,
        user: str | None = None,
    ) -> ExecResult:
        # `docker-py` is synchronous and can block indefinitely (e.g. hung
        # process, daemon issues). Run in a worker thread so we can enforce a
        # timeout without requiring `timeout(1)` in the container image.
        # Use a shared bounded executor so repeated timeouts do not leak one
        # new thread per command.
        cmd: list[str] = [str(c) for c in command]
        await self._recover_workspace_root_ready(timeout=timeout)
        # The workspace root is created during `apply_manifest()`, so the first
        # bootstrap commands must not force Docker to chdir there yet.
        workdir = self.state.manifest.root if self._workspace_root_ready else None
        return await self._exec_run(
            cmd=cmd,
            workdir=workdir,
            user=user,
            timeout=timeout,
            command_for_errors=command,
            kill_on_timeout=True,
        )

    async def _stream_into_exec(
        self,
        *,
        cmd: list[str],
        stream: io.IOBase,
        error_path: Path,
        user: str | User | None = None,
    ) -> None:
        def _write() -> int | None:
            container_client = self._container.client
            assert container_client is not None
            api = container_client.api
            resp = api.exec_create(
                self._container.id,
                cmd,
                stdin=True,
                stdout=True,
                stderr=True,
                workdir=None,
                user=self._coerce_exec_user(user) or "",
            )
            exec_socket = self._start_exec_socket(api=api, exec_id=cast(str, resp["Id"]))
            sock = exec_socket.sock
            raw_sock = exec_socket.raw_sock
            try:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    elif not isinstance(chunk, bytes):
                        chunk = bytes(chunk)
                    if hasattr(raw_sock, "sendall"):
                        raw_sock.sendall(chunk)
                    else:
                        cast(Any, sock).write(chunk)

                try:
                    if hasattr(raw_sock, "shutdown"):
                        raw_sock.shutdown(socket.SHUT_WR)
                    else:
                        cast(Any, sock).flush()
                except Exception:
                    pass

                try:
                    if hasattr(raw_sock, "recv"):
                        while raw_sock.recv(1024 * 1024):
                            pass
                    else:
                        while cast(Any, sock).read(1024 * 1024):
                            pass
                except Exception:
                    pass
            finally:
                exec_socket.close()

            return cast(int | None, api.exec_inspect(resp["Id"]).get("ExitCode"))

        loop = asyncio.get_running_loop()
        try:
            exit_code = await loop.run_in_executor(_DOCKER_EXECUTOR, _write)
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=error_path, cause=e) from e

        if exit_code not in (0, None):
            raise WorkspaceArchiveWriteError(
                path=error_path,
                context={
                    "command": cmd,
                    "exit_code": str(exit_code),
                },
            )

    async def _write_stream_via_exec(
        self,
        *,
        staging_path: Path,
        stream: io.IOBase,
        user: str | User | None = None,
    ) -> None:
        await self._stream_into_exec(
            cmd=["sh", "-lc", 'cat > "$1"', "sh", sandbox_path_str(staging_path)],
            stream=stream,
            error_path=staging_path,
            user=user,
        )

    async def _prepare_user_pty_pid_path(self, *, path: Path, user: str | None) -> None:
        if user is None:
            return
        await self._exec_checked(
            "sh",
            "-lc",
            _PREPARE_USER_PTY_PID_SCRIPT,
            "sh",
            sandbox_path_str(path),
            user,
            error_cls=WorkspaceArchiveWriteError,
            error_path=path,
        )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        workspace_path = await self._validate_path_access(path)

        # Read from inside the container instead of `get_archive()`: with Docker
        # volume-driver-backed mounts attached, daemon archive operations can re-run volume mount
        # setup and some plugins reject the duplicate `Mount` call for the same container id.
        workspace_path_arg = sandbox_path_str(workspace_path)
        res = await self.exec("cat", "--", workspace_path_arg, shell=False, user=user)
        if not res.ok():
            raise WorkspaceReadNotFoundError(
                path=path,
                context={
                    "command": ["cat", "--", workspace_path_arg],
                    "stdout": res.stdout.decode("utf-8", errors="replace"),
                    "stderr": res.stderr.decode("utf-8", errors="replace"),
                },
            )
        return io.BytesIO(res.stdout)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        payload = coerce_write_payload(path=path, data=data)

        path = await self._validate_path_access(path, for_write=True)

        if user is not None:
            await self._stream_into_exec(
                cmd=[
                    "sh",
                    "-lc",
                    'mkdir -p "$(dirname "$1")" && cat > "$1"',
                    "sh",
                    sandbox_path_str(path),
                ],
                stream=payload.stream,
                error_path=path,
                user=user,
            )
            return

        parent = path.parent
        await self.mkdir(parent, parents=True)

        # Stream into a temporary file from inside the container, then copy into place.
        # Avoid `put_archive()`: with Docker volume-driver-backed mounts attached, the daemon can
        # re-run volume mount setup during archive operations and some plugins reject the
        # duplicate `Mount` call for the same container id.
        staging_path = self._archive_stage_path(name_hint=path.name)

        await self._exec_checked(
            "mkdir",
            "-p",
            sandbox_path_str(self._ARCHIVE_STAGING_DIR),
            error_cls=WorkspaceArchiveWriteError,
            error_path=self._ARCHIVE_STAGING_DIR,
        )

        await self._write_stream_via_exec(
            staging_path=staging_path,
            stream=payload.stream,
        )

        # Copy into place using a process inside the container, which can see mounts.
        staging_path_arg = sandbox_path_str(staging_path)
        path_arg = sandbox_path_str(path)
        cp_res = await self.exec("cp", "--", staging_path_arg, path_arg, shell=False)
        if not cp_res.ok():
            raise WorkspaceArchiveWriteError(
                path=parent,
                context={
                    "command": ["cp", "--", staging_path_arg, path_arg],
                    "stdout": cp_res.stdout.decode("utf-8", errors="replace"),
                    "stderr": cp_res.stderr.decode("utf-8", errors="replace"),
                },
            )

        # Best-effort cleanup. Ignore failures (e.g. concurrent cleanup).
        await self._rm_best_effort(staging_path)

    async def running(self) -> bool:
        # docker-py caches container attributes; refresh to avoid stale status,
        # especially right after start/stop.
        try:
            self._container.reload()
        except docker.errors.APIError:
            # Best-effort: if we can't reload, fall back to last known status.
            pass
        return cast(str, self._container.status) == "running"

    async def _shutdown_backend(self) -> None:
        # Best-effort: stop the container if it exists.
        try:
            self._container.reload()
        except Exception:
            pass
        try:
            if await self.running():
                self._container.stop()
        except Exception:
            # If the container is already gone/stopped, ignore.
            pass

    @staticmethod
    def _start_exec_socket(*, api: Any, exec_id: str, tty: bool = False) -> _DockerExecSocket:
        if not all(
            callable(getattr(api, attr, None))
            for attr in ("_post_json", "_url", "_get_raw_response_socket")
        ):
            sock = api.exec_start(exec_id, socket=True, tty=tty)
            return _DockerExecSocket(sock=sock, raw_sock=getattr(sock, "_sock", sock))

        response = api._post_json(
            api._url("/exec/{0}/start", exec_id),
            headers={"Connection": "Upgrade", "Upgrade": "tcp"},
            data={"Tty": tty, "Detach": False},
            stream=True,
        )
        sock = api._get_raw_response_socket(response)
        raw_sock = getattr(sock, "_sock", sock)
        return _DockerExecSocket(sock=sock, raw_sock=raw_sock, response=response)

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
        docker_user = self._coerce_exec_user(user)
        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=None)
        cmd = [str(c) for c in sanitized_command]
        await self._recover_workspace_root_ready(timeout=timeout)
        workdir = self.state.manifest.root if self._workspace_root_ready else None

        loop = asyncio.get_running_loop()
        container_client = self._container.client
        assert container_client is not None
        api = container_client.api

        entry: _DockerPtyProcessEntry | None = None
        pty_pid_path: Path | None = None
        registered = False
        pruned_entry: _DockerPtyProcessEntry | None = None
        process_id = 0
        process_count = 0

        try:
            pty_pid_path = self._archive_stage_path(name_hint="pty.pid")
            await self._prepare_user_pty_pid_path(path=pty_pid_path, user=docker_user)
            wrapped_cmd = [
                "sh",
                "-lc",
                'mkdir -p "$1" && printf "%s" "$$" > "$2" && shift 2 && exec "$@"',
                "sh",
                sandbox_path_str(pty_pid_path.parent),
                sandbox_path_str(pty_pid_path),
                *cmd,
            ]
            resp = await asyncio.wait_for(
                loop.run_in_executor(
                    _DOCKER_EXECUTOR,
                    lambda: api.exec_create(
                        self._container.id,
                        wrapped_cmd,
                        stdin=True,
                        stdout=True,
                        stderr=True,
                        tty=tty,
                        workdir=workdir,
                        user=docker_user or "",
                    ),
                ),
                timeout=timeout,
            )
            exec_id = cast(str, resp["Id"])
            exec_socket = await asyncio.wait_for(
                loop.run_in_executor(
                    _DOCKER_EXECUTOR,
                    lambda: self._start_exec_socket(api=api, exec_id=exec_id, tty=tty),
                ),
                timeout=timeout,
            )
            raw_sock = exec_socket.raw_sock
            if not tty:
                try:
                    cast(Any, raw_sock).shutdown(socket.SHUT_WR)
                except Exception:
                    pass
            entry = _DockerPtyProcessEntry(
                exec_id=exec_id,
                sock=exec_socket,
                raw_sock=raw_sock,
                pid_path=pty_pid_path,
                tty=tty,
            )
            entry.reader_thread = threading.Thread(
                target=self._pump_pty_socket,
                args=(entry, loop),
                daemon=True,
                name=f"agents-docker-pty-{exec_id[:12]}",
            )
            entry.reader_thread.start()
            entry.wait_task = asyncio.create_task(self._watch_pty_exit(entry))

            async with self._pty_lock:
                process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
                self._reserved_pty_process_ids.add(process_id)
                pruned_entry = self._prune_pty_processes_if_needed()
                self._pty_processes[process_id] = entry
                process_count = len(self._pty_processes)
                registered = True
        except asyncio.TimeoutError as e:
            if entry is not None and not registered:
                await self._terminate_pty_entry(entry)
            elif pty_pid_path is not None:
                await self._kill_pty_pid_path(pty_pid_path)
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except Exception as e:
            if entry is not None and not registered:
                await self._terminate_pty_entry(entry)
            raise ExecTransportError(
                command=command,
                context={"retry_safe": True},
                cause=e,
            ) from e
        except BaseException:
            if entry is not None and not registered:
                await self._terminate_pty_entry(entry)
            raise

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
            loop = asyncio.get_running_loop()
            payload = chars.encode("utf-8")
            try:
                await loop.run_in_executor(
                    _DOCKER_EXECUTOR,
                    lambda: cast(Any, entry.raw_sock).sendall(payload),
                )
            except (BrokenPipeError, OSError) as e:
                if not isinstance(e, BrokenPipeError) and e.errno not in {
                    errno.EPIPE,
                    errno.EBADF,
                    errno.ECONNRESET,
                }:
                    raise
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

    def _pump_pty_socket(
        self, entry: _DockerPtyProcessEntry, loop: asyncio.AbstractEventLoop
    ) -> None:
        try:
            for stream_id, chunk in docker_socket.frames_iter(entry.raw_sock, tty=entry.tty):
                _ = stream_id
                future = asyncio.run_coroutine_threadsafe(
                    self._append_pty_output_chunks(entry, [bytes(chunk)]),
                    loop,
                )
                future.result()
        except Exception:
            pass
        finally:
            future = asyncio.run_coroutine_threadsafe(
                self._mark_pty_output_closed(entry),
                loop,
            )
            try:
                future.result()
            except Exception:
                pass

    async def _append_pty_output_chunks(
        self, entry: _DockerPtyProcessEntry, chunks: list[bytes]
    ) -> None:
        async with entry.output_lock:
            entry.output_chunks.extend(chunks)
        entry.output_notify.set()

    async def _mark_pty_output_closed(self, entry: _DockerPtyProcessEntry) -> None:
        entry.output_closed.set()
        entry.output_notify.set()

    async def _watch_pty_exit(self, entry: _DockerPtyProcessEntry) -> None:
        loop = asyncio.get_running_loop()
        container_client = self._container.client
        if container_client is None:
            entry.output_notify.set()
            return
        api = container_client.api

        while True:
            try:
                inspect_result = await loop.run_in_executor(
                    _DOCKER_EXECUTOR,
                    lambda: api.exec_inspect(entry.exec_id),
                )
            except Exception:
                break

            if not inspect_result.get("Running", False):
                exit_code = inspect_result.get("ExitCode")
                if exit_code is not None:
                    entry.exit_code = int(exit_code)
                break

            await asyncio.sleep(0.05)

        entry.output_notify.set()

    async def _refresh_pty_exit_code(self, entry: _DockerPtyProcessEntry) -> None:
        if entry.exit_code is not None:
            return

        loop = asyncio.get_running_loop()
        container_client = self._container.client
        if container_client is None:
            return
        api = container_client.api

        try:
            inspect_result = await loop.run_in_executor(
                _DOCKER_EXECUTOR,
                lambda: api.exec_inspect(entry.exec_id),
            )
        except Exception:
            return

        if inspect_result.get("Running", False):
            return

        exit_code = inspect_result.get("ExitCode")
        if exit_code is not None:
            entry.exit_code = int(exit_code)

    async def _collect_pty_output(
        self,
        *,
        entry: _DockerPtyProcessEntry,
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

            if entry.output_closed.is_set():
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
        entry: _DockerPtyProcessEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        if entry.output_closed.is_set() and entry.exit_code is None:
            await self._refresh_pty_exit_code(entry)

        exit_code = entry.exit_code
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

    def _prune_pty_processes_if_needed(self) -> _DockerPtyProcessEntry | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None

        meta = [
            (process_id, entry.last_used, entry.exit_code is not None)
            for process_id, entry in self._pty_processes.items()
        ]
        process_id = process_id_to_prune_from_meta(meta)
        if process_id is None:
            return None

        self._reserved_pty_process_ids.discard(process_id)
        return self._pty_processes.pop(process_id, None)

    async def _terminate_pty_entry(self, entry: _DockerPtyProcessEntry) -> None:
        if entry.wait_task is not None:
            entry.wait_task.cancel()

        await self._refresh_pty_exit_code(entry)

        if entry.exit_code is None:
            await self._kill_pty_pid_path(entry.pid_path)
        else:
            await self._rm_best_effort(entry.pid_path)

        try:
            cast(Any, entry.sock).close()
        except Exception:
            pass

        if entry.reader_thread is not None:
            await asyncio.to_thread(entry.reader_thread.join, 1.0)

        await asyncio.gather(
            *(task for task in (entry.wait_task,) if task is not None),
            return_exceptions=True,
        )

    async def _kill_pty_pid_path(self, pid_path: Path) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                _DOCKER_EXECUTOR,
                lambda: self._container.exec_run(
                    cmd=[
                        "sh",
                        "-lc",
                        (
                            'if [ -f "$1" ]; then '
                            'pid="$(cat "$1" 2>/dev/null || true)"; '
                            'if [ -n "$pid" ]; then '
                            'kill -KILL "$pid" >/dev/null 2>&1 || true; '
                            "fi; "
                            "fi"
                        ),
                        "sh",
                        sandbox_path_str(pid_path),
                    ],
                    demux=True,
                ),
            )
        except Exception:
            pass

        await self._rm_best_effort(pid_path)

    async def exists(self) -> bool:
        try:
            self._docker_client.containers.get(self.state.container_id)
            return True
        except docker.errors.NotFound:
            return False

    @retry_async(
        retry_if=lambda exc, self: exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
    )
    async def persist_workspace(self) -> io.IOBase:
        skip = self._persist_workspace_skip_relpaths()
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        try:
            staging_parent, staging_workspace = await self._stage_workspace_copy(
                skip_rel_paths=skip
            )
            root_prefixed_archive = self._workspace_archive_stream(
                staging_workspace,
                cleanup_path=staging_parent,
            )
            return strip_tar_member_prefix(root_prefixed_archive, prefix=staging_workspace.name)
        except docker.errors.NotFound as e:
            raise WorkspaceArchiveReadError(path=error_root, cause=e) from e
        except docker.errors.APIError as e:
            raise WorkspaceArchiveReadError(path=error_root, cause=e) from e

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        with tempfile.TemporaryFile() as archive:
            while True:
                chunk = data.read(io.DEFAULT_BUFFER_SIZE)
                if chunk in ("", b""):
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if not isinstance(chunk, bytes | bytearray):
                    raise WorkspaceArchiveWriteError(
                        path=error_root,
                        context={"reason": "non_bytes_tar_payload"},
                    )
                archive.write(chunk)

            try:
                archive.seek(0)
                with tarfile.open(fileobj=archive, mode="r:*") as tar:
                    validate_tarfile(tar)
            except UnsafeTarMemberError as e:
                raise WorkspaceArchiveWriteError(
                    path=error_root,
                    context={"reason": e.reason, "member": e.member},
                    cause=e,
                ) from e
            except (tarfile.TarError, OSError) as e:
                raise WorkspaceArchiveWriteError(path=error_root, cause=e) from e

            await self._exec_checked(
                "mkdir",
                "-p",
                root.as_posix(),
                error_cls=WorkspaceArchiveWriteError,
                error_path=error_root,
            )
            archive.seek(0)
            await self._stream_into_exec(
                cmd=["tar", "-x", "-C", root.as_posix()],
                stream=archive,
                error_path=error_root,
            )

    def _schedule_rm_best_effort(self, path: Path) -> None:
        loop = asyncio.get_running_loop()
        loop.create_task(self._rm_best_effort(path))

    def _workspace_archive_stream(
        self,
        path: Path,
        *,
        cleanup_path: Path | None = None,
    ) -> io.IOBase:
        on_close = (
            (lambda: self._schedule_rm_best_effort(cleanup_path))
            if cleanup_path is not None
            else None
        )
        container_client = getattr(self._container, "client", None)
        api = getattr(container_client, "api", None)
        if api is None:
            bits, _ = self._container.get_archive(sandbox_path_str(path))
            return IteratorIO(it=cast(Iterator[bytes], bits), on_close=on_close)

        url = api._url("/containers/{0}/archive", self._container.id)
        response = api._get(
            url,
            params={"path": sandbox_path_str(path)},
            stream=True,
            headers={"Accept-Encoding": "identity"},
        )
        api._raise_for_status(response)
        return IteratorIO(it=self._iter_archive_chunks(api, response), on_close=on_close)

    @staticmethod
    def _iter_archive_chunks(api: Any, response: Any) -> Iterator[bytes]:
        try:
            yield from api._stream_raw_result(
                response,
                chunk_size=DEFAULT_DATA_CHUNK_SIZE,
                decode=False,
            )
        finally:
            try:
                response.close()
            except Exception:
                pass


class DockerSandboxClient(BaseSandboxClient[DockerSandboxClientOptions]):
    backend_id = "docker"
    docker_client: DockerSDKClient
    _instrumentation: Instrumentation

    def __init__(
        self,
        docker_client: DockerSDKClient,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self.docker_client = docker_client
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: DockerSandboxClientOptions,
    ) -> SandboxSession:
        image = options.image
        session_id = uuid.uuid4()
        manifest = manifest or Manifest()

        container = await self._create_container(
            image,
            manifest=manifest,
            exposed_ports=options.exposed_ports,
            session_id=session_id,
        )
        container.start()

        container_id = container.id
        assert container_id is not None
        snapshot_id = str(session_id)
        snapshot_instance = resolve_snapshot(snapshot, snapshot_id)
        state = DockerSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            image=image,
            snapshot=snapshot_instance,
            container_id=container_id,
            exposed_ports=options.exposed_ports,
        )

        inner = DockerSandboxSession(
            docker_client=self.docker_client,
            container=container,
            state=state,
        )
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, DockerSandboxSession):
            raise TypeError("DockerSandboxClient.delete expects a DockerSandboxSession")
        volume_names = _docker_volume_names_for_manifest(
            inner.state.manifest,
            session_id=inner.state.session_id,
        )
        try:
            container = self.docker_client.containers.get(inner.state.container_id)
        except docker.errors.NotFound:
            container = None
        else:
            # Ensure teardown happens before removal.
            try:
                await inner.shutdown()
            except Exception:
                pass
            try:
                container.remove()
            except docker.errors.NotFound:
                pass

        for volume_name in volume_names:
            try:
                volume = self.docker_client.volumes.get(volume_name)
            except docker.errors.NotFound:
                continue
            volume.remove()
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        if not isinstance(state, DockerSandboxSessionState):
            raise TypeError("DockerSandboxClient.resume expects a DockerSandboxSessionState")
        container = self.get_container(state.container_id)
        reused_existing_container = container is not None
        if container is None:
            container = await self._create_container(
                state.image,
                manifest=state.manifest,
                exposed_ports=state.exposed_ports,
                session_id=state.session_id,
            )
            container_id = container.id
            assert container_id is not None
            state.container_id = container_id
            state.workspace_root_ready = False

        # Use the existing container (or the one we just created).
        inner = DockerSandboxSession(
            container=container, docker_client=self.docker_client, state=state
        )
        inner._resume_workspace_probe_pending = True
        inner._set_start_state_preserved(reused_existing_container)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return DockerSandboxSessionState.model_validate(payload)

    async def _create_container(
        self,
        image: str,
        *,
        manifest: Manifest | None = None,
        exposed_ports: tuple[int, ...] = (),
        session_id: uuid.UUID | None = None,
    ) -> Container:
        # create image if it does not exist
        if not self.image_exists(image):
            repo, tag = parse_repository_tag(image)
            self.docker_client.images.pull(repo, tag=tag or None, all_tags=False)

        assert self.image_exists(image)
        environment: dict[str, str] | None = None
        if manifest:
            environment = await manifest.environment.resolve()
        create_kwargs: dict[str, object] = {
            "entrypoint": ["tail"],
            "image": image,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": environment,
        }
        if manifest is not None:
            docker_mounts = _build_docker_volume_mounts(manifest, session_id=session_id)
            if docker_mounts:
                create_kwargs["mounts"] = docker_mounts
            if _manifest_requires_fuse(manifest):
                create_kwargs.update(
                    devices=["/dev/fuse"],
                    cap_add=["SYS_ADMIN"],
                    security_opt=["apparmor:unconfined"],
                )
            elif _manifest_requires_sys_admin(manifest):
                create_kwargs.update(
                    cap_add=["SYS_ADMIN"],
                    security_opt=["apparmor:unconfined"],
                )
        if exposed_ports:
            create_kwargs["ports"] = {
                _docker_port_key(port): ("127.0.0.1", None) for port in exposed_ports
            }
        return self.docker_client.containers.create(**create_kwargs)

    def image_exists(self, image: str) -> bool:
        try:
            self.docker_client.images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def get_container(self, container_id: str) -> Container | None:
        try:
            return self.docker_client.containers.get(container_id)
        except docker.errors.NotFound:
            return None


def _docker_port_key(port: int) -> str:
    return f"{port}/tcp"


def _manifest_requires_fuse(manifest: Manifest | None) -> bool:
    if manifest is None:
        return False
    for _path, artifact in manifest.iter_entries():
        if not isinstance(artifact, Mount):
            continue
        strategy = artifact.mount_strategy
        if not isinstance(strategy, InContainerMountStrategy):
            continue
        if isinstance(strategy.pattern, FuseMountPattern | MountpointMountPattern):
            return True
        if isinstance(strategy.pattern, RcloneMountPattern) and strategy.pattern.mode == "fuse":
            return True
    return False


def _manifest_requires_sys_admin(manifest: Manifest | None) -> bool:
    if manifest is None:
        return False
    for _path, artifact in manifest.iter_entries():
        if not isinstance(artifact, Mount):
            continue
        strategy = artifact.mount_strategy
        if isinstance(strategy, InContainerMountStrategy):
            if isinstance(strategy.pattern, RcloneMountPattern) and strategy.pattern.mode == "nfs":
                return True
            if isinstance(strategy.pattern, S3FilesMountPattern):
                return True
    return False


def _build_docker_volume_mounts(
    manifest: Manifest,
    *,
    session_id: uuid.UUID | None,
) -> list[DockerSDKMount]:
    mounts: list[DockerSDKMount] = []

    for artifact, mount_path in _docker_volume_mounts_for_manifest(manifest):
        driver_config = artifact.mount_strategy.build_docker_volume_driver_config(artifact)
        assert driver_config is not None
        driver_name, driver_options, read_only = driver_config
        mounts.append(
            DockerSDKMount(
                target=mount_path.as_posix(),
                source=_docker_volume_name(session_id=session_id, mount_path=mount_path),
                type="volume",
                read_only=read_only,
                driver_config=DriverConfig(name=driver_name, options=driver_options),
            )
        )

    return mounts


def _docker_volume_names_for_manifest(
    manifest: Manifest,
    *,
    session_id: uuid.UUID | None,
) -> list[str]:
    return [
        _docker_volume_name(session_id=session_id, mount_path=mount_path)
        for _artifact, mount_path in _docker_volume_mounts_for_manifest(manifest)
    ]


def _docker_volume_mounts_for_manifest(manifest: Manifest) -> list[tuple[Mount, Path]]:
    mounts: list[tuple[Mount, Path]] = []
    root = posix_path_as_path(coerce_posix_path(manifest.root))
    for rel_path, artifact in manifest.iter_entries():
        if not isinstance(artifact, Mount):
            continue
        if artifact.mount_strategy.build_docker_volume_driver_config(artifact) is None:
            continue

        dest = resolve_workspace_path(root, rel_path)
        mount_path = artifact._resolve_mount_path_for_root(root, dest)
        normalized_mount_path = manifest._normalize_in_workspace_path(root, mount_path)
        if normalized_mount_path is not None:
            mount_path = normalized_mount_path

        mounts.append((artifact, mount_path))
    return mounts


def _docker_volume_name(*, session_id: uuid.UUID | None, mount_path: Path) -> str:
    session_prefix = f"{session_id.hex}_" if session_id is not None else ""
    # Keep the readable path suffix, but include a path hash so distinct mount
    # targets like `/workspace/a_b` and `/workspace/a/b` cannot alias after
    # slash replacement.
    mount_path_posix = mount_path.as_posix()
    path_hash = hashlib.sha256(mount_path_posix.encode("utf-8")).hexdigest()[:12]
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", mount_path_posix.strip("/")) or "workspace"
    return f"sandbox_{session_prefix}{path_hash}_{sanitized}"

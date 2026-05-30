"""
OpenShell sandbox (https://github.com/NVIDIA/OpenShell) implementation.

Export ``OPENSHELL_GATEWAY`` or configure a gateway cluster to connect.

The ``openshell`` dependency is optional, so package-level exports should guard
imports of this module. Within this module, OpenShell SDK imports are lazy so
users without the extra can still import the package.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import logging
import shlex
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field

from ....sandbox.errors import (
    ExecTransportError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import SandboxSession, SandboxSessionState
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.session.tar_workspace import shell_tar_exclude_args
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, User
from ....sandbox.workspace_paths import posix_path_as_path, sandbox_path_str

logger = logging.getLogger(__name__)

# OpenShell phase constant for a ready sandbox.
_OPENSHELL_PHASE_READY = 2


class OpenShellSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the OpenShell sandbox backend."""

    type: Literal["openshell"] = "openshell"
    cluster: str | None = None
    endpoint: str | None = None
    tls_ca_path: str | None = None
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
    image: str | None = None
    envs: dict[str, str] | None = None
    gpu: bool = False
    providers: list[str] | None = None
    timeout: float = 30.0
    ready_timeout: float = 120.0

    def __init__(
        self,
        cluster: str | None = None,
        endpoint: str | None = None,
        tls_ca_path: str | None = None,
        tls_cert_path: str | None = None,
        tls_key_path: str | None = None,
        image: str | None = None,
        envs: dict[str, str] | None = None,
        gpu: bool = False,
        providers: list[str] | None = None,
        timeout: float = 30.0,
        ready_timeout: float = 120.0,
        *,
        type: Literal["openshell"] = "openshell",
    ) -> None:
        super().__init__(
            type=type,
            cluster=cluster,
            endpoint=endpoint,
            tls_ca_path=tls_ca_path,
            tls_cert_path=tls_cert_path,
            tls_key_path=tls_key_path,
            image=image,
            envs=envs,
            gpu=gpu,
            providers=providers,
            timeout=timeout,
            ready_timeout=ready_timeout,
        )


class OpenShellSandboxSessionState(SandboxSessionState):
    """Serializable state for an OpenShell-backed session."""

    type: Literal["openshell"] = "openshell"
    sandbox_id: str
    sandbox_name: str
    cluster: str | None = None
    endpoint: str | None = None
    tls_ca_path: str | None = None
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
    base_envs: dict[str, str] = Field(default_factory=dict)
    image: str | None = None
    gpu: bool = False
    providers_list: list[str] = Field(default_factory=list)
    client_timeout: float = 30.0
    ready_timeout: float = 120.0


def _import_openshell_client() -> Any:
    """Lazy-import the OpenShell SandboxClient class."""
    try:
        from openshell.sandbox import SandboxClient

        return SandboxClient
    except ImportError as exc:
        raise ImportError(
            "OpenShellSandboxClient requires the optional `openshell` dependency.\n"
            "Install the openshell extra before using this sandbox backend."
        ) from exc


def _import_openshell_proto() -> Any:
    """Lazy-import the OpenShell protobuf module."""
    try:
        from openshell._proto import openshell_pb2

        return openshell_pb2
    except ImportError as exc:
        raise ImportError(
            "OpenShellSandboxClient requires the optional `openshell` dependency."
        ) from exc


def _build_tls_config(
    *,
    ca_path: str | None,
    cert_path: str | None,
    key_path: str | None,
) -> Any:
    """Build an OpenShell TlsConfig from file paths."""
    import pathlib

    from openshell.sandbox import TlsConfig

    assert ca_path is not None, "ca_path is required for TLS"
    assert cert_path is not None, "cert_path is required for TLS"
    assert key_path is not None, "key_path is required for TLS"
    return TlsConfig(
        ca_path=pathlib.Path(ca_path),
        cert_path=pathlib.Path(cert_path),
        key_path=pathlib.Path(key_path),
    )


def _resolve_openshell_client(options: OpenShellSandboxClientOptions) -> Any:
    """Create an OpenShell SandboxClient from options."""
    SandboxClientCls = _import_openshell_client()
    if options.endpoint:
        tls = (
            _build_tls_config(
                ca_path=options.tls_ca_path,
                cert_path=options.tls_cert_path,
                key_path=options.tls_key_path,
            )
            if options.tls_ca_path
            else None
        )
        return SandboxClientCls(options.endpoint, tls=tls, timeout=options.timeout)
    return SandboxClientCls.from_active_cluster(cluster=options.cluster, timeout=options.timeout)


def _build_openshell_client(state: OpenShellSandboxSessionState) -> Any:
    """Rebuild an OpenShell SandboxClient from persisted state."""
    SandboxClientCls = _import_openshell_client()
    if state.endpoint:
        tls = (
            _build_tls_config(
                ca_path=state.tls_ca_path,
                cert_path=state.tls_cert_path,
                key_path=state.tls_key_path,
            )
            if state.tls_ca_path
            else None
        )
        return SandboxClientCls(state.endpoint, tls=tls, timeout=state.client_timeout)
    return SandboxClientCls.from_active_cluster(cluster=state.cluster, timeout=state.client_timeout)


def _build_sandbox_spec(options: OpenShellSandboxClientOptions) -> Any:
    """Build an openshell_pb2.SandboxSpec from client options."""
    pb2 = _import_openshell_proto()
    template = None
    if options.image:
        template = pb2.SandboxTemplate(image=options.image)
    return pb2.SandboxSpec(
        environment=dict(options.envs or {}),
        template=template,
        gpu=options.gpu,
        providers=list(options.providers or []),
    )


class OpenShellSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by an OpenShell sandbox."""

    state: OpenShellSandboxSessionState

    def __init__(
        self,
        *,
        state: OpenShellSandboxSessionState,
        openshell_client: Any,
    ) -> None:
        self.state = state
        self._openshell_client = openshell_client
        self._workspace_root_ready = state.workspace_root_ready

    # -- internal helpers ------------------------------------------------------

    async def _run_sync(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous function in the default executor."""
        loop = asyncio.get_running_loop()
        bound = functools.partial(fn, *args, **kwargs)
        return await loop.run_in_executor(None, bound)

    async def _resolved_envs(self) -> dict[str, str]:
        """Merge base environment with manifest-declared environment variables."""
        manifest_env = await self.state.manifest.environment.resolve()
        merged: dict[str, str] = {}
        merged.update(self.state.base_envs)
        for key, value in manifest_env.items():
            if value is not None:
                merged[key] = value
        return merged

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        """Validate path against workspace root using local normalization.

        OpenShell rejects command arguments containing newline characters, so the
        remote path resolution helper (which installs a multi-line shell script
        via exec) cannot be used. Local normalization is sufficient because
        OpenShell enforces its own filesystem policy inside the sandbox.
        """
        return self.normalize_path(path, for_write=for_write)

    def _mark_workspace_root_ready_from_probe(self) -> None:
        """Record that the preserved-backend workspace root was proven ready."""
        super()._mark_workspace_root_ready_from_probe()
        self._workspace_root_ready = True

    async def _prepare_backend_workspace(self) -> None:
        """Ensure the workspace root directory exists inside the sandbox."""
        root = PurePosixPath(self.state.manifest.root)
        try:
            result = await self._exec_internal("mkdir", "-p", "--", root.as_posix())
        except Exception as exc:
            raise WorkspaceStartError(path=posix_path_as_path(root), cause=exc) from exc

        if result.exit_code != 0:
            raise WorkspaceStartError(
                path=posix_path_as_path(root),
                context={
                    "exit_code": result.exit_code,
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        self._workspace_root_ready = True

    async def _shutdown_backend(self) -> None:
        """Best-effort delete of the sandbox and close the gRPC channel."""
        try:
            await self._run_sync(self._openshell_client.delete, self.state.sandbox_name)
        except Exception as exc:
            logger.warning("OpenShell sandbox delete failed (non-fatal): %s", exc)
        try:
            await self._run_sync(self._openshell_client.close)
        except Exception as exc:
            logger.warning("OpenShell client close failed (non-fatal): %s", exc)

    # -- exec ------------------------------------------------------------------

    async def _exec_internal(
        self, *command: str | Path, timeout: float | None = None
    ) -> ExecResult:
        """Execute a command inside the OpenShell sandbox."""
        cmd_list = [str(part) for part in command]
        if not cmd_list:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        workdir: str | None = None
        if self._workspace_root_ready:
            workdir = self.state.manifest.root

        envs = await self._resolved_envs()

        exec_kwargs: dict[str, Any] = {
            "workdir": workdir,
            "env": envs or None,
        }
        if timeout is not None:
            exec_kwargs["timeout_seconds"] = int(timeout)

        try:
            result = await self._run_sync(
                self._openshell_client.exec,
                self.state.sandbox_id,
                cmd_list,
                **exec_kwargs,
            )
            # OpenShell ExecResult returns stdout/stderr as str.
            stdout = (
                result.stdout.encode("utf-8") if isinstance(result.stdout, str) else result.stdout
            )
            stderr = (
                result.stderr.encode("utf-8") if isinstance(result.stderr, str) else result.stderr
            )
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=result.exit_code)
        except ExecTransportError:
            raise
        except Exception as exc:
            raise ExecTransportError(
                command=cmd_list,
                context={"backend": "openshell", "sandbox_id": self.state.sandbox_id},
                cause=exc,
            ) from exc

    # -- file I/O --------------------------------------------------------------

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        """Read a file from the sandbox by base64-encoding on the remote side."""
        normalized = await self._validate_path_access(path)
        path_arg = sandbox_path_str(normalized)
        result = await self.exec("base64", "-w0", "--", path_arg, shell=False, user=user)
        if not result.ok():
            raise WorkspaceReadNotFoundError(path=normalized)
        raw = base64.b64decode(result.stdout)
        return io.BytesIO(raw)

    async def write(self, path: Path, data: io.IOBase, *, user: str | User | None = None) -> None:
        """Write a file into the sandbox by piping base64-encoded data."""
        normalized = await self._validate_path_access(path, for_write=True)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            from ....sandbox.errors import WorkspaceWriteTypeError

            raise WorkspaceWriteTypeError(path=normalized, actual_type=type(payload).__name__)

        encoded = base64.b64encode(bytes(payload)).decode("ascii")
        path_arg = sandbox_path_str(normalized)
        # Ensure the parent directory exists.
        parent_cmd = ("mkdir", "-p", "--", str(PurePosixPath(path_arg).parent))
        await self.exec(*parent_cmd, shell=False, user=user)
        # Write the file via printf | base64 -d.
        write_cmd = f"printf '%s' {shlex.quote(encoded)} | base64 -d > {shlex.quote(path_arg)}"
        result = await self.exec("sh", "-c", write_cmd, shell=False, user=user)
        if not result.ok():
            raise WorkspaceArchiveWriteError(
                path=normalized,
                context={
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )

    # -- status ----------------------------------------------------------------

    async def running(self) -> bool:
        """Check whether the sandbox is still running."""
        if not self._workspace_root_ready:
            return False
        try:
            ref = await self._run_sync(self._openshell_client.get, self.state.sandbox_name)
            return bool(ref.phase == _OPENSHELL_PHASE_READY)
        except Exception:
            return False

    # -- workspace persistence -------------------------------------------------

    def _tar_exclude_args(self) -> list[str]:
        """Build tar exclude flags from the skip paths."""
        return shell_tar_exclude_args(self._persist_workspace_skip_relpaths())

    async def persist_workspace(self) -> io.IOBase:
        """Serialize the workspace to a tar archive streamed via base64."""
        root = self._workspace_root_path()
        excludes = " ".join(self._tar_exclude_args())
        tar_cmd = f"tar {excludes} -C {shlex.quote(root.as_posix())} -cf - . | base64 -w0"
        result = await self._exec_internal("sh", "-c", tar_cmd)
        if result.exit_code != 0:
            raise WorkspaceArchiveReadError(
                path=root,
                context={
                    "reason": "tar_failed",
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        raw = base64.b64decode(result.stdout)
        return io.BytesIO(raw)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        """Populate the workspace from a tar archive."""
        root = self._workspace_root_path()
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            from ....sandbox.errors import WorkspaceWriteTypeError

            raise WorkspaceWriteTypeError(path=root, actual_type=type(payload).__name__)

        encoded = base64.b64encode(bytes(payload)).decode("ascii")
        await self.mkdir(root, parents=True)
        untar_cmd = (
            f"printf '%s' {shlex.quote(encoded)} "
            f"| base64 -d "
            f"| tar xf - -C {shlex.quote(root.as_posix())}"
        )
        result = await self._exec_internal("sh", "-c", untar_cmd)
        if result.exit_code != 0:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "untar_failed",
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )


class OpenShellSandboxClient(BaseSandboxClient["OpenShellSandboxClientOptions"]):
    """OpenShell-backed sandbox client."""

    backend_id = "openshell"

    def __init__(
        self,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: OpenShellSandboxClientOptions,
    ) -> SandboxSession:
        manifest = manifest or Manifest()
        os_client = _resolve_openshell_client(options)
        spec = _build_sandbox_spec(options)

        loop = asyncio.get_running_loop()
        sandbox_ref = await loop.run_in_executor(
            None, functools.partial(os_client.create, spec=spec)
        )
        sandbox_ref = await loop.run_in_executor(
            None,
            functools.partial(
                os_client.wait_ready,
                sandbox_ref.name,
                timeout_seconds=options.ready_timeout,
            ),
        )

        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = OpenShellSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            sandbox_id=str(sandbox_ref.id),
            sandbox_name=str(sandbox_ref.name),
            cluster=options.cluster,
            endpoint=options.endpoint,
            tls_ca_path=options.tls_ca_path,
            tls_cert_path=options.tls_cert_path,
            tls_key_path=options.tls_key_path,
            base_envs=dict(options.envs or {}),
            image=options.image,
            gpu=options.gpu,
            providers_list=list(options.providers or []),
            client_timeout=options.timeout,
            ready_timeout=options.ready_timeout,
        )
        inner = OpenShellSandboxSession(state=state, openshell_client=os_client)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, OpenShellSandboxSession):
            raise TypeError("OpenShellSandboxClient.delete expects an OpenShellSandboxSession")
        try:
            await inner._run_sync(inner._openshell_client.delete, inner.state.sandbox_name)
        except Exception as exc:
            logger.warning(
                "Failed to delete OpenShell sandbox.",
                extra={"sandbox_name": inner.state.sandbox_name},
                exc_info=exc,
            )
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, OpenShellSandboxSessionState):
            raise TypeError("OpenShellSandboxClient.resume expects an OpenShellSandboxSessionState")

        os_client = _build_openshell_client(state)
        reconnected = False
        loop = asyncio.get_running_loop()
        try:
            sandbox_ref = await loop.run_in_executor(
                None,
                functools.partial(os_client.get, state.sandbox_name),
            )
            state.sandbox_id = str(sandbox_ref.id)
            reconnected = True
        except Exception:
            sandbox_ref = await loop.run_in_executor(
                None, functools.partial(os_client.create, spec=None)
            )
            sandbox_ref = await loop.run_in_executor(
                None,
                functools.partial(
                    os_client.wait_ready,
                    sandbox_ref.name,
                    timeout_seconds=state.ready_timeout,
                ),
            )
            state.sandbox_id = str(sandbox_ref.id)
            state.sandbox_name = str(sandbox_ref.name)
            state.workspace_root_ready = False

        inner = OpenShellSandboxSession(state=state, openshell_client=os_client)
        inner._set_start_state_preserved(reconnected, system=reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        """Deserialize an OpenShell session state from a JSON-compatible payload."""
        return OpenShellSandboxSessionState.model_validate(payload)


__all__ = [
    "OpenShellSandboxClient",
    "OpenShellSandboxClientOptions",
    "OpenShellSandboxSession",
    "OpenShellSandboxSessionState",
]

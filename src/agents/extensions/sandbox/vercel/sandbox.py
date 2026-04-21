"""
Vercel sandbox (https://vercel.com) implementation.

This module provides a Vercel-backed sandbox client/session implementation backed by
`vercel.sandbox.AsyncSandbox`.

The `vercel` dependency is optional, so package-level exports should guard imports of this
module. Within this module, Vercel SDK imports are normal so users with the extra installed get
full type navigation.
"""

from __future__ import annotations

import asyncio
import io
import json
import posixpath
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter, field_serializer, field_validator
from vercel.sandbox import (
    AsyncSandbox,
    NetworkPolicy,
    Resources,
    SandboxStatus,
    SnapshotSource,
)

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
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tarfile
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

WorkspacePersistenceMode = Literal["tar", "snapshot"]

_WORKSPACE_PERSISTENCE_TAR: WorkspacePersistenceMode = "tar"
_WORKSPACE_PERSISTENCE_SNAPSHOT: WorkspacePersistenceMode = "snapshot"
_VERCEL_SNAPSHOT_MAGIC = b"UC_VERCEL_SNAPSHOT_V1\n"
DEFAULT_VERCEL_WORKSPACE_ROOT = "/vercel/sandbox"
_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)
DEFAULT_VERCEL_SANDBOX_TIMEOUT_MS = 270_000
DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S = 45.0
_NETWORK_POLICY_ADAPTER: TypeAdapter[NetworkPolicy] = TypeAdapter(NetworkPolicy)

_VERCEL_TRANSIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ReadError,
    httpx.NetworkError,
    httpx.ProtocolError,
)


def _is_transient_create_error(exc: BaseException) -> bool:
    if exception_chain_has_status_code(exc, {408, 425, 429, 500, 502, 503, 504}):
        return True

    return exception_chain_contains_type(exc, _VERCEL_TRANSIENT_TRANSPORT_ERRORS)


def _is_transient_write_error(exc: BaseException) -> bool:
    if exception_chain_has_status_code(exc, {408, 425, 429, 500, 502, 503, 504}):
        return True

    return exception_chain_contains_type(exc, _VERCEL_TRANSIENT_TRANSPORT_ERRORS)


@retry_async(retry_if=lambda exc, **_kwargs: _is_transient_create_error(exc))
async def _create_sandbox_with_retry(**kwargs):
    return await AsyncSandbox.create(**kwargs)


def _encode_snapshot_ref(*, snapshot_id: str) -> bytes:
    body = json.dumps({"snapshot_id": snapshot_id}, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return _VERCEL_SNAPSHOT_MAGIC + body


def _decode_snapshot_ref(raw: bytes) -> str | None:
    if not raw.startswith(_VERCEL_SNAPSHOT_MAGIC):
        return None

    body = raw[len(_VERCEL_SNAPSHOT_MAGIC) :]
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None

    snapshot_id = payload.get("snapshot_id")
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


def _resolve_manifest_root(manifest: Manifest | None) -> Manifest:
    if manifest is None:
        return Manifest(root=DEFAULT_VERCEL_WORKSPACE_ROOT)

    if manifest.root == _DEFAULT_MANIFEST_ROOT:
        return manifest.model_copy(update={"root": DEFAULT_VERCEL_WORKSPACE_ROOT})
    return manifest


def _validate_network_policy(value: object) -> NetworkPolicy | None:
    if value is None:
        return None

    return _NETWORK_POLICY_ADAPTER.validate_python(value)


def _serialize_network_policy(value: NetworkPolicy | None) -> object | None:
    if value is None:
        return None

    return cast(object | None, _NETWORK_POLICY_ADAPTER.dump_python(value, mode="json"))


class VercelSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Vercel sandbox backend."""

    type: Literal["vercel"] = "vercel"
    project_id: str | None = None
    team_id: str | None = None
    timeout_ms: int | None = DEFAULT_VERCEL_SANDBOX_TIMEOUT_MS
    runtime: str | None = None
    resources: dict[str, object] | None = None
    env: dict[str, str] | None = None
    exposed_ports: tuple[int, ...] = ()
    interactive: bool = False
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    snapshot_expiration_ms: int | None = None
    network_policy: NetworkPolicy | None = None

    def __init__(
        self,
        project_id: str | None = None,
        team_id: str | None = None,
        timeout_ms: int | None = DEFAULT_VERCEL_SANDBOX_TIMEOUT_MS,
        runtime: str | None = None,
        resources: dict[str, object] | None = None,
        env: dict[str, str] | None = None,
        exposed_ports: tuple[int, ...] = (),
        interactive: bool = False,
        workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR,
        snapshot_expiration_ms: int | None = None,
        network_policy: NetworkPolicy | None = None,
        *,
        type: Literal["vercel"] = "vercel",
    ) -> None:
        super().__init__(
            type=type,
            project_id=project_id,
            team_id=team_id,
            timeout_ms=timeout_ms,
            runtime=runtime,
            resources=resources,
            env=env,
            exposed_ports=exposed_ports,
            interactive=interactive,
            workspace_persistence=workspace_persistence,
            snapshot_expiration_ms=snapshot_expiration_ms,
            network_policy=network_policy,
        )

    @field_validator("network_policy", mode="before")
    @classmethod
    def _coerce_network_policy(cls, value: object) -> NetworkPolicy | None:
        return _validate_network_policy(value)

    @field_serializer("network_policy", when_used="json")
    def _serialize_network_policy_field(self, value: NetworkPolicy | None) -> object | None:
        return _serialize_network_policy(value)


class VercelSandboxSessionState(SandboxSessionState):
    """Serializable state for a Vercel-backed session."""

    type: Literal["vercel"] = "vercel"
    sandbox_id: str
    project_id: str | None = None
    team_id: str | None = None
    timeout_ms: int | None = None
    runtime: str | None = None
    resources: dict[str, object] | None = None
    env: dict[str, str] | None = None
    interactive: bool = False
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    snapshot_expiration_ms: int | None = None
    network_policy: NetworkPolicy | None = None

    @field_validator("network_policy", mode="before")
    @classmethod
    def _coerce_network_policy(cls, value: object) -> NetworkPolicy | None:
        return _validate_network_policy(value)

    @field_serializer("network_policy", when_used="json")
    def _serialize_network_policy_field(self, value: NetworkPolicy | None) -> object | None:
        return _serialize_network_policy(value)


class VercelSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by a Vercel sandbox."""

    state: VercelSandboxSessionState
    _sandbox: Any | None
    _token: str | None

    def __init__(
        self,
        *,
        state: VercelSandboxSessionState,
        sandbox: Any | None = None,
        token: str | None = None,
    ) -> None:
        self.state = state
        self._sandbox = sandbox
        self._token = token

    @classmethod
    def from_state(
        cls,
        state: VercelSandboxSessionState,
        *,
        sandbox: Any | None = None,
        token: str | None = None,
    ) -> VercelSandboxSession:
        return cls(state=state, sandbox=sandbox, token=token)

    def supports_pty(self) -> bool:
        return False

    def _reject_user_arg(self, *, op: Literal["exec", "read", "write"], user: str | User) -> None:
        user_name = user.name if isinstance(user, User) else user
        raise ConfigurationError(
            message=(
                "VercelSandboxSession does not support sandbox-local users; "
                f"`{op}` must be called without `user`"
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op=op,
            context={"backend": "vercel", "user": user_name},
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

    def _validate_tar_bytes(self, raw: bytes) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
                validate_tarfile(tar)
        except UnsafeTarMemberError as exc:
            raise ValueError(str(exc)) from exc
        except (tarfile.TarError, OSError) as exc:
            raise ValueError("invalid tar stream") from exc

    async def _prepare_backend_workspace(self) -> None:
        root = PurePosixPath(posixpath.normpath(self.state.manifest.root))
        try:
            sandbox = await self._ensure_sandbox()
            finished = await sandbox.run_command("mkdir", ["-p", "--", root.as_posix()])
        except Exception as exc:
            raise WorkspaceStartError(path=posix_path_as_path(root), cause=exc) from exc

        if finished.exit_code != 0:
            raise WorkspaceStartError(
                path=posix_path_as_path(root),
                context={
                    "exit_code": finished.exit_code,
                    "stdout": await finished.stdout(),
                    "stderr": await finished.stderr(),
                },
            )

    async def _ensure_sandbox(self, *, source: Any | None = None) -> Any:
        sandbox = self._sandbox
        if sandbox is not None:
            return sandbox

        manifest_env = cast(dict[str, str | None], await self.state.manifest.environment.resolve())
        env = {
            key: value
            for key, value in {**(self.state.env or {}), **manifest_env}.items()
            if value is not None
        }
        sandbox = await _create_sandbox_with_retry(
            source=source,
            ports=list(self.state.exposed_ports) or None,
            timeout=self.state.timeout_ms,
            resources=(
                Resources.model_validate(self.state.resources)
                if self.state.resources is not None
                else None
            ),
            runtime=self.state.runtime,
            token=self._token,
            project_id=self.state.project_id,
            team_id=self.state.team_id,
            interactive=self.state.interactive,
            env=env or None,
            network_policy=self.state.network_policy,
        )
        await sandbox.wait_for_status(
            SandboxStatus.RUNNING,
            timeout=DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S,
        )
        self._sandbox = sandbox
        self.state.sandbox_id = sandbox.sandbox_id
        return sandbox

    async def _close_sandbox_client(self) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            await sandbox.client.aclose()
        except Exception:
            return

    async def _stop_attached_sandbox(self) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            await sandbox.stop()
        except Exception:
            pass
        finally:
            await self._close_sandbox_client()
            self._sandbox = None

    async def _replace_sandbox_from_snapshot(self, snapshot_id: str) -> None:
        await self._stop_attached_sandbox()
        await self._ensure_sandbox(source=SnapshotSource(snapshot_id=snapshot_id))

    async def _restore_snapshot_reference_id(self, snapshot: SnapshotBase) -> str | None:
        if not await snapshot.restorable():
            return None
        restored = await snapshot.restore()
        try:
            raw = restored.read()
        finally:
            try:
                restored.close()
            except Exception:
                pass

        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            return None
        return _decode_snapshot_ref(bytes(raw))

    async def running(self) -> bool:
        sandbox = self._sandbox
        if sandbox is None:
            return False
        try:
            await sandbox.refresh()
        except Exception:
            return False
        return bool(sandbox.status == SandboxStatus.RUNNING)

    async def shutdown(self) -> None:
        await self._stop_attached_sandbox()

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        sandbox = await self._ensure_sandbox()
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        try:
            finished = await asyncio.wait_for(
                sandbox.run_command(
                    normalized[0],
                    normalized[1:],
                    cwd=self.state.manifest.root,
                ),
                timeout=timeout,
            )
            stdout = (await finished.stdout()).encode("utf-8")
            stderr = (await finished.stderr()).encode("utf-8")
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=finished.exit_code)
        except TimeoutError as exc:
            raise ExecTimeoutError(command=normalized, timeout_s=timeout, cause=exc) from exc
        except ExecTimeoutError:
            raise
        except Exception as exc:
            raise ExecTransportError(
                command=normalized,
                context={"backend": "vercel", "sandbox_id": self.state.sandbox_id},
                cause=exc,
            ) from exc

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        sandbox = await self._ensure_sandbox()
        try:
            domain = sandbox.domain(port)
        except Exception as exc:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "vercel", "sandbox_id": self.state.sandbox_id},
                cause=exc,
            ) from exc

        parsed = urlsplit(domain)
        host = parsed.hostname
        if not host:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "vercel", "domain": domain},
            )
        tls = parsed.scheme == "https"
        return ExposedPortEndpoint(
            host=host,
            port=parsed.port or (443 if tls else 80),
            tls=tls,
        )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            self._reject_user_arg(op="read", user=user)

        normalized_path = await self._validate_path_access(path)
        sandbox = await self._ensure_sandbox()
        try:
            payload = await sandbox.read_file(sandbox_path_str(normalized_path))
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc
        if payload is None:
            raise WorkspaceReadNotFoundError(path=normalized_path)
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
            await self._write_files_with_retry(
                [{"path": sandbox_path_str(normalized_path), "content": bytes(payload)}]
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc

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
            root = self._workspace_root_path()
            sandbox = await self._ensure_sandbox()
            try:
                snapshot = await sandbox.snapshot(expiration=self.state.snapshot_expiration_ms)
            except Exception as exc:
                raise WorkspaceArchiveReadError(path=root, cause=exc) from exc
            return io.BytesIO(_encode_snapshot_ref(snapshot_id=snapshot.snapshot_id))

        root = self._workspace_root_path()
        sandbox = await self._ensure_sandbox()
        archive_path = posix_path_as_path(
            coerce_posix_path(f"/tmp/openai-agents-{self.state.session_id.hex}.tar")
        )
        excludes = [
            f"--exclude=./{rel_path.as_posix()}"
            for rel_path in sorted(
                self._persist_workspace_skip_relpaths(),
                key=lambda item: item.as_posix(),
            )
        ]
        tar_command = ("tar", "cf", archive_path.as_posix(), *excludes, ".")
        try:
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_command,
                        context={"backend": "vercel", "sandbox_id": self.state.sandbox_id},
                    ),
                )
            archive = await sandbox.read_file(archive_path.as_posix())
            if archive is None:
                raise WorkspaceReadNotFoundError(path=archive_path)
            return io.BytesIO(archive)
        except WorkspaceReadNotFoundError:
            raise
        except WorkspaceArchiveReadError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=root, cause=exc) from exc
        finally:
            try:
                await sandbox.run_command(
                    "rm", [archive_path.as_posix()], cwd=self.state.manifest.root
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
            lambda: self._hydrate_workspace_internal(bytes(raw)),
            error_path=self._workspace_root_path(),
            error_cls=WorkspaceArchiveWriteError,
            operation_error_context_key="hydrate_error_before_remount_corruption",
        )

    async def _hydrate_workspace_internal(self, raw: bytes) -> None:
        snapshot_id = (
            _decode_snapshot_ref(raw)
            if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT
            else None
        )
        if snapshot_id is not None:
            try:
                await self._replace_sandbox_from_snapshot(snapshot_id)
            except Exception as exc:
                raise WorkspaceArchiveWriteError(
                    path=self._workspace_root_path(),
                    cause=exc,
                ) from exc
            return

        root = self._workspace_root_path()
        sandbox = await self._ensure_sandbox()
        archive_path = posix_path_as_path(
            coerce_posix_path(f"/tmp/openai-agents-{self.state.session_id.hex}.tar")
        )
        tar_command = ("tar", "xf", archive_path.as_posix(), "-C", root.as_posix())
        try:
            self._validate_tar_bytes(raw)
            await self.mkdir(root, parents=True)
            await self._write_files_with_retry([{"path": archive_path.as_posix(), "content": raw}])
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_command,
                        context={"backend": "vercel", "sandbox_id": self.state.sandbox_id},
                    ),
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=root, cause=exc) from exc
        finally:
            try:
                await sandbox.run_command(
                    "rm", [archive_path.as_posix()], cwd=self.state.manifest.root
                )
            except Exception:
                pass

    @retry_async(
        retry_if=lambda exc, self, _files: _is_transient_write_error(exc),
    )
    async def _write_files_with_retry(self, files: list[dict[str, object]]) -> None:
        sandbox = await self._ensure_sandbox()
        await sandbox.write_files(files)


class VercelSandboxClient(BaseSandboxClient[VercelSandboxClientOptions]):
    """Vercel-backed sandbox client."""

    backend_id = "vercel"
    _instrumentation: Instrumentation
    _token: str | None
    _project_id: str | None
    _team_id: str | None

    def __init__(
        self,
        *,
        token: str | None = None,
        project_id: str | None = None,
        team_id: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self._token = token
        self._project_id = project_id
        self._team_id = team_id
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: VercelSandboxClientOptions,
    ) -> SandboxSession:
        resolved_manifest = _resolve_manifest_root(manifest)
        resolved_token = self._token
        resolved_project_id = options.project_id or self._project_id
        resolved_team_id = options.team_id or self._team_id
        if self._project_id is None and resolved_project_id is not None:
            self._project_id = resolved_project_id
        if self._team_id is None and resolved_team_id is not None:
            self._team_id = resolved_team_id
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = VercelSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sandbox_id="",
            project_id=resolved_project_id,
            team_id=resolved_team_id,
            timeout_ms=options.timeout_ms,
            runtime=options.runtime,
            resources=options.resources,
            env=dict(options.env or {}) or None,
            exposed_ports=options.exposed_ports,
            interactive=options.interactive,
            workspace_persistence=options.workspace_persistence,
            snapshot_expiration_ms=options.snapshot_expiration_ms,
            network_policy=options.network_policy,
        )
        inner = VercelSandboxSession.from_state(state, token=resolved_token)
        await inner._ensure_sandbox()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, VercelSandboxSession):
            raise TypeError("VercelSandboxClient.delete expects a VercelSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, VercelSandboxSessionState):
            raise TypeError("VercelSandboxClient.resume expects a VercelSandboxSessionState")

        resolved_token = self._token
        resolved_project_id = state.project_id or self._project_id
        resolved_team_id = state.team_id or self._team_id
        if state.project_id is None:
            state.project_id = resolved_project_id
        if state.team_id is None:
            state.team_id = resolved_team_id

        snapshot_id: str | None = None
        if state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT:
            probe = VercelSandboxSession.from_state(state, token=resolved_token)
            snapshot_id = await probe._restore_snapshot_reference_id(state.snapshot)

        if snapshot_id is not None:
            inner = VercelSandboxSession.from_state(state, token=resolved_token)
            await inner._ensure_sandbox(source=SnapshotSource(snapshot_id=snapshot_id))
            return self._wrap_session(inner, instrumentation=self._instrumentation)

        sandbox = None
        reconnected = False
        if state.sandbox_id:
            try:
                sandbox = await AsyncSandbox.get(
                    sandbox_id=state.sandbox_id,
                    token=resolved_token,
                    project_id=resolved_project_id,
                    team_id=resolved_team_id,
                )
                # XXX(scotttrinh): This will wait even if in a terminal state.
                # We should make wait_for_status smarter about the possible
                # transitions to avoid waiting for a status if it's impossible
                # to transition to it from the current status.
                await sandbox.wait_for_status(
                    SandboxStatus.RUNNING,
                    timeout=DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S,
                )
                reconnected = True
            except TimeoutError:
                if sandbox is not None:
                    await sandbox.client.aclose()
                    sandbox = None
            except Exception:
                sandbox = None

        inner = VercelSandboxSession.from_state(state, sandbox=sandbox, token=resolved_token)
        if sandbox is None:
            state.workspace_root_ready = False
            await inner._ensure_sandbox()
        inner._set_start_state_preserved(reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return VercelSandboxSessionState.model_validate(payload)


__all__ = [
    "VercelSandboxClient",
    "VercelSandboxClientOptions",
    "VercelSandboxSession",
    "VercelSandboxSessionState",
]

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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter, field_serializer, field_validator
from vercel import sandbox as vercel_sandbox

from ....sandbox.entries import BaseEntry, Dir, S3Mount, resolve_workspace_path
from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecNonZeroError,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    MountConfigError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.materialization import MaterializationResult
from ....sandbox.session import SandboxSession, SandboxSessionState, manifest_ops
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

AsyncSandbox = vercel_sandbox.AsyncSandbox
NetworkPolicy = vercel_sandbox.NetworkPolicy
Resources = vercel_sandbox.Resources
SandboxStatus = vercel_sandbox.SandboxStatus
SnapshotSource = vercel_sandbox.SnapshotSource

WorkspacePersistenceMode = Literal["tar", "snapshot"]

_WORKSPACE_PERSISTENCE_TAR: WorkspacePersistenceMode = "tar"
_WORKSPACE_PERSISTENCE_SNAPSHOT: WorkspacePersistenceMode = "snapshot"
_VERCEL_SNAPSHOT_MAGIC = b"UC_VERCEL_SNAPSHOT_V1\n"
_VERCEL_S3_MOUNT_START_SESSION: ContextVar[object | None] = ContextVar(
    "vercel_s3_mount_start_session",
    default=None,
)
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
_VERCEL_RETRYABLE_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    vercel_sandbox.SandboxRateLimitError,
    vercel_sandbox.SandboxServerError,
)
_VERCEL_NON_RETRYABLE_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    vercel_sandbox.SandboxAuthError,
    vercel_sandbox.SandboxNotFoundError,
    vercel_sandbox.SandboxPermissionError,
    vercel_sandbox.SandboxValidationError,
)
_VERCEL_HTTP_STATUS_RETRYABLE: dict[int, bool] = {
    400: False,
    401: False,
    403: False,
    404: False,
    408: True,
    425: True,
    422: False,
    429: True,
    500: True,
    502: True,
    503: True,
    504: True,
}

# Sandbox status values from which the sandbox can still transition to RUNNING.
# Only "pending" qualifies: a freshly created sandbox transitions PENDING -> RUNNING.
# Other non-RUNNING states ("stopping", "stopped", "failed", "aborted",
# "snapshotting") cannot reach RUNNING, so waiting is futile.
_VERCEL_TRANSIENT_SANDBOX_STATUSES: frozenset[str] = frozenset({"pending"})


def _vercel_provider_retryability(exc: BaseException) -> bool | None:
    if exception_chain_contains_type(exc, _VERCEL_RETRYABLE_PROVIDER_ERRORS):
        return True
    if exception_chain_contains_type(exc, _VERCEL_NON_RETRYABLE_PROVIDER_ERRORS):
        return False
    if exception_chain_contains_type(exc, _VERCEL_TRANSIENT_TRANSPORT_ERRORS):
        return True
    for status_code, retryable in _VERCEL_HTTP_STATUS_RETRYABLE.items():
        if exception_chain_has_status_code(exc, {status_code}):
            return retryable
    return None


def _is_transient_create_error(exc: BaseException) -> bool:
    return _vercel_provider_retryability(exc) is True


def _is_transient_write_error(exc: BaseException) -> bool:
    return _vercel_provider_retryability(exc) is True


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


def _vercel_s3_mounts(manifest: Manifest) -> list[S3Mount]:
    mounts: list[S3Mount] = []
    for mount, _mount_path in manifest.mount_targets():
        if mount.mount_strategy.type != "vercel_cloud_bucket":
            continue
        if not isinstance(mount, S3Mount):
            raise MountConfigError(
                message="VercelCloudBucketMountStrategy only supports S3Mount",
                context={"backend": "vercel", "mount_type": mount.type},
            )
        mounts.append(mount)
    return mounts


def _entry_without_vercel_s3_mounts(entry: BaseEntry) -> BaseEntry | None:
    if isinstance(entry, S3Mount) and entry.mount_strategy.type == "vercel_cloud_bucket":
        return None
    if not isinstance(entry, Dir):
        return entry.model_copy(deep=True)

    children: dict[str | Path, BaseEntry] = {}
    for name, child in entry.children.items():
        retained = _entry_without_vercel_s3_mounts(child)
        if retained is not None:
            children[name] = retained
    return entry.model_copy(update={"children": children}, deep=True)


def _manifest_without_vercel_s3_mounts(manifest: Manifest) -> Manifest:
    entries: dict[str | Path, BaseEntry] = {}
    for name, entry in manifest.entries.items():
        retained = _entry_without_vercel_s3_mounts(entry)
        if retained is not None:
            entries[name] = retained
    return manifest.model_copy(update={"entries": entries}, deep=True)


def _vercel_s3_mount_activation_targets(manifest: Manifest) -> list[tuple[S3Mount, Path]]:
    root = posix_path_as_path(coerce_posix_path(manifest.root))
    targets: list[tuple[S3Mount, Path]] = []
    for logical_path, entry in manifest.iter_entries():
        if not isinstance(entry, S3Mount):
            continue
        if entry.mount_strategy.type != "vercel_cloud_bucket":
            continue
        targets.append((entry, resolve_workspace_path(root, logical_path)))
    return targets


def _vercel_s3_mount_map(manifest: Manifest) -> dict[str, S3Mount]:
    _vercel_s3_mounts(manifest)
    targets = manifest.mount_targets()
    root = posixpath.normpath(manifest.root)
    root_path = PurePosixPath(root)
    entry_targets = [
        (
            entry,
            PurePosixPath(posixpath.normpath(posixpath.join(root, logical_path.as_posix()))),
        )
        for logical_path, entry in manifest.iter_entries()
    ]
    mounts: dict[str, S3Mount] = {}
    for index, (mount, mount_path) in enumerate(targets):
        if mount.mount_strategy.type != "vercel_cloud_bucket":
            continue
        assert isinstance(mount, S3Mount)
        path_text = posixpath.normpath(mount_path.as_posix())
        if path_text == root:
            raise MountConfigError(
                message="Vercel does not support mounting an S3 bucket at the workspace root",
                context={"backend": "vercel", "mount_path": path_text},
            )
        path = PurePosixPath(path_text)
        if root_path not in path.parents:
            raise MountConfigError(
                message="Vercel S3 mount paths must stay within the workspace root",
                context={
                    "backend": "vercel",
                    "mount_path": path_text,
                    "workspace_root": root,
                },
            )
        for entry, entry_path in entry_targets:
            if entry is mount or isinstance(entry, Dir) and entry_path in path.parents:
                continue
            if path == entry_path or path in entry_path.parents or entry_path in path.parents:
                raise MountConfigError(
                    message="Vercel S3 mount paths must not overlap manifest entries",
                    context={
                        "backend": "vercel",
                        "mount_path": path_text,
                        "overlapping_entry_path": entry_path.as_posix(),
                    },
                )
        for other_index, (_other_mount, other_path) in enumerate(targets):
            if other_index == index:
                continue
            other = PurePosixPath(posixpath.normpath(other_path.as_posix()))
            if path == other or path in other.parents or other in path.parents:
                raise MountConfigError(
                    message="Vercel S3 mount paths must not overlap other mounts",
                    context={
                        "backend": "vercel",
                        "mount_path": path_text,
                        "overlapping_mount_path": other.as_posix(),
                    },
                )
        mounts[path_text] = mount
    return mounts


def _strip_vercel_mount_inline_credentials(value: object) -> None:
    if isinstance(value, dict):
        mount_strategy = value.get("mount_strategy")
        if (
            value.get("type") == "s3_mount"
            and isinstance(mount_strategy, dict)
            and mount_strategy.get("type") == "vercel_cloud_bucket"
        ):
            value.pop("access_key_id", None)
            value.pop("secret_access_key", None)
            value.pop("session_token", None)
        for nested_value in value.values():
            _strip_vercel_mount_inline_credentials(nested_value)
    elif isinstance(value, list | tuple):
        for nested_value in value:
            _strip_vercel_mount_inline_credentials(nested_value)


def _manifest_without_vercel_s3_credentials(manifest: Manifest) -> Manifest:
    sanitized = manifest.model_copy(deep=True)
    for mount in _vercel_s3_mounts(sanitized):
        mount.access_key_id = None
        mount.secret_access_key = None
        mount.session_token = None
    return sanitized


def _manifest_has_vercel_s3_credentials(manifest: Manifest) -> bool:
    return any(
        credential is not None
        for mount in _vercel_s3_mounts(manifest)
        for credential in (
            mount.access_key_id,
            mount.secret_access_key,
            mount.session_token,
        )
    )


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
    allow_s3_credential_exposure: bool = False

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
        allow_s3_credential_exposure: bool = False,
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
            allow_s3_credential_exposure=allow_s3_credential_exposure,
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
    s3_mounts_non_resumable: bool = False

    @field_serializer("manifest")
    def _serialize_manifest_without_inline_credentials(
        self,
        manifest: Manifest,
    ) -> dict[str, object]:
        payload = cast(
            dict[str, object],
            manifest.model_dump(mode="json", serialize_as_any=True),
        )
        _strip_vercel_mount_inline_credentials(payload)
        return payload

    @field_validator("network_policy", mode="before")
    @classmethod
    def _coerce_network_policy(cls, value: object) -> NetworkPolicy | None:
        return _validate_network_policy(value)

    @field_serializer("network_policy", when_used="json")
    def _serialize_network_policy_field(self, value: NetworkPolicy | None) -> object | None:
        return _serialize_network_policy(value)


class VercelSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by a Vercel sandbox.

    This provider applies the remote mount simplicity boundary by fixing the mount set at creation
    and keeping its trusted configuration only in memory. Keep the lifecycle limited to create,
    tar detach/remount, and close. Do not add dynamic mutation, persisted mount reconstruction,
    credential refresh, or best-effort reconciliation without a trusted provider primitive that
    makes those transitions unambiguous.
    """

    state: VercelSandboxSessionState
    _sandbox: Any | None
    _token: str | None
    _s3_mounts_started: bool
    _active_s3_mount_paths: set[str]
    _detached_s3_mount_paths: set[str]
    _trusted_s3_mounts: dict[str, S3Mount]
    _trusted_s3_mount_credentials: dict[
        str,
        tuple[str | None, str | None, str | None],
    ]
    _trusted_manifest: Manifest
    _s3_mount_session_closed: bool
    _s3_mount_failure: str | None
    _s3_mount_operation_lock: asyncio.Lock
    _s3_mount_operation_owner: asyncio.Task[Any] | None

    def __init__(
        self,
        *,
        state: VercelSandboxSessionState,
        sandbox: Any | None = None,
        token: str | None = None,
        allow_s3_credential_exposure: bool = False,
        trusted_s3_mounts: dict[str, S3Mount] | None = None,
    ) -> None:
        resolved_trusted_s3_mounts: dict[str, S3Mount] = {}
        trusted_s3_mount_credentials: dict[
            str,
            tuple[str | None, str | None, str | None],
        ] = {}
        for path, mount in (trusted_s3_mounts or {}).items():
            trusted_mount = mount.model_copy(deep=True)
            credentials = (
                trusted_mount.access_key_id,
                trusted_mount.secret_access_key,
                trusted_mount.session_token,
            )
            trusted_mount.access_key_id = None
            trusted_mount.secret_access_key = None
            trusted_mount.session_token = None
            resolved_trusted_s3_mounts[path] = trusted_mount
            trusted_s3_mount_credentials[path] = credentials
        has_trusted_credentials = any(
            credential is not None
            for credentials in trusted_s3_mount_credentials.values()
            for credential in credentials
        )
        if has_trusted_credentials and not allow_s3_credential_exposure:
            raise MountConfigError(
                message=(
                    "Vercel S3 mounts expose inline credentials to code running in the sandbox; "
                    "set allow_s3_credential_exposure=True only for credentials scoped to that "
                    "sandbox"
                ),
                context={"backend": "vercel"},
            )
        declared_mount_paths = set(_vercel_s3_mount_map(state.manifest))
        if declared_mount_paths != set(resolved_trusted_s3_mounts):
            raise MountConfigError(
                message=(
                    "Vercel S3 mount topology must match trusted create-time configuration; "
                    "persisted session state cannot reconstruct or change it"
                ),
                context={
                    "backend": "vercel",
                    "declared_mount_paths": sorted(declared_mount_paths),
                    "trusted_mount_paths": sorted(resolved_trusted_s3_mounts),
                },
            )
        self.state = state
        self._sandbox = sandbox
        self._token = token
        self._s3_mounts_started = False
        self._active_s3_mount_paths = set()
        self._detached_s3_mount_paths = set()
        self._trusted_s3_mounts = resolved_trusted_s3_mounts
        self._trusted_s3_mount_credentials = trusted_s3_mount_credentials
        self._trusted_manifest = state.manifest.model_copy(deep=True)
        self._s3_mount_session_closed = False
        self._s3_mount_failure = None
        self._s3_mount_operation_lock = asyncio.Lock()
        self._s3_mount_operation_owner = None

    @classmethod
    def from_state(
        cls,
        state: VercelSandboxSessionState,
        *,
        sandbox: Any | None = None,
        token: str | None = None,
        allow_s3_credential_exposure: bool = False,
        trusted_s3_mounts: dict[str, S3Mount] | None = None,
    ) -> VercelSandboxSession:
        return cls(
            state=state,
            sandbox=sandbox,
            token=token,
            allow_s3_credential_exposure=allow_s3_credential_exposure,
            trusted_s3_mounts=trusted_s3_mounts,
        )

    @staticmethod
    def _s3_mount_path_key(path: Path) -> str:
        return posixpath.normpath(path.as_posix())

    def _runtime_s3_mount_activation_allowed(self) -> bool:
        return _VERCEL_S3_MOUNT_START_SESSION.get() is self

    def _runtime_s3_mount_is_active(self, path: Path) -> bool:
        return self._s3_mount_path_key(path) in self._active_s3_mount_paths

    def _runtime_s3_mount_is_detached(self, path: Path) -> bool:
        return self._s3_mount_path_key(path) in self._detached_s3_mount_paths

    def _runtime_trusted_s3_mount(self, path: Path) -> S3Mount:
        key = self._s3_mount_path_key(path)
        mount = self._trusted_s3_mounts.get(key)
        if mount is None:
            raise MountConfigError(
                message="Vercel S3 mount configuration is unavailable outside sandbox creation",
                context={"backend": "vercel", "mount_path": key},
            )
        return mount

    def _runtime_s3_mount_is_authenticated(self, path: Path) -> bool:
        key = self._s3_mount_path_key(path)
        credentials = self._trusted_s3_mount_credentials.get(key)
        return credentials is not None and credentials[0] is not None

    def _runtime_s3_mount_environment(self, path: Path) -> dict[str, str]:
        key = self._s3_mount_path_key(path)
        credentials = self._trusted_s3_mount_credentials.get(key)
        mount = self._trusted_s3_mounts.get(key)
        if credentials is None or mount is None:
            raise MountConfigError(
                message="Vercel S3 mount configuration is unavailable outside sandbox creation",
                context={"backend": "vercel", "mount_path": key},
            )
        access_key_id, secret_access_key, session_token = credentials
        env: dict[str, str] = {}
        if access_key_id is not None and secret_access_key is not None:
            env["AWS_ACCESS_KEY_ID"] = access_key_id
            env["AWS_SECRET_ACCESS_KEY"] = secret_access_key
        if session_token is not None:
            env["AWS_SESSION_TOKEN"] = session_token
        if mount.region is not None:
            env["AWS_REGION"] = mount.region
        return env

    def _runtime_provider_retryability(self, error: BaseException) -> bool | None:
        return _vercel_provider_retryability(error)

    async def _runtime_fail_s3_mount_transition(self, error: BaseException) -> None:
        self._s3_mount_failure = type(error).__name__
        stop_task = asyncio.create_task(self._stop_attached_sandbox())
        while not stop_task.done():
            try:
                await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                # A cancelled privileged transition has unknown state, so cleanup must finish.
                continue
        await stop_task

    def _runtime_assert_s3_mount_topology(self) -> None:
        topology_changed = (
            self.state.manifest != self._trusted_manifest
            if self._trusted_s3_mounts
            else bool(_vercel_s3_mounts(self.state.manifest))
        )
        if topology_changed:
            raise MountConfigError(
                message="Vercel S3 mount topology cannot change after sandbox creation",
                context={"backend": "vercel"},
            )

    def _runtime_assert_s3_workspace_root(self) -> None:
        if self._trusted_s3_mounts and self.state.manifest.root != self._trusted_manifest.root:
            raise MountConfigError(
                message="Vercel S3 mount topology cannot change after sandbox creation",
                context={"backend": "vercel"},
            )

    @asynccontextmanager
    async def _s3_mount_operation(
        self,
        *,
        force_lock: bool = False,
        validate_topology: bool = True,
    ) -> AsyncIterator[None]:
        if not self._trusted_s3_mounts or (
            self._runtime_s3_mount_activation_allowed()
            and not self._active_s3_mount_paths
            and not self._detached_s3_mount_paths
            and not force_lock
        ):
            if validate_topology and self._trusted_s3_mounts:
                self._runtime_assert_s3_workspace_root()
            yield
            return

        current_task = asyncio.current_task()
        assert current_task is not None
        if self._s3_mount_operation_owner is current_task:
            yield
            return

        async with self._s3_mount_operation_lock:
            if validate_topology:
                self._runtime_assert_s3_workspace_root()
            self._s3_mount_operation_owner = current_task
            try:
                yield
            finally:
                self._s3_mount_operation_owner = None

    def _runtime_record_s3_mount_active(self, path: Path) -> None:
        key = self._s3_mount_path_key(path)
        self._detached_s3_mount_paths.discard(key)
        self._active_s3_mount_paths.add(key)

    def _runtime_record_s3_mount_inactive(self, path: Path) -> None:
        key = self._s3_mount_path_key(path)
        self._active_s3_mount_paths.discard(key)
        self._detached_s3_mount_paths.discard(key)

    def _runtime_record_s3_mount_detached(self, path: Path) -> None:
        key = self._s3_mount_path_key(path)
        self._active_s3_mount_paths.discard(key)
        self._detached_s3_mount_paths.add(key)

    def _runtime_record_s3_mount_restored(self, path: Path) -> None:
        self._runtime_record_s3_mount_active(path)

    async def _start_workspace(self) -> None:
        self._runtime_assert_s3_mount_topology()
        if not _vercel_s3_mounts(self.state.manifest):
            await super()._start_workspace()
            return
        if self._s3_mounts_started:
            raise MountConfigError(
                message=(
                    "Vercel S3 mount topology is fixed when the sandbox is created; "
                    "starting the same mounted session again is not supported"
                ),
                context={"backend": "vercel"},
            )

        activation_token = _VERCEL_S3_MOUNT_START_SESSION.set(self)
        try:
            await super()._start_workspace()
            for mount, destination in _vercel_s3_mount_activation_targets(self._trusted_manifest):
                await mount.mount_strategy.activate(
                    mount,
                    self,
                    destination,
                    self._manifest_base_dir(),
                )
        except (Exception, asyncio.CancelledError) as exc:
            if self._active_s3_mount_paths:
                self._s3_mounts_started = True
                await self._runtime_fail_s3_mount_transition(exc)
            raise
        finally:
            _VERCEL_S3_MOUNT_START_SESSION.reset(activation_token)
        self._s3_mounts_started = True

    async def _apply_manifest(
        self,
        *,
        only_ephemeral: bool = False,
        provision_accounts: bool = True,
    ) -> MaterializationResult:
        if self._runtime_s3_mount_activation_allowed() and self._trusted_s3_mounts:
            return await manifest_ops.apply_manifest(
                self,
                manifest=_manifest_without_vercel_s3_mounts(self._trusted_manifest),
                only_ephemeral=only_ephemeral,
                provision_accounts=provision_accounts,
            )
        return await super()._apply_manifest(
            only_ephemeral=only_ephemeral,
            provision_accounts=provision_accounts,
        )

    async def _validate_manifest_application(self, *, only_ephemeral: bool = False) -> None:
        _ = only_ephemeral
        if not self._runtime_s3_mount_activation_allowed() and (
            self._trusted_s3_mounts or _vercel_s3_mounts(self.state.manifest)
        ):
            raise MountConfigError(
                message=(
                    "Vercel S3 mount topology is fixed when the sandbox is created; "
                    "dynamic manifest application is not supported"
                ),
                context={"backend": "vercel"},
            )

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

    def _validate_tar_bytes(
        self,
        raw: bytes,
        *,
        reject_rel_paths: set[Path] | None = None,
        allow_external_symlink_targets: bool = True,
    ) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
                validate_tarfile(
                    tar,
                    reject_rel_paths=reject_rel_paths or (),
                    allow_external_symlink_targets=allow_external_symlink_targets,
                )
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
            raise WorkspaceStartError(
                path=posix_path_as_path(root),
                cause=exc,
                retryable=_vercel_provider_retryability(exc),
            ) from exc

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
        if self._s3_mount_session_closed:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context={
                    "backend": "vercel",
                    "reason": "mounted_session_closed",
                },
            )
        if self._s3_mount_failure is not None:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context={
                    "backend": "vercel",
                    "reason": "mount_transition_failed",
                    "cause_type": self._s3_mount_failure,
                },
            )
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
        await sandbox.stop(blocking=True)
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
        async with self._s3_mount_operation(validate_topology=False):
            if self._s3_mount_session_closed:
                return
            await self._shutdown_with_s3_mounts()

    async def stop(self) -> None:
        if self._s3_mount_session_closed:
            return
        await super().stop()

    def _should_compute_snapshot_fingerprint_on_persist(self) -> bool:
        return not self._trusted_s3_mounts

    async def _shutdown_with_s3_mounts(self) -> None:
        first_error: Exception | None = None
        if self._s3_mount_failure is None:
            for mount_path_text, mount in self._trusted_s3_mounts.items():
                mount_path = Path(mount_path_text)
                try:
                    await mount.mount_strategy.teardown_for_snapshot(mount, self, mount_path)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        try:
            await self._stop_attached_sandbox()
        except (Exception, asyncio.CancelledError) as exc:
            if self._detached_s3_mount_paths:
                await self._runtime_fail_s3_mount_transition(exc)
            raise
        self._active_s3_mount_paths.clear()
        self._detached_s3_mount_paths.clear()
        if self._trusted_s3_mounts:
            self._s3_mount_session_closed = True
        if first_error is not None:
            raise first_error

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        async with self._s3_mount_operation():
            return await self._exec_internal_with_s3_mounts(*command, timeout=timeout)

    async def _exec_internal_with_s3_mounts(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        sandbox = await self._ensure_sandbox()
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        async def run_and_collect_output() -> ExecResult:
            finished = await sandbox.run_command(
                normalized[0],
                normalized[1:],
                cwd=self.state.manifest.root,
            )
            stdout = (await finished.stdout()).encode("utf-8")
            stderr = (await finished.stderr()).encode("utf-8")
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=finished.exit_code)

        try:
            return await asyncio.wait_for(run_and_collect_output(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ExecTimeoutError(command=normalized, timeout_s=timeout, cause=exc) from exc
        except ExecTimeoutError:
            raise
        except Exception as exc:
            context: dict[str, object] = {
                "backend": "vercel",
                "sandbox_id": self.state.sandbox_id,
            }
            raise ExecTransportError(
                command=normalized,
                context=context,
                cause=exc,
                retryable=_vercel_provider_retryability(exc),
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
                retryable=_vercel_provider_retryability(exc),
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
        async with self._s3_mount_operation():
            return await self._read_with_s3_mounts(path, user=user)

    async def _read_with_s3_mounts(
        self,
        path: Path,
        *,
        user: str | User | None = None,
    ) -> io.IOBase:
        if user is not None:
            self._reject_user_arg(op="read", user=user)

        normalized_path = await self._validate_path_access(path)
        sandbox = await self._ensure_sandbox()
        try:
            payload = await sandbox.read_file(sandbox_path_str(normalized_path))
        except Exception as exc:
            raise WorkspaceArchiveReadError(
                path=normalized_path,
                cause=exc,
                retryable=_vercel_provider_retryability(exc),
            ) from exc
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
        async with self._s3_mount_operation():
            await self._write_with_s3_mounts(path, data, user=user)

    async def _write_with_s3_mounts(
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
            raise WorkspaceArchiveWriteError(
                path=normalized_path,
                cause=exc,
                retryable=_vercel_provider_retryability(exc),
            ) from exc

    async def persist_workspace(self) -> io.IOBase:
        async with self._s3_mount_operation(validate_topology=False):
            self._runtime_assert_s3_mount_topology()
            try:
                return await with_ephemeral_mounts_removed(
                    self,
                    self._persist_workspace_internal,
                    error_path=self._workspace_root_path(),
                    error_cls=WorkspaceArchiveReadError,
                    operation_error_context_key="snapshot_error_before_remount_corruption",
                )
            except (Exception, asyncio.CancelledError) as exc:
                if self._detached_s3_mount_paths:
                    await self._runtime_fail_s3_mount_transition(exc)
                raise

    async def _persist_workspace_internal(self) -> io.IOBase:
        if (
            self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT
            and not self._native_snapshot_requires_tar_fallback()
        ):
            root = self._workspace_root_path()
            sandbox = await self._ensure_sandbox()
            try:
                snapshot = await sandbox.snapshot(expiration=self.state.snapshot_expiration_ms)
            except Exception as exc:
                raise WorkspaceArchiveReadError(
                    path=root,
                    cause=exc,
                    retryable=_vercel_provider_retryability(exc),
                ) from exc
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
        tar_command = ("tar", "cf", archive_path.as_posix(), "--no-wildcards", *excludes, ".")
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
            raise WorkspaceArchiveReadError(
                path=root,
                cause=exc,
                retryable=_vercel_provider_retryability(exc),
            ) from exc
        finally:
            try:
                await sandbox.run_command(
                    "rm", [archive_path.as_posix()], cwd=self.state.manifest.root
                )
            except Exception:
                pass

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        async with self._s3_mount_operation(validate_topology=False):
            self._runtime_assert_s3_mount_topology()
            await self._hydrate_workspace_with_s3_mounts(data)

    async def _hydrate_workspace_with_s3_mounts(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=self._workspace_root_path(),
                actual_type=type(raw).__name__,
            )

        raw_bytes = bytes(raw)
        if self._active_s3_mount_paths:
            if _decode_snapshot_ref(raw_bytes) is not None:
                raise MountConfigError(
                    message="Vercel cannot hydrate a native snapshot while S3 mounts are active",
                    context={"backend": "vercel"},
                )
            try:
                self._validate_tar_bytes(
                    raw_bytes,
                    reject_rel_paths=self._mount_relpaths_within_workspace(),
                    allow_external_symlink_targets=False,
                )
            except Exception as exc:
                raise WorkspaceArchiveWriteError(
                    path=self._workspace_root_path(),
                    cause=exc,
                ) from exc

        try:
            await with_ephemeral_mounts_removed(
                self,
                lambda: self._hydrate_workspace_internal(raw_bytes),
                error_path=self._workspace_root_path(),
                error_cls=WorkspaceArchiveWriteError,
                operation_error_context_key="hydrate_error_before_remount_corruption",
            )
        except (Exception, asyncio.CancelledError) as exc:
            if self._detached_s3_mount_paths:
                await self._runtime_fail_s3_mount_transition(exc)
            raise

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
                    retryable=_vercel_provider_retryability(exc),
                ) from exc
            return

        root = self._workspace_root_path()
        sandbox = await self._ensure_sandbox()
        archive_path = posix_path_as_path(
            coerce_posix_path(f"/tmp/openai-agents-{self.state.session_id.hex}.tar")
        )
        tar_command = ("tar", "xf", archive_path.as_posix(), "-C", root.as_posix())
        try:
            self._validate_tar_bytes(raw, allow_external_symlink_targets=False)
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
            raise WorkspaceArchiveWriteError(
                path=root,
                cause=exc,
                retryable=_vercel_provider_retryability(exc),
            ) from exc
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


class _VercelSandboxSessionWrapper(SandboxSession):
    async def aclose(self) -> None:
        try:
            await super().aclose()
        except BaseException as error:
            inner = cast(VercelSandboxSession, self._inner)
            if inner._trusted_s3_mounts and inner._sandbox is not None:
                try:
                    await inner.shutdown()
                except BaseException as cleanup_error:
                    if isinstance(cleanup_error, asyncio.CancelledError):
                        raise cleanup_error from error
                    raise error from cleanup_error
            raise


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

    def _wrap_session(
        self,
        inner: BaseSandboxSession,
        *,
        instrumentation: Instrumentation | None = None,
    ) -> SandboxSession:
        return _VercelSandboxSessionWrapper(
            inner,
            instrumentation=instrumentation,
            dependencies=self._resolve_dependencies(),
        )

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: VercelSandboxClientOptions,
    ) -> SandboxSession:
        resolved_manifest = _resolve_manifest_root(manifest)
        if (
            _manifest_has_vercel_s3_credentials(resolved_manifest)
            and not options.allow_s3_credential_exposure
        ):
            raise MountConfigError(
                message=(
                    "Vercel S3 mounts expose inline credentials to code running in the sandbox; "
                    "set allow_s3_credential_exposure=True only for credentials scoped to that "
                    "sandbox"
                ),
                context={"backend": "vercel"},
            )
        trusted_s3_mounts = _vercel_s3_mount_map(resolved_manifest)
        for mount in trusted_s3_mounts.values():
            mount.mount_strategy.validate_mount(mount)
        state_manifest = _manifest_without_vercel_s3_credentials(resolved_manifest)
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
            manifest=state_manifest,
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
            s3_mounts_non_resumable=bool(trusted_s3_mounts),
        )
        inner = VercelSandboxSession.from_state(
            state,
            token=resolved_token,
            allow_s3_credential_exposure=options.allow_s3_credential_exposure,
            trusted_s3_mounts=trusted_s3_mounts,
        )
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
        if state.s3_mounts_non_resumable or _vercel_s3_mounts(state.manifest):
            raise MountConfigError(
                message=(
                    "Vercel sessions containing S3 mounts cannot be resumed; "
                    "create a new sandbox with a trusted manifest instead"
                ),
                context={"backend": "vercel"},
            )

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
                current_status = str(sandbox.status)
                if current_status == str(SandboxStatus.RUNNING):
                    # Already running; skip the wait entirely.
                    reconnected = True
                elif current_status in _VERCEL_TRANSIENT_SANDBOX_STATUSES:
                    # Still transitioning toward RUNNING (e.g. PENDING); wait normally.
                    await sandbox.wait_for_status(
                        SandboxStatus.RUNNING,
                        timeout=DEFAULT_VERCEL_WAIT_FOR_RUNNING_TIMEOUT_S,
                    )
                    reconnected = True
                else:
                    # Cannot reach RUNNING from here (STOPPING, STOPPED, FAILED,
                    # ABORTED, SNAPSHOTTING). Drop the handle and recreate below.
                    await sandbox.client.aclose()
                    sandbox = None
            except asyncio.TimeoutError:
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

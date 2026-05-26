"""
Tensorlake sandbox (https://tensorlake.ai) implementation.

Set `TENSORLAKE_API_KEY` (or run `tl login`) to authenticate.

This module provides a Tensorlake-backed sandbox client/session implementation backed by
`tensorlake.sandbox.AsyncSandbox`.

Note: The `tensorlake` dependency is optional (installed via the `tensorlake` extra). The
SDK is imported at module load and importing this module without the extra installed
raises `ImportError` with installation guidance.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import uuid
from contextlib import suppress
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

try:
    from tensorlake.sandbox import (
        AsyncSandbox,
        CheckpointType,
        RemoteAPIError,
        SandboxError,
        SandboxStatus,
    )
except ImportError as exc:  # pragma: no cover - exercised via unit tests with fakes
    raise ImportError(
        "TensorlakeSandboxClient requires the optional `tensorlake` dependency.\n"
        'Install it with `pip install "openai-agents[tensorlake]"`.'
    ) from exc

from ....sandbox.errors import (
    ExecNonZeroError,
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
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import posix_path_for_error, sandbox_path_str

logger = logging.getLogger(__name__)

WorkspacePersistenceMode = Literal["tar", "snapshot"]
CheckpointMode = Literal["filesystem", "memory"]
CheckpointWaitUntil = Literal["local_ready", "completed"]

_WORKSPACE_PERSISTENCE_TAR: WorkspacePersistenceMode = "tar"
_WORKSPACE_PERSISTENCE_SNAPSHOT: WorkspacePersistenceMode = "snapshot"

# Default manifest root for the Tensorlake provider. The default image runs as the
# non-root `tl-user`, so `/workspace` (the cross-provider default) is not writable;
# tmpfs paths like `/tmp/*` are writable but excluded from FILESYSTEM checkpoints.
# `/home/tl-user/workspace` is both `tl-user`-writable and persisted across snapshots.
DEFAULT_TENSORLAKE_WORKSPACE_ROOT = "/home/tl-user/workspace"
_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)
_GENERATED_TENSORLAKE_NAME_PREFIX = "openai-agents-"

# Magic prefix for Tensorlake checkpoint references that are not tar bytes.
_TENSORLAKE_SNAPSHOT_MAGIC = b"TENSORLAKE_SANDBOX_SNAPSHOT_V1\n"

_DEFAULT_EXPOSED_PORT_HOST_TEMPLATE = "{port}-{sandbox}.sandbox.tensorlake.ai"

# Hostnames that indicate a local proxy where port-prefixed subdomain routing does not apply
# (the SDK uses a `Host` header instead).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _unwrap_traced_bytes(payload: Any) -> bytes:
    """Unwrap a `Traced[bytes]` returned by `AsyncSandbox.read_file` into raw bytes.

    The SDK wraps the value on `.value` and exposes a W3C trace id on `.trace_id`; detect
    via `trace_id` so a `Traced[None]` still unwraps correctly.
    """
    if hasattr(payload, "trace_id") and not isinstance(payload, bytes | bytearray):
        payload = payload.value
    if isinstance(payload, bytes | bytearray):
        return bytes(payload)
    return str(payload).encode("utf-8", errors="replace")


def _encode_tensorlake_snapshot_ref(*, snapshot_id: str) -> bytes:
    body = json.dumps({"snapshot_id": snapshot_id}, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return _TENSORLAKE_SNAPSHOT_MAGIC + body


def _decode_tensorlake_snapshot_ref(raw: bytes) -> str | None:
    if not raw.startswith(_TENSORLAKE_SNAPSHOT_MAGIC):
        return None
    body = raw[len(_TENSORLAKE_SNAPSHOT_MAGIC) :]
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    snapshot_id = payload.get("snapshot_id") if isinstance(payload, dict) else None
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


async def _restore_tensorlake_snapshot_reference_id(snapshot: SnapshotBase) -> str | None:
    """Best-effort extraction of the Tensorlake snapshot id from a persisted snapshot.

    Returns ``None`` when the persisted payload is not a Tensorlake checkpoint reference
    or the snapshot store cannot be reached. `client.resume()` runs before session
    dependencies are wired, so e.g. `RemoteSnapshot` would raise; callers fall back to
    the slower `hydrate_workspace` path in those cases.
    """

    try:
        if not await snapshot.restorable():
            return None
        restored = await snapshot.restore()
        try:
            raw = restored.read()
        finally:
            with suppress(Exception):
                restored.close()
    except Exception:
        return None

    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes | bytearray):
        return None
    return _decode_tensorlake_snapshot_ref(bytes(raw))


class TensorlakeSandboxTimeouts(BaseModel):
    """Timeout configuration for Tensorlake operations.

    Attributes:
        exec_timeout_unbounded_s: Safety cap, in seconds, applied to `exec(...)` calls that
            were invoked with `timeout=None`. Prevents an "unbounded" command from holding
            the sandbox indefinitely. Defaults to 24 hours.
        fast_op_s: Per-operation timeout, in seconds, for short backend operations such as
            `mkdir`, `delete_file`, and exposed-port updates. Defaults to 30 seconds.
        snapshot_tar_s: Per-operation timeout, in seconds, for tar-based workspace
            persist and hydrate operations. Defaults to 300 seconds.
    """

    # Caller-supplied timeout=None should mean "no timeout" without bypassing the safety net.
    exec_timeout_unbounded_s: float = Field(default=24 * 60 * 60, ge=1)  # 24 hours
    fast_op_s: float = Field(default=30, ge=1)
    snapshot_tar_s: float = Field(default=300, ge=1)


class TensorlakeSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Tensorlake sandbox backend.

    Attributes:
        image: Optional Tensorlake registered image name. Falls back to the SDK default
            image when None. The default image runs as the non-root `tl-user` account, so
            workspace paths must be writable by that user; see
            [`DEFAULT_TENSORLAKE_WORKSPACE_ROOT`][agents.extensions.sandbox.tensorlake.sandbox.DEFAULT_TENSORLAKE_WORKSPACE_ROOT].
        cpus: Optional CPU allocation for the sandbox. Uses the Tensorlake SDK default
            when None.
        memory_mb: Optional memory allocation for the sandbox, in megabytes.
        disk_mb: Optional disk allocation for the sandbox, in megabytes.
        timeout_secs: Optional sandbox lifetime, in seconds. The Tensorlake backend
            automatically terminates the sandbox after this period.
        name: Optional friendly name for the sandbox; surfaces in Tensorlake dashboards
            and is used as part of the fallback exposed-port hostname. If omitted while
            `pause_on_exit=True`, a stable name is generated because Tensorlake only
            supports suspend/resume for named sandboxes.
        secret_names: Names of Tensorlake-managed secrets to inject into the sandbox
            environment.
        envs: Additional environment variables to inject on every command. Tensorlake
            does not accept envs at sandbox-create time; they are passed on every
            `AsyncSandbox.run(...)` call instead, merged with manifest-supplied envs.
        allow_internet_access: Whether the sandbox is allowed to make outbound internet
            connections. Defaults to True.
        allow_out: Hostname allow-list for outbound traffic. When set, only the listed
            hostnames are reachable from the sandbox.
        deny_out: Hostname deny-list for outbound traffic.
        exposed_ports: TCP ports inside the sandbox to expose through Tensorlake's
            per-sandbox URL.
        allow_unauthenticated_port_access: When True, exposed ports skip Tensorlake's
            built-in auth check. Defaults to False.
        pause_on_exit: When True, run the sandbox as a named Tensorlake sandbox and
            suspend it on shutdown so it can later be resumed via `client.resume(state)`.
            When False (default), the sandbox is terminated on shutdown.
        workspace_persistence: How to persist the workspace between runs. `"tar"`
            (default) captures the manifest root as a tar archive; `"snapshot"` uses
            Tensorlake's native sandbox checkpoint API and stores only a snapshot id.
            Snapshot mode falls back to tar when path-level skips are required.
        checkpoint_mode: For `workspace_persistence="snapshot"`, either `"filesystem"`
            (default; persists across hosts) or `"memory"` (faster, host-local).
        checkpoint_timeout_s: Timeout, in seconds, for a single native checkpoint
            operation. Defaults to 300. Must be strictly less than `timeout_secs`
            when `workspace_persistence="snapshot"`, so the sandbox lives long enough
            for the snapshot to settle before the Tensorlake backend auto-terminates
            it; otherwise the snapshot can be orphaned mid-poll.
        checkpoint_wait_until: How long to wait for the native checkpoint before
            returning a snapshot id. `"local_ready"` (default, matches Tensorlake's SDK
            default) returns as soon as the snapshot is locally resumable — fast and
            sufficient for `AsyncSandbox.create(snapshot_id=...)` restore on the same
            backend. `"completed"` additionally blocks until the snapshot is uploaded
            to durable remote storage; use this only when you need a durable
            `snapshot_uri` (e.g. for cross-host restore after the source host is gone).
        timeouts: Optional `TensorlakeSandboxTimeouts` override (or dict of the same
            shape) controlling fine-grained per-operation timeouts.
        entrypoint: Optional command override for the sandbox image entrypoint.
        startup_timeout: Optional seconds to wait for the sandbox to become ready after
            create.
        proxy_url: Optional override for the Tensorlake sandbox proxy URL (e.g., for
            self-hosted or dev deployments). When set, the exposed-port host is resolved
            from `AsyncSandbox.info().sandbox_url` instead of the public template.
        api_url: Optional override for the Tensorlake control-plane API URL.
        namespace: Optional Tensorlake namespace selector.
        organization_id: Optional Tensorlake organization id.
        project_id: Optional Tensorlake project id.
        routing_hint: Optional routing hint passed to `AsyncSandbox.connect(...)` when
            resuming an existing sandbox. Not used at create time.
    """

    type: Literal["tensorlake"] = "tensorlake"
    image: str | None = None
    cpus: float | None = None
    memory_mb: int | None = None
    timeout_secs: int | None = None
    name: str | None = None
    secret_names: tuple[str, ...] = ()
    envs: dict[str, str] | None = None
    allow_internet_access: bool = True
    allow_out: tuple[str, ...] = ()
    deny_out: tuple[str, ...] = ()
    exposed_ports: tuple[int, ...] = ()
    allow_unauthenticated_port_access: bool = False
    pause_on_exit: bool = False
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    checkpoint_mode: CheckpointMode = "filesystem"
    checkpoint_timeout_s: float = 300.0
    timeouts: TensorlakeSandboxTimeouts | dict[str, object] | None = None
    disk_mb: int | None = None
    entrypoint: tuple[str, ...] = ()
    startup_timeout: float | None = None
    proxy_url: str | None = None
    api_url: str | None = None
    namespace: str | None = None
    organization_id: str | None = None
    project_id: str | None = None
    routing_hint: str | None = None
    checkpoint_wait_until: CheckpointWaitUntil = "local_ready"

    def __init__(
        self,
        image: str | None = None,
        cpus: float | None = None,
        memory_mb: int | None = None,
        timeout_secs: int | None = None,
        name: str | None = None,
        secret_names: tuple[str, ...] = (),
        envs: dict[str, str] | None = None,
        allow_internet_access: bool = True,
        allow_out: tuple[str, ...] = (),
        deny_out: tuple[str, ...] = (),
        exposed_ports: tuple[int, ...] = (),
        allow_unauthenticated_port_access: bool = False,
        pause_on_exit: bool = False,
        workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR,
        checkpoint_mode: CheckpointMode = "filesystem",
        checkpoint_timeout_s: float = 300.0,
        timeouts: TensorlakeSandboxTimeouts | dict[str, object] | None = None,
        disk_mb: int | None = None,
        entrypoint: tuple[str, ...] = (),
        startup_timeout: float | None = None,
        proxy_url: str | None = None,
        api_url: str | None = None,
        namespace: str | None = None,
        organization_id: str | None = None,
        project_id: str | None = None,
        routing_hint: str | None = None,
        checkpoint_wait_until: CheckpointWaitUntil = "local_ready",
        *,
        type: Literal["tensorlake"] = "tensorlake",
    ) -> None:
        super().__init__(
            type=type,
            image=image,
            cpus=cpus,
            memory_mb=memory_mb,
            timeout_secs=timeout_secs,
            name=name,
            secret_names=secret_names,
            envs=envs,
            allow_internet_access=allow_internet_access,
            allow_out=allow_out,
            deny_out=deny_out,
            exposed_ports=exposed_ports,
            allow_unauthenticated_port_access=allow_unauthenticated_port_access,
            pause_on_exit=pause_on_exit,
            workspace_persistence=workspace_persistence,
            checkpoint_mode=checkpoint_mode,
            checkpoint_timeout_s=checkpoint_timeout_s,
            timeouts=timeouts,
            disk_mb=disk_mb,
            entrypoint=entrypoint,
            startup_timeout=startup_timeout,
            proxy_url=proxy_url,
            api_url=api_url,
            namespace=namespace,
            organization_id=organization_id,
            project_id=project_id,
            routing_hint=routing_hint,
            checkpoint_wait_until=checkpoint_wait_until,
        )


class TensorlakeSandboxSessionState(SandboxSessionState):
    """Serializable state for a Tensorlake-backed session.

    Captured at `create(...)` time and round-trippable via
    `client.serialize_session_state` / `client.deserialize_session_state`. Mirrors the
    knobs from
    [`TensorlakeSandboxClientOptions`][agents.extensions.sandbox.tensorlake.sandbox.TensorlakeSandboxClientOptions]
    needed to reconnect to the same sandbox (via `AsyncSandbox.connect`) or, if it has
    expired, to recreate it from a stored snapshot id or hydrate it from a tar archive.

    Attributes:
        sandbox_id: The Tensorlake-assigned identifier of the underlying sandbox.
        base_envs: Caller-supplied environment variables to merge with manifest envs on
            every command.
    """

    type: Literal["tensorlake"] = "tensorlake"
    sandbox_id: str
    name: str | None = None
    image: str | None = None
    cpus: float | None = None
    memory_mb: int | None = None
    timeout_secs: int | None = None
    secret_names: tuple[str, ...] = ()
    base_envs: dict[str, str] = Field(default_factory=dict)
    allow_internet_access: bool = True
    allow_out: tuple[str, ...] = ()
    deny_out: tuple[str, ...] = ()
    allow_unauthenticated_port_access: bool = False
    pause_on_exit: bool = False
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    checkpoint_mode: CheckpointMode = "filesystem"
    checkpoint_timeout_s: float = 300.0
    timeouts: TensorlakeSandboxTimeouts = Field(default_factory=TensorlakeSandboxTimeouts)
    disk_mb: int | None = None
    entrypoint: tuple[str, ...] = ()
    startup_timeout: float | None = None
    proxy_url: str | None = None
    api_url: str | None = None
    namespace: str | None = None
    organization_id: str | None = None
    project_id: str | None = None
    routing_hint: str | None = None
    checkpoint_wait_until: CheckpointWaitUntil = "local_ready"

    @classmethod
    def from_options(
        cls,
        options: TensorlakeSandboxClientOptions,
        *,
        session_id: uuid.UUID,
        manifest: Manifest,
        snapshot: SnapshotBase,
        sandbox_id: str,
        name: str | None,
        timeouts: TensorlakeSandboxTimeouts,
    ) -> TensorlakeSandboxSessionState:
        """Build a session state from the create-time options plus derived values.

        Carry-over fields are pulled by name from `options`; the explicit keyword
        arguments override fields that have no `options` counterpart (`session_id`,
        `manifest`, `snapshot`, `sandbox_id`, `base_envs`) or that the caller derives
        separately (`name`, `timeouts`).
        """
        # `name` is resolved by `_resolve_lifecycle_sandbox_name`, and `timeouts` is
        # the validated `TensorlakeSandboxTimeouts` (options.timeouts may be a dict).
        # `envs` is renamed to `base_envs` and copied so the state owns the dict.
        overrides = {"type", "name", "timeouts"}
        carry = {
            f: getattr(options, f)
            for f in cls.model_fields
            if f in type(options).model_fields and f not in overrides
        }
        return cls(
            **carry,
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot,
            sandbox_id=sandbox_id,
            name=name,
            base_envs=dict(options.envs or {}),
            timeouts=timeouts,
        )


def _resolve_lifecycle_sandbox_name(
    *,
    name: str | None,
    pause_on_exit: bool,
    session_id: uuid.UUID,
) -> str | None:
    if name is not None and name.strip():
        return name
    if pause_on_exit:
        return f"{_GENERATED_TENSORLAKE_NAME_PREFIX}{session_id.hex}"
    return name


# Scalar create-kwargs are emitted when their value is not None; tuple create-kwargs are
# emitted as a list when non-empty. `routing_hint` is accepted by `AsyncSandbox.connect`
# but not `AsyncSandbox.create`.
_CREATE_SCALAR_FIELDS: tuple[str, ...] = (
    "image",
    "cpus",
    "memory_mb",
    "disk_mb",
    "timeout_secs",
    "name",
    "startup_timeout",
    "proxy_url",
    "api_url",
    "namespace",
    "organization_id",
    "project_id",
)
_CREATE_LIST_FIELDS: tuple[str, ...] = (
    "secret_names",
    "allow_out",
    "deny_out",
    "entrypoint",
)
_CONNECT_FIELDS: tuple[str, ...] = (
    "proxy_url",
    "api_url",
    "namespace",
    "organization_id",
    "project_id",
    "routing_hint",
)


@dataclass(frozen=True, kw_only=True, slots=True)
class _TensorlakeLifecycleConfig:
    """Normalized lifecycle config shared by `AsyncSandbox.create` and `connect`.

    Private to this module. Built once from either `TensorlakeSandboxClientOptions`
    (at create time) or `TensorlakeSandboxSessionState` (on resume/restore) so the
    kwargs derivation lives in one place — adding a new Tensorlake option then
    becomes a single change here plus the public option/state classes.
    """

    image: str | None = None
    cpus: float | None = None
    memory_mb: int | None = None
    disk_mb: int | None = None
    timeout_secs: int | None = None
    name: str | None = None
    secret_names: tuple[str, ...] = ()
    allow_internet_access: bool = True
    allow_out: tuple[str, ...] = ()
    deny_out: tuple[str, ...] = ()
    entrypoint: tuple[str, ...] = ()
    startup_timeout: float | None = None
    proxy_url: str | None = None
    api_url: str | None = None
    namespace: str | None = None
    organization_id: str | None = None
    project_id: str | None = None
    routing_hint: str | None = None

    @classmethod
    def from_options(
        cls,
        options: TensorlakeSandboxClientOptions,
        *,
        name: str | None,
    ) -> _TensorlakeLifecycleConfig:
        # `name` is the *resolved* sandbox name from `_resolve_lifecycle_sandbox_name`,
        # which differs from the raw `options.name` (e.g. when `pause_on_exit` forces a
        # generated name), so override that single field after pulling the rest.
        attrs = {f.name: getattr(options, f.name) for f in fields(cls)}
        attrs["name"] = name
        return cls(**attrs)

    @classmethod
    def from_state(
        cls,
        state: TensorlakeSandboxSessionState,
    ) -> _TensorlakeLifecycleConfig:
        return cls(**{f.name: getattr(state, f.name) for f in fields(cls)})


# Tensorlake memory snapshots restore image, resources, entrypoint, and secrets from
# the snapshot itself; passing them at restore time is rejected by the backend. The
# docs say: "Image, resources (CPUs, memory), entrypoint, and secrets come from the
# snapshot and cannot be changed at restore time."
# See https://docs.tensorlake.ai/sandboxes/snapshots.
_MEMORY_SNAPSHOT_RESTORE_EXCLUDED_SCALARS: frozenset[str] = frozenset(
    {"image", "cpus", "memory_mb", "disk_mb"}
)
_MEMORY_SNAPSHOT_RESTORE_EXCLUDED_LISTS: frozenset[str] = frozenset({"entrypoint", "secret_names"})


def _create_kwargs(
    cfg: _TensorlakeLifecycleConfig,
    *,
    snapshot_id: str | None = None,
    memory_snapshot: bool = False,
) -> dict[str, object]:
    """Derive the kwargs accepted by `AsyncSandbox.create(...)` from a lifecycle config.

    Only includes optional fields when they are set so the SDK can apply its own defaults.
    Tensorlake does not accept environment variables at sandbox-create time; envs are passed
    on each `sandbox.run(...)` call instead.

    When restoring from a memory snapshot, image/resources/entrypoint/secrets must be
    omitted because the backend always sources them from the snapshot.
    """

    excluded_scalars: frozenset[str] = frozenset()
    excluded_lists: frozenset[str] = frozenset()
    if snapshot_id is not None and memory_snapshot:
        excluded_scalars = _MEMORY_SNAPSHOT_RESTORE_EXCLUDED_SCALARS
        excluded_lists = _MEMORY_SNAPSHOT_RESTORE_EXCLUDED_LISTS

    kwargs: dict[str, object] = {"allow_internet_access": cfg.allow_internet_access}
    for name in _CREATE_SCALAR_FIELDS:
        if name in excluded_scalars:
            continue
        value = getattr(cfg, name)
        if value is not None:
            kwargs[name] = value
    for name in _CREATE_LIST_FIELDS:
        if name in excluded_lists:
            continue
        value = getattr(cfg, name)
        if value:
            kwargs[name] = list(value)
    if snapshot_id is not None:
        kwargs["snapshot_id"] = snapshot_id
    return kwargs


def _connect_kwargs(cfg: _TensorlakeLifecycleConfig) -> dict[str, object]:
    """Derive the kwargs accepted by `AsyncSandbox.connect(sandbox_id, ...)`."""

    return {name: value for name in _CONNECT_FIELDS if (value := getattr(cfg, name)) is not None}


async def _resolve_sandbox_id(sandbox: Any) -> str | None:
    """Return sandbox_id, seeding the SDK's `info()` cache when necessary.

    `AsyncSandbox.sandbox_id` is a `@property` that raises `SandboxError` until the id
    has been populated (e.g. immediately after `AsyncSandbox.create(snapshot_id=...)`);
    awaiting `info()` fills `_cached_info` and then `_sandbox_id` so the second read
    succeeds. Seeding the cache here also lets subsequent `update()` calls route via
    the stable identifier instead of the create-time bootstrap one.
    """

    for attempt in range(2):
        try:
            value = sandbox.sandbox_id
        except SandboxError:
            value = None
        if isinstance(value, str) and value:
            return value
        if attempt == 0:
            with suppress(SandboxError):
                await sandbox.info()
    return None


class TensorlakeSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by a Tensorlake sandbox."""

    state: TensorlakeSandboxSessionState
    _sandbox: Any
    _envs_cache: dict[str, str] | None
    _cached_proxy_hostname: str | None
    _proxy_hostname_resolved: bool
    _backend_lifecycle_finalized: bool

    def __init__(
        self,
        *,
        state: TensorlakeSandboxSessionState,
        sandbox: Any,
    ) -> None:
        self.state = state
        self._sandbox = sandbox
        self._envs_cache = None
        self._cached_proxy_hostname = None
        self._proxy_hostname_resolved = False
        self._backend_lifecycle_finalized = False

    @classmethod
    def from_state(
        cls,
        state: TensorlakeSandboxSessionState,
        *,
        sandbox: Any,
    ) -> TensorlakeSandboxSession:
        return cls(state=state, sandbox=sandbox)

    @property
    def sandbox_id(self) -> str:
        return self.state.sandbox_id

    def supports_pty(self) -> bool:
        # WebSocket PTY API not yet wired through this integration.
        return False

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.sandbox_id

    async def _resolved_envs(self) -> dict[str, str]:
        # The manifest is treated as immutable for the lifetime of a session, so we resolve
        # secret-store/env values once and reuse the merged dict across exec/file operations.
        if self._envs_cache is None:
            manifest_envs = await self.state.manifest.environment.resolve()
            self._envs_cache = {**self.state.base_envs, **manifest_envs}
        return self._envs_cache

    def _coerce_exec_timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return float(self.state.timeouts.exec_timeout_unbounded_s)
        if timeout_s <= 0:
            # The SDK's `timeout` is an int seconds value; the call site clamps to a 1s
            # floor via `max(1, math.ceil(...))`. Return 1.0 here (matching E2B) instead
            # of a sub-second sentinel so the intent is obvious at the source.
            return 1.0
        return float(timeout_s)

    async def _exec_checked_nonzero(self, *command: str | Path) -> ExecResult:
        """Run a privileged metadata command as ``root`` via the ``sudo`` wrap.

        Manifest account provisioning (`groupadd`/`useradd`/`usermod`), entry
        metadata (`chgrp`/`chmod`), and mount-pattern config writes all funnel
        through this hook. The default Tensorlake image's process user is the
        non-root `tl-user`, which lacks permission for these operations. The
        Tensorlake SDK does not expose a process-user knob on `run()`, so route
        these commands through ``exec(user="root")`` to pick up the base
        session's `sudo -u root --` wrap (the established cross-user pattern
        used by `mkdir(..., user=...)` and verified in the smoke tests).
        """
        result = await self.exec(*command, shell=False, user="root")
        if not result.ok():
            raise ExecNonZeroError(result, command=command)
        return result

    async def _run_mkdir(
        self,
        argv: list[str],
        *,
        user: str | User | None = None,
    ) -> Any:
        """Run `mkdir argv` via the SDK with resolved envs and the fast-op timeout.

        When `user` is provided, wrap with `sudo -u <name> --` so the created
        directory is owned by that sandbox-local user — matching the `sudo`-based
        user switching that `_prepare_exec_command` uses for the `exec` path.
        Caller is responsible for wrapping SDK exceptions with its own error type
        and inspecting `exit_code` on the returned result.
        """
        envs = await self._resolved_envs()
        if user is None:
            command = "mkdir"
            args = argv
        else:
            user_name = user.name if isinstance(user, User) else user
            command = "sudo"
            args = ["-u", user_name, "--", "mkdir", *argv]
        return await self._sandbox.run(
            command,
            args,
            env=envs or None,
            timeout=int(self.state.timeouts.fast_op_s),
        )

    async def _prepare_backend_workspace(self) -> None:
        # Skip the mkdir round-trip when the base start flow probed a reconnected
        # sandbox and confirmed the workspace root already exists.
        if self._workspace_state_preserved_on_start() and self._start_workspace_root_ready:
            return
        root = sandbox_path_str(self.state.manifest.root)
        try:
            result = await self._run_mkdir(["-p", "--", root])
        except Exception as exc:
            raise WorkspaceStartError(path=Path(root), cause=exc) from exc

        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            raise WorkspaceStartError(
                path=Path(root),
                context={
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": exit_code,
                    "stderr": str(getattr(result, "stderr", "") or ""),
                },
            )

    async def _after_start(self) -> None:
        # Checkpoint restore replaces the sandbox and sandbox_id; reinstall runtime helpers only
        # when the cache now points at a different backend.
        if self._runtime_helper_cache_key != self._current_runtime_helper_cache_key():
            await self._ensure_runtime_helpers()

    def _close_sandbox_handle(self) -> None:
        """Close the SDK's local Rust client and drop the reference.

        Use after a path that did NOT already close the handle for us — primarily the
        suspend path, since `AsyncSandbox.suspend()` does not close the local Rust client
        (only `terminate()` does). After a successful `terminate()` the SDK has already
        closed; just set `self._sandbox = None` directly instead of calling this helper,
        to avoid a redundant close on the Rust binding.
        """
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            sandbox.close()
        except Exception:
            pass
        self._sandbox = None

    async def _shutdown_backend(self) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            if self.state.pause_on_exit:
                await sandbox.suspend()
                self._backend_lifecycle_finalized = True
                # `suspend()` does not close the local Rust client; release it
                # explicitly so the connection pool does not leak.
                self._close_sandbox_handle()
            else:
                await sandbox.terminate()
                self._backend_lifecycle_finalized = True
                # `terminate()` already closed the local Rust client; just drop the
                # reference to avoid a redundant close.
                self._sandbox = None
        except Exception as exc:
            if self.state.pause_on_exit:
                logger.warning(
                    "Failed to suspend Tensorlake sandbox on shutdown; falling back to terminate.",
                    extra={"sandbox_id": self.state.sandbox_id},
                    exc_info=exc,
                )
                try:
                    await sandbox.terminate()
                    self._backend_lifecycle_finalized = True
                    self._sandbox = None
                except Exception as term_exc:
                    logger.warning(
                        "Failed to terminate Tensorlake sandbox after suspend fallback failure.",
                        extra={"sandbox_id": self.state.sandbox_id},
                        exc_info=term_exc,
                    )
                    # Leave `self._sandbox` attached so `client.delete()` can retry.
            else:
                logger.warning(
                    "Failed to terminate Tensorlake sandbox on shutdown.",
                    extra={"sandbox_id": self.state.sandbox_id},
                    exc_info=exc,
                )
                # Leave `self._sandbox` attached so `client.delete()` can retry.

    async def running(self) -> bool:
        if not self.state.workspace_root_ready:
            return False
        sandbox = self._sandbox
        if sandbox is None:
            return False
        try:
            status = await sandbox.status()
        except Exception:
            return False
        return bool(status == SandboxStatus.RUNNING)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        envs = await self._resolved_envs()
        cwd = sandbox_path_str(self.state.manifest.root)
        exec_timeout = self._coerce_exec_timeout(timeout)

        try:
            # Rely on the SDK's own `timeout` so the backend tears down the running
            # process; an outer `asyncio.wait_for` only cancels the local awaiter and
            # would leave the sandbox-side command running until the next tick.
            result = await self._sandbox.run(
                normalized[0],
                normalized[1:],
                env=envs or None,
                working_dir=cwd,
                timeout=max(1, math.ceil(exec_timeout)),
            )
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower():
                raise ExecTimeoutError(command=command, timeout_s=timeout, cause=exc) from exc
            raise ExecTransportError(
                command=command,
                context={"backend": "tensorlake", "sandbox_id": self.state.sandbox_id},
                cause=exc,
            ) from exc

        stdout_str = str(getattr(result, "stdout", "") or "")
        stderr_str = str(getattr(result, "stderr", "") or "")
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        return ExecResult(
            stdout=stdout_str.encode("utf-8", errors="replace"),
            stderr=stderr_str.encode("utf-8", errors="replace"),
            exit_code=exit_code,
        )

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        # Prefer the backend's per-sandbox URL so non-default `TENSORLAKE_SANDBOX_PROXY_URL`
        # deployments (e.g. tensorlake.dev) resolve correctly; fall back to the public template.
        proxy_hostname = await self._get_proxy_hostname()
        if proxy_hostname:
            host = f"{port}-{proxy_hostname}"
        else:
            host = _DEFAULT_EXPOSED_PORT_HOST_TEMPLATE.format(
                port=port, sandbox=self.state.name or self.state.sandbox_id
            )
        return ExposedPortEndpoint(host=host, port=443, tls=True)

    async def _get_proxy_hostname(self) -> str | None:
        if self._proxy_hostname_resolved:
            return self._cached_proxy_hostname
        custom_control_plane = self.state.proxy_url is not None or self.state.api_url is not None
        try:
            info = await self._sandbox.info()
        except Exception:
            info = None
        sandbox_url = getattr(info, "sandbox_url", None) if info is not None else None
        if custom_control_plane and not sandbox_url:
            # Some Tensorlake create paths cache minimal info without `sandbox_url`;
            # `status()` performs a fresh lifecycle read and refreshes the SDK cache.
            with suppress(Exception):
                await self._sandbox.status()
                info = await self._sandbox.info()
                sandbox_url = getattr(info, "sandbox_url", None) if info is not None else None
        hostname: str | None = None
        if isinstance(sandbox_url, str) and sandbox_url:
            parsed = urlsplit(sandbox_url).hostname
            if parsed and parsed not in _LOOPBACK_HOSTS:
                hostname = parsed
        self._cached_proxy_hostname = hostname
        # For custom control planes, an unresolved hostname is almost certainly a
        # transient info()/status() failure rather than a steady-state answer (the
        # public template fallback cannot route to a custom deployment). Leave the
        # cache "unresolved" so a later call can retry instead of permanently
        # returning the wrong fallback for the rest of the session.
        self._proxy_hostname_resolved = hostname is not None or not custom_control_plane
        if hostname is None and custom_control_plane:
            logger.warning(
                "Could not resolve Tensorlake sandbox URL from info(); falling back to the "
                "public exposed-port template, which will not route correctly for this "
                "custom proxy_url/api_url deployment. Will retry on the next lookup.",
                extra={
                    "sandbox_id": self.state.sandbox_id,
                    "proxy_url": self.state.proxy_url,
                    "api_url": self.state.api_url,
                },
            )
        return hostname

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            await self._check_read_with_exec(path, user=user)

        normalized_path = await self._validate_path_access(path)

        try:
            payload = await self._sandbox.read_file(sandbox_path_str(normalized_path))
        except FileNotFoundError as exc:
            raise WorkspaceReadNotFoundError(path=normalized_path, cause=exc) from exc
        except Exception as exc:
            if isinstance(exc, RemoteAPIError) and getattr(exc, "status_code", None) == 404:
                raise WorkspaceReadNotFoundError(path=normalized_path, cause=exc) from exc
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc

        return io.BytesIO(_unwrap_traced_bytes(payload))

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            await self._check_write_with_exec(path, user=user)

        normalized_path = await self._validate_path_access(path, for_write=True)

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=normalized_path, actual_type=type(payload).__name__)

        try:
            await self._sandbox.write_file(sandbox_path_str(normalized_path), bytes(payload))
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc

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

        flag = "-p" if parents else ""
        argv = [a for a in [flag, "--", sandbox_path_str(path)] if a]
        try:
            result = await self._run_mkdir(argv, user=user)
        except Exception as exc:
            raise WorkspaceArchiveWriteError(
                path=path, context={"reason": "mkdir_failed"}, cause=exc
            ) from exc

        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            raise WorkspaceArchiveWriteError(
                path=path,
                context={
                    "reason": "mkdir_nonzero_exit",
                    "exit_code": exit_code,
                    "stderr": str(getattr(result, "stderr", "") or ""),
                },
            )

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
            return await self._persist_workspace_via_checkpoint()
        return await self._persist_workspace_via_tar()

    async def _persist_workspace_via_checkpoint(self) -> io.IOBase:
        """Persist using Tensorlake's native sandbox checkpoint API.

        Falls back to tar when the backend declines or when path-level skips are required —
        Tensorlake checkpoints capture the whole sandbox and have no path-level excludes.
        """

        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)

        if self._native_snapshot_requires_tar_fallback():
            return await self._persist_workspace_via_tar()

        skip = self._persist_workspace_skip_relpaths()
        mount_targets = self.state.manifest.ephemeral_mount_targets()
        mount_skip_rel_paths: set[Path] = set()
        for _, mount_path in mount_targets:
            try:
                mount_skip_rel_paths.add(mount_path.relative_to(root))
            except ValueError:
                continue
        if skip - mount_skip_rel_paths:
            return await self._persist_workspace_via_tar()

        checkpoint_type = (
            CheckpointType.MEMORY
            if self.state.checkpoint_mode == "memory"
            else CheckpointType.FILESYSTEM
        )

        # Rely on the SDK's own `timeout` so the backend tears down the operation;
        # an outer `asyncio.wait_for` would only cancel the local awaiter. The
        # `wait_until` knob defaults to `"local_ready"` (Tensorlake SDK default) — that
        # is sufficient for `AsyncSandbox.create(snapshot_id=...)` restore and avoids
        # blocking on remote-storage upload. Set `checkpoint_wait_until="completed"`
        # only when a durable `snapshot_uri` is required.
        try:
            snapshot = await self._sandbox.checkpoint(
                checkpoint_type=checkpoint_type,
                timeout=int(self.state.checkpoint_timeout_s),
                wait_until=self.state.checkpoint_wait_until,
            )
        except Exception as exc:
            raise WorkspaceArchiveReadError(
                path=error_root,
                context={"reason": "tensorlake_checkpoint_failed"},
                cause=exc,
            ) from exc

        snapshot_id = getattr(snapshot, "snapshot_id", None)
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise WorkspaceArchiveReadError(
                path=error_root,
                context={
                    "reason": "tensorlake_checkpoint_unexpected_return",
                    "type": type(snapshot).__name__,
                },
            )
        return io.BytesIO(_encode_tensorlake_snapshot_ref(snapshot_id=snapshot_id))

    async def _persist_workspace_via_tar(self) -> io.IOBase:
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        archive_path = f"/tmp/openai-agents-{self.state.session_id.hex}.tar"
        skip = list(self._persist_workspace_skip_relpaths())
        # When the workspace root is /tmp (or /) the archive file falls inside the tree being
        # archived; exclude it to prevent tar's "file is the archive" error.
        try:
            skip.append(Path(archive_path).relative_to(root))
        except ValueError:
            pass  # archive is outside the workspace root
        excludes = shell_tar_exclude_args(skip)
        tar_argv = ["cf", archive_path, *excludes, "-C", root.as_posix(), "."]

        try:
            archive_bytes = await self._run_persist_workspace_command(tar_argv, archive_path)
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=error_root, cause=exc) from exc
        finally:
            await self._remove_tmp_archive(archive_path)

        return io.BytesIO(archive_bytes)

    @retry_async(
        retry_if=lambda exc, *_args, **_kwargs: exception_chain_has_status_code(
            exc, TRANSIENT_HTTP_STATUS_CODES
        )
    )
    async def _run_persist_workspace_command(self, tar_argv: list[str], archive_path: str) -> bytes:
        envs = await self._resolved_envs()
        result = await self._sandbox.run(
            "tar",
            tar_argv,
            env=envs or None,
            timeout=int(self.state.timeouts.snapshot_tar_s),
        )
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            raise ExecNonZeroError(
                ExecResult(
                    stdout=str(getattr(result, "stdout", "") or "").encode(
                        "utf-8", errors="replace"
                    ),
                    stderr=str(getattr(result, "stderr", "") or "").encode(
                        "utf-8", errors="replace"
                    ),
                    exit_code=exit_code,
                ),
                command=("tar", *tar_argv),
                context={"backend": "tensorlake", "sandbox_id": self.state.sandbox_id},
            )
        payload = await self._sandbox.read_file(archive_path)
        return _unwrap_traced_bytes(payload)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=self._workspace_root_path(), actual_type=type(raw).__name__
            )

        await with_ephemeral_mounts_removed(
            self,
            lambda: self._hydrate_workspace_internal(bytes(raw)),
            error_path=self._workspace_root_path(),
            error_cls=WorkspaceArchiveWriteError,
            operation_error_context_key="hydrate_error_before_remount_corruption",
        )

    async def _hydrate_workspace_internal(self, raw: bytes) -> None:
        snapshot_id = _decode_tensorlake_snapshot_ref(raw)
        if snapshot_id is not None:
            await self._restore_from_checkpoint(snapshot_id)
            return

        await self._hydrate_workspace_via_tar(raw)

    async def _restore_from_checkpoint(self, snapshot_id: str) -> None:
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)

        try:
            await self._sandbox.terminate()
        except Exception:
            pass

        kwargs = _create_kwargs(
            _TensorlakeLifecycleConfig.from_state(self.state),
            snapshot_id=snapshot_id,
            memory_snapshot=self.state.checkpoint_mode == "memory",
        )

        try:
            sandbox = await AsyncSandbox.create(**kwargs)
        except Exception as exc:
            raise WorkspaceArchiveWriteError(
                path=error_root,
                context={
                    "reason": "tensorlake_checkpoint_restore_failed",
                    "snapshot_id": snapshot_id,
                },
                cause=exc,
            ) from exc

        self._sandbox = sandbox
        # `_backend_lifecycle_finalized` tracks the current `self._sandbox` handle.
        # Rebinding must clear it so `delete()` does not short-circuit on a live sandbox.
        self._backend_lifecycle_finalized = False
        # The new sandbox has a different sandbox_url; clear the cache so the next
        # _resolve_exposed_port() call fetches the updated hostname from the new backend.
        self._proxy_hostname_resolved = False
        self._cached_proxy_hostname = None
        new_id = await _resolve_sandbox_id(sandbox)
        if new_id is not None:
            self.state.sandbox_id = new_id
        try:
            await self._apply_exposed_ports()
        except Exception:
            with suppress(Exception):
                await sandbox.terminate()
            self._backend_lifecycle_finalized = True
            raise
        self.state.workspace_root_ready = True
        # The restored checkpoint carries full OS state (users, groups, system packages), so
        # the base start flow must not re-run groupadd/useradd for accounts already present.
        self._set_start_state_preserved(True, system=True)

    async def _hydrate_workspace_via_tar(self, raw: bytes) -> None:
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)

        try:
            validate_tar_bytes(raw, allow_external_symlink_targets=False)
        except UnsafeTarMemberError as exc:
            raise WorkspaceArchiveWriteError(
                path=error_root,
                context={
                    "reason": "unsafe_or_invalid_tar",
                    "member": exc.member,
                    "detail": str(exc),
                },
                cause=exc,
            ) from exc

        archive_path = f"/tmp/openai-agents-hydrate-{self.state.session_id.hex}.tar"

        try:
            await self._prepare_backend_workspace()
            await self._sandbox.write_file(archive_path, raw)
            envs = await self._resolved_envs()
            result = await self._sandbox.run(
                "tar",
                ["xf", archive_path, "-C", root.as_posix()],
                env=envs or None,
                timeout=int(self.state.timeouts.snapshot_tar_s),
            )
        except WorkspaceStartError as exc:
            raise WorkspaceArchiveWriteError(path=error_root, cause=exc) from exc
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=error_root, cause=exc) from exc
        finally:
            await self._remove_tmp_archive(archive_path)

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
        self.state.workspace_root_ready = True

    async def _remove_tmp_archive(self, archive_path: str) -> None:
        """Best-effort cleanup of a `/tmp` tar archive used for workspace persistence."""
        try:
            # `delete_file` has no timeout knob; bound it so a hung daemon doesn't
            # block the outer persist/hydrate flow indefinitely on a best-effort op.
            await asyncio.wait_for(
                self._sandbox.delete_file(archive_path),
                timeout=self.state.timeouts.fast_op_s,
            )
        except Exception:
            pass

    async def _apply_exposed_ports(self) -> None:
        ports = list(self.state.exposed_ports)
        if not ports:
            return
        try:
            await asyncio.wait_for(
                self._sandbox.update(
                    exposed_ports=ports,
                    allow_unauthenticated_access=self.state.allow_unauthenticated_port_access,
                ),
                timeout=self.state.timeouts.fast_op_s,
            )
        except Exception as exc:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                message="failed to expose Tensorlake sandbox ports",
                context={
                    "reason": "tensorlake_exposed_ports_update_failed",
                    "sandbox_id": self.state.sandbox_id,
                    "ports": ports,
                    "allow_unauthenticated_access": self.state.allow_unauthenticated_port_access,
                },
                cause=exc,
            ) from exc


class TensorlakeSandboxClient(BaseSandboxClient[TensorlakeSandboxClientOptions]):
    """Tensorlake-backed sandbox client."""

    backend_id = "tensorlake"
    _instrumentation: Instrumentation

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
        options: TensorlakeSandboxClientOptions,
    ) -> SandboxSession:
        if manifest is None:
            manifest = Manifest(root=DEFAULT_TENSORLAKE_WORKSPACE_ROOT)
        elif manifest.root == _DEFAULT_MANIFEST_ROOT:
            # The default Tensorlake image runs as `tl-user`, which cannot write to the
            # cross-provider default `/workspace`. Rewrite manifests that still carry that
            # default so common construction patterns like `Manifest(entries=...)` work
            # against this backend without callers having to know the writable path.
            manifest = manifest.model_copy(
                update={"root": DEFAULT_TENSORLAKE_WORKSPACE_ROOT}, deep=True
            )

        timeouts_in = options.timeouts
        if isinstance(timeouts_in, TensorlakeSandboxTimeouts):
            timeouts = timeouts_in
        elif timeouts_in is None:
            timeouts = TensorlakeSandboxTimeouts()
        else:
            timeouts = TensorlakeSandboxTimeouts.model_validate(timeouts_in)

        if options.workspace_persistence not in (
            _WORKSPACE_PERSISTENCE_TAR,
            _WORKSPACE_PERSISTENCE_SNAPSHOT,
        ):
            raise ValueError(
                "TensorlakeSandboxClient.create requires workspace_persistence to be one of "
                f"{_WORKSPACE_PERSISTENCE_TAR!r} or {_WORKSPACE_PERSISTENCE_SNAPSHOT!r}"
            )

        # `timeout_secs` is an *idle threshold* on sandbox-proxy traffic, not a
        # wall-clock lifetime. `checkpoint()` polling goes through Tensorlake's
        # lifecycle/control-plane client rather than the sandbox proxy, so no proxied
        # traffic flows while a checkpoint is in flight — if the idle threshold is
        # smaller than the checkpoint poll budget, the sandbox can idle-time out
        # mid-poll and orphan the snapshot. Require the idle threshold to exceed the
        # poll budget so the snapshot can settle. `timeout_secs=0` requests the plan
        # maximum (≥1h on every Tensorlake plan, far larger than any reasonable
        # `checkpoint_timeout_s`), so it is exempt alongside `None`.
        if (
            options.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT
            and options.timeout_secs is not None
            and options.timeout_secs > 0
            and options.timeout_secs <= options.checkpoint_timeout_s
        ):
            raise ValueError(
                "timeout_secs must be strictly greater than checkpoint_timeout_s when "
                "workspace_persistence='snapshot'; otherwise the sandbox can be "
                "auto-terminated during checkpoint polling, orphaning the snapshot. "
                f"Got timeout_secs={options.timeout_secs}, "
                f"checkpoint_timeout_s={options.checkpoint_timeout_s}."
            )

        session_id = uuid.uuid4()
        sandbox_name = _resolve_lifecycle_sandbox_name(
            name=options.name,
            pause_on_exit=options.pause_on_exit,
            session_id=session_id,
        )

        kwargs = _create_kwargs(_TensorlakeLifecycleConfig.from_options(options, name=sandbox_name))

        sandbox = await AsyncSandbox.create(**kwargs)
        sandbox_id = await _resolve_sandbox_id(sandbox)
        if not sandbox_id:
            with suppress(Exception):
                await sandbox.terminate()
            raise RuntimeError(
                "Tensorlake `AsyncSandbox.create` did not return a sandbox with a `sandbox_id`."
            )

        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = TensorlakeSandboxSessionState.from_options(
            options,
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            sandbox_id=sandbox_id,
            name=sandbox_name,
            timeouts=timeouts,
        )
        inner = TensorlakeSandboxSession.from_state(state, sandbox=sandbox)
        try:
            await inner._apply_exposed_ports()
        except Exception:
            with suppress(Exception):
                await sandbox.terminate()
            raise
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, TensorlakeSandboxSession):
            raise TypeError("TensorlakeSandboxClient.delete expects a TensorlakeSandboxSession")
        # `delete` runs after `shutdown()` in the manager; only terminate when shutdown didn't
        # already (e.g. `pause_on_exit=True` suspended instead) so we don't double-call the
        # backend, while still freeing remote resources on direct `client.delete(...)` use.
        if inner._sandbox is None:
            return session
        if inner._backend_lifecycle_finalized:
            # Remote lifecycle already finalized (e.g. via `_restore_from_checkpoint`'s
            # error path) but the local Rust client handle is still attached — release
            # it so the connection pool is freed.
            inner._close_sandbox_handle()
            return session
        try:
            await inner._sandbox.terminate()
            inner._backend_lifecycle_finalized = True
            # `terminate()` already closed the local Rust client; drop the reference
            # directly to avoid a redundant close.
            inner._sandbox = None
        except Exception:
            # Terminate failed; this is the final cleanup hop, so free the local handle
            # even though the remote may still be running.
            inner._close_sandbox_handle()
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        if not isinstance(state, TensorlakeSandboxSessionState):
            raise TypeError(
                "TensorlakeSandboxClient.resume expects a TensorlakeSandboxSessionState"
            )

        cfg = _TensorlakeLifecycleConfig.from_state(state)
        connect_kwargs = _connect_kwargs(cfg)

        sandbox: Any = None
        reconnected = False
        try:
            sandbox = await AsyncSandbox.connect(state.sandbox_id, **connect_kwargs)
            if state.pause_on_exit:
                # `connect` returns a handle even for a paused/expired sandbox; `resume` is
                # what actually transitions it to running. Failures must fall through so the
                # outer handler recreates rather than marking a dead backend as preserved.
                await sandbox.resume()
            status = await sandbox.status()
            if status != SandboxStatus.RUNNING:
                raise RuntimeError("tensorlake sandbox is not running")
            reconnected = True
        except Exception:
            if sandbox is not None:
                if state.pause_on_exit:
                    # The user opted into suspend lifecycle and expects the backend to
                    # preserve workspace state across resume. A probe failure after a
                    # successful `connect()` is ambiguous — it can be a transient blip
                    # against a still-suspended sandbox — so terminating here would
                    # destroy potentially recoverable state (especially with a Noop or
                    # stale snapshot). Drop the local handle only; the remote either
                    # reconnects on a later attempt or auto-expires via `timeout_secs`.
                    with suppress(Exception):
                        sandbox.close()
                else:
                    # Without suspend lifecycle, no cross-resume state is expected.
                    # Terminate so the abandoned remote sandbox doesn't linger on the
                    # backend until its own timeout expires.
                    with suppress(Exception):
                        await sandbox.terminate()
                sandbox = None

        recreate_snapshot_id: str | None = None
        if sandbox is None:
            if state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT:
                # Skip the throwaway empty sandbox that `hydrate_workspace` would otherwise
                # terminate and replace from the same snapshot.
                recreate_snapshot_id = await _restore_tensorlake_snapshot_reference_id(
                    state.snapshot
                )
            sandbox_name = _resolve_lifecycle_sandbox_name(
                name=state.name,
                pause_on_exit=state.pause_on_exit,
                session_id=state.session_id,
            )
            recreate_cfg = replace(cfg, name=sandbox_name)
            kwargs = _create_kwargs(
                recreate_cfg,
                snapshot_id=recreate_snapshot_id,
                memory_snapshot=state.checkpoint_mode == "memory",
            )
            sandbox = await AsyncSandbox.create(**kwargs)
            new_id = await _resolve_sandbox_id(sandbox)
            if new_id is not None:
                state.sandbox_id = new_id
            state.name = sandbox_name
            state.workspace_root_ready = recreate_snapshot_id is not None

        inner = TensorlakeSandboxSession.from_state(state, sandbox=sandbox)
        preserved = reconnected or recreate_snapshot_id is not None
        inner._set_start_state_preserved(preserved, system=preserved)
        try:
            await inner._apply_exposed_ports()
        except Exception:
            if not reconnected:
                with suppress(Exception):
                    await sandbox.terminate()
            raise
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return cast(SandboxSessionState, TensorlakeSandboxSessionState.model_validate(payload))


__all__ = [
    "DEFAULT_TENSORLAKE_WORKSPACE_ROOT",
    "TensorlakeSandboxClient",
    "TensorlakeSandboxClientOptions",
    "TensorlakeSandboxSession",
    "TensorlakeSandboxSessionState",
    "TensorlakeSandboxTimeouts",
]

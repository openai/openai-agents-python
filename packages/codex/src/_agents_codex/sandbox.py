"""Sandbox resource providers for native executor environments."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import ValidationError

from agents.classic.sandbox._mount_security import (
    _mark_mount_error_data_safe,
    rebind_manifest_mount_authority,
    redact_mount_error_data_sync,
)
from agents.classic.sandbox.entries import Dir, LocalDir, LocalFile
from agents.classic.sandbox.entries.mounts import (
    AzureBlobMount,
    BoxMount,
    DockerVolumeMountStrategy,
    FuseMountPattern,
    GCSMount,
    InContainerMountStrategy,
    Mount,
    MountpointMountPattern,
    R2Mount,
    RcloneMountPattern,
    S3FilesMount,
    S3FilesMountPattern,
    S3Mount,
)
from agents.classic.sandbox.errors import MountConfigError
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.session.sandbox_client import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
)
from agents.classic.sandbox.session.sandbox_session import SandboxSession

# Optional provider owners initialize before their shared resource types.
# isort: split
from _agents_shared.apply_diff import apply_diff
from _agents_shared.retry import BackoffStrategy, retry_async
from _agents_shared.sandbox_limits import SandboxArchiveLimits, SandboxConcurrencyLimits
from _agents_shared.sandbox_session_state import SandboxSessionState
from _agents_shared.snapshot import (
    LocalSnapshot,
    LocalSnapshotSpec,
    NoopSnapshot,
    NoopSnapshotSpec,
    RemoteSnapshot,
    RemoteSnapshotSpec,
    SnapshotBase,
    SnapshotSpec,
)
from _agents_shared.token_truncation import TruncationPolicy, truncate_text

# isort: split

from ._public import preserve_module_identity as _preserve_module_identity
from .executor import Environment

_OptionsT = TypeVar("_OptionsT")
_ContextT = TypeVar("_ContextT")


def _configuration_error(error: ValueError | TypeError, target: str) -> MountConfigError:
    # Missing-field names come from the declared schema, not input values or messages.
    missing = (
        sorted(
            {
                str(item["loc"][-1])
                for item in error.errors(
                    include_input=False, include_context=False, include_url=False
                )
                if item["type"] == "missing" and item["loc"]
            }
        )
        if isinstance(error, ValidationError)
        else []
    )
    suffix = " (missing: " + ", ".join(missing) + ")" if missing else ""
    safe = MountConfigError(message=f"Invalid sandbox {target}{suffix}")
    _mark_mount_error_data_safe(safe)
    return safe


def _manifest(value: Manifest) -> Manifest:
    if not isinstance(value, Manifest):
        error = MountConfigError(message="manifest must be a trusted Manifest instance")
        _mark_mount_error_data_safe(error)
        raise error
    try:
        return value.model_copy(deep=True)
    except (ValueError, TypeError) as error:
        raise _configuration_error(error, "manifest") from None


@redact_mount_error_data_sync
def _assert_current_mount_authority(state: SandboxSessionState, manifest: Manifest) -> None:
    rebound = rebind_manifest_mount_authority(
        state.manifest, manifest, provider_backend_id=state.type
    )
    if state.manifest.mount_targets() != rebound.mount_targets() or (
        state.manifest.mount_targets()
        and state.manifest.environment.normalized() != manifest.environment.normalized()
    ):
        raise MountConfigError(
            message="Sandbox mount authority must match current trusted configuration"
        )


@redact_mount_error_data_sync
def sandbox_environment(
    client: BaseSandboxClient[_OptionsT],
    *,
    manifest: Manifest,
    options: _OptionsT | Mapping[str, Any],
    connect: Callable[[SandboxSession], AbstractAsyncContextManager[Environment[_ContextT]]],
    snapshot: SnapshotSpec | SnapshotBase | None = None,
    session: SandboxSession | None = None,
    authorized_resume_state: SandboxSessionState | None = None,
    concurrency_limits: SandboxConcurrencyLimits | None = None,
    archive_limits: SandboxArchiveLimits | None = None,
) -> AbstractAsyncContextManager[Environment[_ContextT]]:
    """Own a provider-selected sandbox and its native gateway for one native Session.

    The connect manager must connect to the supplied sandbox and own gateway cleanup.
    The provider validates and materializes the captured manifest for create and
    resume. A borrowed session must match the current mount authority. A supplied live
    session is borrowed: only its gateway is closed. An authorized_resume_state
    explicitly authorizes all provider identity and snapshot fields, not merely
    their types. Never pass untrusted deserialized state without authorizing those
    fields. Resume uses the current manifest for filesystem authority. Snapshot,
    live session and resume state are mutually exclusive. Create a new source for
    each native Session.
    Cleanup follows client policy, including deletion of selected backends where
    the provider implements it. The caller must authorize that disposal.
    Supply an application-authorized Manifest and trusted provider options.
    Mappings are not accepted as manifests. Validate untrusted data before
    constructing a Manifest; construction alone does not authorize its contents.
    """
    try:
        captured_options = deepcopy(
            cast(_OptionsT, BaseSandboxClientOptions.parse(dict(options)))
            if isinstance(options, Mapping)
            else options
        )
    except (ValueError, TypeError) as error:
        raise _configuration_error(error, "options") from None

    captured_manifest = _manifest(manifest)
    if session is not None or authorized_resume_state is not None:
        captured_manifest = _manifest(
            client.resolve_trusted_manifest_for_resume(captured_manifest, captured_options)
        )
    if concurrency_limits is not None:
        concurrency_limits = deepcopy(concurrency_limits)
        concurrency_limits.validate()
    if archive_limits is not None:
        archive_limits = deepcopy(archive_limits)
        archive_limits.validate()
    if sum(value is not None for value in (snapshot, session, authorized_resume_state)) > 1:
        raise ValueError("snapshot, session and authorized_resume_state are mutually exclusive")
    if session is not None and not isinstance(session, SandboxSession):
        raise TypeError("session must be a SandboxSession")
    if session is not None:
        if session.state.type != client.backend_id:
            raise ValueError("session backend must match the configured client")
        if concurrency_limits is not None or archive_limits is not None:
            raise ValueError("configure borrowed session budgets before supplying the session")
        _assert_current_mount_authority(session.state, captured_manifest)
    if authorized_resume_state is not None:
        if not isinstance(authorized_resume_state, SandboxSessionState):
            raise TypeError(
                "authorized_resume_state must be an application-authorized SandboxSessionState"
            )
        if authorized_resume_state.type != client.backend_id:
            raise ValueError("resume state backend must match the configured client")
        if authorized_resume_state.manifest.root != captured_manifest.root:
            raise ValueError("resume state workspace root must match current manifest")
        if not authorized_resume_state.mount_authority_redacted:
            _assert_current_mount_authority(authorized_resume_state, captured_manifest)
        # Provider identity and snapshot fields require explicit caller authorization.
        # Current configuration, never persisted manifest fields, grants authority.
        authorized_resume_state = authorized_resume_state.model_copy(deep=True)
        authorized_resume_state = authorized_resume_state.rebind_persisted_path_grants(
            captured_manifest
        )
        authorized_resume_state = authorized_resume_state.rebind_persisted_mount_authority(
            captured_manifest, provider_backend_id=client.backend_id
        )
        authorized_resume_state.manifest = _manifest(captured_manifest)

    @asynccontextmanager
    async def source() -> AsyncIterator[Environment[_ContextT]]:
        async with AsyncExitStack() as owners:
            selected = session
            if selected is None:
                selected = (
                    await client.resume(authorized_resume_state.model_copy(deep=True))
                    if authorized_resume_state is not None
                    else await client.create(
                        manifest=_manifest(captured_manifest),
                        options=deepcopy(captured_options),
                        **({"snapshot": snapshot} if snapshot is not None else {}),
                    )
                )
                owners.push_async_callback(client.delete, selected)
                owners.push_async_callback(selected.aclose)
                if concurrency_limits is not None:
                    selected._set_concurrency_limits(concurrency_limits)
                if archive_limits is not None:
                    selected._set_archive_limits(archive_limits)
                await selected.start()
            else:
                _assert_current_mount_authority(selected.state, captured_manifest)
                await selected._validate_manifest_application(manifest=captured_manifest)
            yield await owners.enter_async_context(connect(selected))

    return source()


@redact_mount_error_data_sync
def render_storage_layout(manifest: Manifest) -> str:
    """Render only mount paths, access modes and explicit caller descriptions.

    Supply the same application-authorized Manifest used for the sandbox.
    Credentials, provider settings, environment values and file contents are not
    included. Descriptions are trusted caller text; do not put secrets in them.
    The result is guidance, not filesystem authority.
    Pass the prepared session.state.manifest or configure the provider's effective
    root explicitly before rendering. Providers can replace the default root.
    """
    manifest = _manifest(manifest)
    storage = []
    for mount, path in manifest.mount_targets():
        read_only = mount.read_only
        if mount.type == "blaxel_drive_mount":
            path = Path("/") / (getattr(mount, "drive_mount_path", "") or path)
            read_only = getattr(mount, "drive_read_only", read_only)
        storage.append(
            {
                "path": path.as_posix(),
                "access": "read_only" if read_only else "read_write",
                **({"description": mount.description} if mount.description is not None else {}),
            }
        )
    return json.dumps(
        {
            "workspace": manifest.root,
            "storage": storage,
        },
        ensure_ascii=True,
        indent=2,
    )


__all__ = [
    "RemoteSnapshotSpec",
    "RemoteSnapshot",
    "NoopSnapshotSpec",
    "NoopSnapshot",
    "LocalSnapshotSpec",
    "LocalSnapshot",
    "truncate_text",
    "TruncationPolicy",
    "retry_async",
    "BackoffStrategy",
    "apply_diff",
    "SandboxConcurrencyLimits",
    "SandboxArchiveLimits",
    "SnapshotSpec",
    "SnapshotBase",
    "SandboxSessionState",
    "AzureBlobMount",
    "BaseSandboxClient",
    "BoxMount",
    "Dir",
    "DockerVolumeMountStrategy",
    "FuseMountPattern",
    "GCSMount",
    "InContainerMountStrategy",
    "LocalDir",
    "LocalFile",
    "Manifest",
    "Mount",
    "MountpointMountPattern",
    "R2Mount",
    "RcloneMountPattern",
    "S3FilesMount",
    "S3FilesMountPattern",
    "S3Mount",
    "SandboxSession",
    "render_storage_layout",
    "sandbox_environment",
]


_preserve_module_identity(globals(), "agents.sandbox")

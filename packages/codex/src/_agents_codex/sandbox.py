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


def _manifest(value: Manifest | Mapping[str, Any]) -> Manifest:
    try:
        captured = Manifest.model_validate(value).model_copy(deep=True)
        # Typed models remain mutable after their construction-time validation.
        for mount, _path in captured.mount_targets():
            mount.mount_strategy.validate_mount(mount)
        return captured
    except (ValueError, TypeError) as error:
        raise _configuration_error(error, "manifest") from None


@redact_mount_error_data_sync
def sandbox_environment(
    client: BaseSandboxClient[_OptionsT],
    *,
    manifest: Manifest | Mapping[str, Any],
    options: _OptionsT | Mapping[str, Any],
    connect: Callable[[SandboxSession], AbstractAsyncContextManager[Environment[_ContextT]]],
) -> AbstractAsyncContextManager[Environment[_ContextT]]:
    """Own a provider-selected sandbox and its native gateway for one native Session.

    The connect manager must connect to the supplied sandbox and own gateway cleanup.
    The provider validates and materializes the captured manifest. No saved sandbox
    state or snapshot is accepted. Create a new source for each native Session.
    Cleanup follows client policy, including deletion of selected backends where
    the provider implements it. The caller must authorize that disposal.
    """
    captured_manifest = _manifest(manifest)
    try:
        captured_options = deepcopy(
            cast(_OptionsT, BaseSandboxClientOptions.parse(dict(options)))
            if isinstance(options, Mapping)
            else options
        )
    except (ValueError, TypeError) as error:
        raise _configuration_error(error, "options") from None

    @asynccontextmanager
    async def source() -> AsyncIterator[Environment[_ContextT]]:
        async with AsyncExitStack() as owners:
            session = await client.create(manifest=captured_manifest, options=captured_options)
            owners.push_async_callback(client.delete, session)
            owners.push_async_callback(session.aclose)
            await session.start()
            yield await owners.enter_async_context(connect(session))

    return source()


@redact_mount_error_data_sync
def render_storage_layout(manifest: Manifest | Mapping[str, Any]) -> str:
    """Render only mount paths, access modes and explicit caller descriptions.

    Credentials, provider settings, environment values and file contents are not
    included. Descriptions are trusted caller text; do not put secrets in them.
    The result is guidance, not filesystem authority.
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

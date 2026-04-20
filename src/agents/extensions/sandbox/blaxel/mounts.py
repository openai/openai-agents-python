"""
Mount strategies for Blaxel sandboxes.

Two strategies are provided:

* **BlaxelCloudBucketMountStrategy** -- mounts S3, R2, and GCS buckets via
  FUSE tools (``s3fs``, ``gcsfuse``) executed inside the sandbox.  Credentials
  are written to ephemeral temp files, referenced by the FUSE tool, and deleted
  immediately after the mount succeeds.

* **BlaxelDriveMountStrategy** -- mounts Blaxel Drives (persistent network
  volumes) into the sandbox using the sandbox ``drives`` API
  (``POST /drives/mount``).  Drives persist data across sandbox sessions and
  can be shared between sandboxes.  See
  `Blaxel Drive docs <https://docs.blaxel.ai/Agent-drive/Overview>`_.
"""

from __future__ import annotations

import logging
import shlex
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ....sandbox.entries import GCSMount, Mount, R2Mount, S3Mount
from ....sandbox.entries.mounts.base import MountStrategyBase
from ....sandbox.errors import MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.types import FileMode, Permissions
from ....sandbox.workspace_paths import sandbox_path_str

logger = logging.getLogger(__name__)

BlaxelBucketProvider = Literal["s3", "r2", "gcs"]


@dataclass(frozen=True)
class BlaxelCloudBucketMountConfig:
    """Resolved mount config ready to be executed inside a Blaxel sandbox."""

    provider: BlaxelBucketProvider
    bucket: str
    mount_path: str
    read_only: bool = True

    # S3 / R2 fields.
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    prefix: str | None = None

    # GCS fields.
    service_account_key: str | None = None


class BlaxelCloudBucketMountStrategy(MountStrategyBase):
    """Mount S3/R2/GCS buckets inside Blaxel sandboxes via FUSE tools.

    ``activate`` installs the FUSE tool (if needed) and runs the mount command
    inside the sandbox.  ``deactivate`` / ``teardown_for_snapshot`` unmount via
    ``fusermount`` or ``umount``.
    """

    type: Literal["blaxel_cloud_bucket"] = "blaxel_cloud_bucket"

    def validate_mount(self, mount: Mount) -> None:
        _build_mount_config(mount, mount_path="/validate")

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _assert_blaxel_session(session)
        _ = base_dir
        mount_path = mount._resolve_mount_path(session, dest)
        config = _build_mount_config(mount, mount_path=mount_path.as_posix())
        await _mount_bucket(session, config)
        return []

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _assert_blaxel_session(session)
        _ = base_dir
        mount_path = mount._resolve_mount_path(session, dest)
        await _unmount_bucket(session, mount_path.as_posix())

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_blaxel_session(session)
        _ = mount
        await _unmount_bucket(session, sandbox_path_str(path))

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_blaxel_session(session)
        config = _build_mount_config(mount, mount_path=sandbox_path_str(path))
        await _mount_bucket(session, config)

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        _ = mount
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_INSTALL_RETRIES = 3


def _assert_blaxel_session(session: BaseSandboxSession) -> None:
    if type(session).__name__ != "BlaxelSandboxSession":
        raise MountConfigError(
            message="blaxel cloud bucket mounts require a BlaxelSandboxSession",
            context={"session_type": type(session).__name__},
        )


def _build_mount_config(mount: Mount, *, mount_path: str) -> BlaxelCloudBucketMountConfig:
    """Translate an S3Mount / R2Mount / GCSMount into a BlaxelCloudBucketMountConfig."""

    if isinstance(mount, S3Mount):
        return BlaxelCloudBucketMountConfig(
            provider="s3",
            bucket=mount.bucket,
            mount_path=mount_path,
            read_only=mount.read_only,
            access_key_id=mount.access_key_id,
            secret_access_key=mount.secret_access_key,
            session_token=mount.session_token,
            region=mount.region,
            endpoint_url=mount.endpoint_url,
            prefix=mount.prefix,
        )

    if isinstance(mount, R2Mount):
        mount._validate_credential_pair()
        return BlaxelCloudBucketMountConfig(
            provider="r2",
            bucket=mount.bucket,
            mount_path=mount_path,
            read_only=mount.read_only,
            access_key_id=mount.access_key_id,
            secret_access_key=mount.secret_access_key,
            endpoint_url=(
                mount.custom_domain or f"https://{mount.account_id}.r2.cloudflarestorage.com"
            ),
        )

    if isinstance(mount, GCSMount):
        if mount._use_s3_compatible_rclone():
            return BlaxelCloudBucketMountConfig(
                provider="s3",
                bucket=mount.bucket,
                mount_path=mount_path,
                read_only=mount.read_only,
                access_key_id=mount.access_id,
                secret_access_key=mount.secret_access_key,
                region=mount.region,
                endpoint_url=mount.endpoint_url or "https://storage.googleapis.com",
                prefix=mount.prefix,
            )
        return BlaxelCloudBucketMountConfig(
            provider="gcs",
            bucket=mount.bucket,
            mount_path=mount_path,
            read_only=mount.read_only,
            service_account_key=mount.service_account_credentials,
            prefix=mount.prefix,
        )

    raise MountConfigError(
        message="blaxel cloud bucket mounts only support S3Mount, R2Mount, and GCSMount",
        context={"mount_type": mount.type},
    )


async def _exec(session: BaseSandboxSession, cmd: str, timeout: float = 120) -> Any:
    """Execute a shell command inside the sandbox and return the result."""
    result = await session.exec("sh", "-c", cmd, timeout=timeout)
    return result


_APK_PACKAGE_NAMES: dict[str, str] = {
    "s3fs": "s3fs-fuse",
}

# gcsfuse is not available in Alpine repos.  We extract the static binary from the
# official .deb package (ar archive containing a data tarball).
_GCSFUSE_INSTALL_ALPINE = (
    "apk add --no-cache fuse curl binutils && "
    "GCSFUSE_VER=$("
    "curl -s https://api.github.com/repos/GoogleCloudPlatform/gcsfuse/releases/latest "
    '| grep -o \'"tag_name": *"[^"]*"\' | head -1 | grep -o \'v[0-9.]*\') && '
    "curl -fsSL https://github.com/GoogleCloudPlatform/gcsfuse/releases/download/"
    "${GCSFUSE_VER}/gcsfuse_${GCSFUSE_VER#v}_amd64.deb -o /tmp/gcsfuse.deb && "
    "cd /tmp && ar x gcsfuse.deb && "
    "tar -xf data.tar* -C / && "
    "rm -f gcsfuse.deb control.tar* data.tar* debian-binary"
)


# gcsfuse on Debian requires adding the Google Cloud apt repository first.
_GCSFUSE_INSTALL_DEBIAN = (
    "DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
    "apt-get install -y -qq curl gpg lsb-release && "
    "curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg "
    "| gpg --dearmor -o /etc/apt/keyrings/gcsfuse.gpg && "
    "CODENAME=$(lsb_release -cs) && "
    'echo "deb [signed-by=/etc/apt/keyrings/gcsfuse.gpg] '
    'https://packages.cloud.google.com/apt gcsfuse-${CODENAME} main" '
    "| tee /etc/apt/sources.list.d/gcsfuse.list && "
    "apt-get update -qq && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gcsfuse"
)


async def _install_tool(session: BaseSandboxSession, tool: str) -> None:
    """Install a FUSE tool (s3fs or gcsfuse) via apk/apt-get with retries."""
    # Detect package manager.
    detect = await _exec(session, "which apk >/dev/null 2>&1 && echo apk || echo apt")
    pkg_mgr = "apk" if b"apk" in detect.stdout else "apt"

    if pkg_mgr == "apk" and tool == "gcsfuse":
        # gcsfuse has no Alpine package; extract binary from the official .deb.
        install_cmd = _GCSFUSE_INSTALL_ALPINE
    elif pkg_mgr == "apk":
        pkg = _APK_PACKAGE_NAMES.get(tool, tool)
        install_cmd = f"apk add --no-cache {shlex.quote(pkg)}"
    elif tool == "gcsfuse":
        # gcsfuse is not in default Debian repos; add the Google Cloud apt source.
        install_cmd = _GCSFUSE_INSTALL_DEBIAN
    else:
        install_cmd = (
            f"apt-get update -qq && "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {shlex.quote(tool)}"
        )

    for _attempt in range(_INSTALL_RETRIES):
        result = await _exec(session, install_cmd, timeout=180)
        if result.exit_code == 0:
            return
    raise MountConfigError(
        message=f"failed to install {tool} after {_INSTALL_RETRIES} attempts",
        context={"tool": tool, "exit_code": result.exit_code},
    )


async def _ensure_tool(session: BaseSandboxSession, tool: str) -> None:
    """Check if a tool is available; install it if not."""
    check = await _exec(session, f"which {shlex.quote(tool)} >/dev/null 2>&1")
    if check.exit_code == 0:
        return
    await _install_tool(session, tool)


async def _mount_s3(session: BaseSandboxSession, config: BlaxelCloudBucketMountConfig) -> None:
    """Mount an S3 or R2 bucket using s3fs-fuse."""
    await _ensure_tool(session, "s3fs")

    # Write credentials to a temp file.
    cred_path = f"/tmp/s3fs-passwd-{uuid.uuid4().hex[:8]}"
    if config.access_key_id and config.secret_access_key:
        cred_content = f"{config.access_key_id}:{config.secret_access_key}"
        if config.session_token:
            cred_content += f":{config.session_token}"
        await session.exec(
            "sh",
            "-c",
            f"printf %s {shlex.quote(cred_content)} > {cred_path} && chmod 600 {cred_path}",
        )
    else:
        cred_path = ""

    # Build the s3fs command.
    bucket = config.bucket
    if config.prefix:
        bucket = f"{config.bucket}:/{config.prefix.strip('/')}"
    mount_path = shlex.quote(config.mount_path)

    opts = ["allow_other", "nonempty"]
    if cred_path:
        opts.append(f"passwd_file={cred_path}")
    else:
        opts.append("public_bucket=1")

    if config.endpoint_url:
        opts.append(f"url={config.endpoint_url}")
    elif config.region:
        opts.append(f"url=https://s3.{config.region}.amazonaws.com")
        opts.append(f"endpoint={config.region}")

    if config.provider == "r2":
        opts.append("sigv4")

    if config.read_only:
        opts.append("ro")

    opts_str = ",".join(opts)
    cmd = f"s3fs {shlex.quote(bucket)} {mount_path} -o {opts_str}"

    try:
        await _exec(session, f"mkdir -p {mount_path}")
        result = await _exec(session, cmd, timeout=60)
        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            raise MountConfigError(
                message="s3fs mount failed",
                context={"cmd": cmd, "exit_code": result.exit_code, "stderr": stderr},
            )
    finally:
        # Clean up credentials file.
        if cred_path:
            await _exec(session, f"rm -f {cred_path}")


async def _mount_gcs(session: BaseSandboxSession, config: BlaxelCloudBucketMountConfig) -> None:
    """Mount a GCS bucket using gcsfuse."""
    await _ensure_tool(session, "gcsfuse")

    mount_path = shlex.quote(config.mount_path)
    bucket = shlex.quote(config.bucket)

    # Write service account key if provided.
    key_path = ""
    if config.service_account_key:
        key_path = f"/tmp/gcs-creds-{uuid.uuid4().hex[:8]}.json"
        await session.exec(
            "sh",
            "-c",
            f"printf %s {shlex.quote(config.service_account_key)} "
            f"> {key_path} && chmod 600 {key_path}",
        )

    opts: list[str] = []
    if key_path:
        opts.append(f"--key-file={key_path}")
    else:
        opts.append("--anonymous-access")

    if config.read_only:
        opts.append("-o ro")

    if config.prefix:
        opts.append(f"--only-dir={config.prefix.strip('/')}")

    opts_str = " ".join(opts)
    cmd = f"gcsfuse {opts_str} {bucket} {mount_path}"

    try:
        await _exec(session, f"mkdir -p {mount_path}")
        result = await _exec(session, cmd, timeout=60)
        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            raise MountConfigError(
                message="gcsfuse mount failed",
                context={"cmd": cmd, "exit_code": result.exit_code, "stderr": stderr},
            )
    finally:
        if key_path:
            await _exec(session, f"rm -f {key_path}")


async def _mount_bucket(session: BaseSandboxSession, config: BlaxelCloudBucketMountConfig) -> None:
    """Dispatch to the appropriate FUSE mount function."""
    if config.provider in ("s3", "r2"):
        await _mount_s3(session, config)
    elif config.provider == "gcs":
        await _mount_gcs(session, config)
    else:
        raise MountConfigError(
            message=f"unsupported mount provider: {config.provider}",
            context={"provider": config.provider},
        )


async def _unmount_bucket(session: BaseSandboxSession, mount_path: str) -> None:
    """Unmount a FUSE mount point.  Tries fusermount first, falls back to umount."""
    path = shlex.quote(mount_path)
    # Try fusermount (FUSE-aware).
    result = await _exec(session, f"fusermount -u {path}")
    if result.exit_code == 0:
        return
    logger.debug("fusermount failed for %s (exit %d), trying umount", mount_path, result.exit_code)
    # Fallback to regular umount.
    result = await _exec(session, f"umount {path}")
    if result.exit_code == 0:
        return
    logger.debug("umount failed for %s (exit %d), trying lazy umount", mount_path, result.exit_code)
    # Last resort: lazy unmount.
    result = await _exec(session, f"umount -l {path}")
    if result.exit_code != 0:
        logger.warning(
            "all unmount attempts failed for %s (last exit %d)", mount_path, result.exit_code
        )


# ---------------------------------------------------------------------------
# Blaxel Drive mount strategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlaxelDriveMountConfig:
    """Configuration for mounting a Blaxel Drive into a sandbox.

    Blaxel Drives are persistent network volumes managed by the Blaxel platform.
    Data written to a drive persists across sandbox sessions and can be shared
    between multiple sandboxes.

    See https://docs.blaxel.ai/Agent-drive/Overview for details.
    """

    drive_name: str
    mount_path: str
    drive_path: str = "/"
    read_only: bool = False


class BlaxelDriveMount(Mount):
    """A concrete Mount entry for Blaxel Drives.

    Carries the drive configuration fields directly on the mount, following
    the same pattern as ``S3Mount``, ``R2Mount``, and ``GCSMount``.

    Usage::

        from agents.extensions.sandbox.blaxel import (
            BlaxelDriveMount,
            BlaxelDriveMountStrategy,
        )

        mount = BlaxelDriveMount(
            drive_name="my-drive",
            drive_mount_path="/data",
            mount_strategy=BlaxelDriveMountStrategy(),
        )
    """

    type: Literal["blaxel_drive_mount"] = "blaxel_drive_mount"
    drive_name: str
    drive_mount_path: str = ""
    drive_path: str = "/"
    drive_read_only: bool = False

    def model_post_init(self, context: object, /) -> None:
        """Validate the mount strategy without requiring in-container or docker patterns.

        Blaxel drives use a platform-level API (``POST /drives/mount``) rather
        than in-container FUSE tools or Docker volume drivers, so the base
        ``Mount`` validation for those patterns does not apply.
        """
        _ = context
        default_permissions = Permissions(
            owner=FileMode.ALL,
            group=FileMode.READ | FileMode.EXEC,
            other=FileMode.READ | FileMode.EXEC,
        )
        if (
            self.permissions.owner != default_permissions.owner
            or self.permissions.group != default_permissions.group
            or self.permissions.other != default_permissions.other
        ):
            warnings.warn(
                "Mount permissions are not enforced. "
                "Please configure access in the cloud provider instead; "
                "mount-level permissions can be unreliable.",
                stacklevel=2,
            )
            self.permissions.owner = default_permissions.owner
            self.permissions.group = default_permissions.group
            self.permissions.other = default_permissions.other
        self.permissions.directory = True
        self.mount_strategy.validate_mount(self)


class BlaxelDriveMountStrategy(MountStrategyBase):
    """Mount a Blaxel Drive into a sandbox via the sandbox drives API.

    This strategy uses the sandbox's ``drives`` sub-system (which wraps
    ``POST /drives/mount`` and ``DELETE /drives/mount/<path>``) to attach
    and detach persistent drives.

    Usage with a ``BlaxelDriveMount`` entry::

        from agents.extensions.sandbox.blaxel import (
            BlaxelDriveMount,
            BlaxelDriveMountStrategy,
        )

        mount = BlaxelDriveMount(
            drive_name="my-drive",
            drive_mount_path="/data",
            mount_strategy=BlaxelDriveMountStrategy(),
        )
    """

    type: Literal["blaxel_drive"] = "blaxel_drive"

    def validate_mount(self, mount: Mount) -> None:
        if not isinstance(mount, BlaxelDriveMount):
            raise MountConfigError(
                message=("BlaxelDriveMountStrategy requires a BlaxelDriveMount entry"),
                context={"mount_type": mount.type},
            )

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _assert_blaxel_session(session)
        _ = base_dir
        config = self._resolve_config(mount, session, dest)
        sandbox = getattr(session, "_sandbox", None)
        if sandbox is None:
            raise MountConfigError(
                message="cannot access sandbox instance for drive mount",
                context={"session_type": type(session).__name__},
            )
        await _attach_drive(sandbox, config)
        return []

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _assert_blaxel_session(session)
        _ = base_dir
        config = self._resolve_config(mount, session, dest)
        sandbox = getattr(session, "_sandbox", None)
        if sandbox is not None:
            await _detach_drive(sandbox, config.mount_path)

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_blaxel_session(session)
        effective_path = self._effective_mount_path(mount, path)
        sandbox = getattr(session, "_sandbox", None)
        if sandbox is not None:
            await _detach_drive(sandbox, effective_path)

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_blaxel_session(session)
        effective_path = self._effective_mount_path(mount, path)
        config = self._resolve_config_from_source(mount, effective_path)
        sandbox = getattr(session, "_sandbox", None)
        if sandbox is None:
            raise MountConfigError(
                message="cannot access sandbox instance for drive remount",
                context={"session_type": type(session).__name__},
            )
        await _attach_drive(sandbox, config)

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        _ = mount
        return None

    @staticmethod
    def _resolve_config(
        mount: Mount, session: BaseSandboxSession, dest: Path
    ) -> BlaxelDriveMountConfig:
        if not isinstance(mount, BlaxelDriveMount):
            raise MountConfigError(
                message="BlaxelDriveMountStrategy requires a BlaxelDriveMount entry",
                context={"mount_type": mount.type},
            )
        mount_path = mount.drive_mount_path or sandbox_path_str(
            mount._resolve_mount_path(session, dest)
        )
        return BlaxelDriveMountConfig(
            drive_name=mount.drive_name,
            mount_path=mount_path,
            drive_path=mount.drive_path,
            read_only=mount.drive_read_only,
        )

    @staticmethod
    def _effective_mount_path(mount: Mount, fallback: Path) -> str:
        """Return the actual mount path, preferring ``drive_mount_path`` over the manifest path."""
        if isinstance(mount, BlaxelDriveMount) and mount.drive_mount_path:
            return mount.drive_mount_path
        return sandbox_path_str(fallback)

    @staticmethod
    def _resolve_config_from_source(mount: Mount, mount_path: str) -> BlaxelDriveMountConfig:
        if not isinstance(mount, BlaxelDriveMount):
            raise MountConfigError(
                message="BlaxelDriveMountStrategy requires a BlaxelDriveMount entry",
                context={"mount_type": mount.type},
            )
        return BlaxelDriveMountConfig(
            drive_name=mount.drive_name,
            mount_path=mount_path,
            drive_path=mount.drive_path,
            read_only=mount.drive_read_only,
        )


async def _attach_drive(sandbox: Any, config: BlaxelDriveMountConfig) -> None:
    """Attach a Blaxel Drive to a sandbox via ``sandbox.drives.mount()``."""
    drives = getattr(sandbox, "drives", None)
    if drives is not None and hasattr(drives, "mount"):
        try:
            await drives.mount(config.drive_name, config.mount_path, config.drive_path)
        except Exception as e:
            raise MountConfigError(
                message=f"drive mount failed for {config.drive_name}",
                context={
                    "drive_name": config.drive_name,
                    "mount_path": config.mount_path,
                    "detail": str(e),
                },
            ) from e
        return
    raise MountConfigError(
        message="sandbox does not expose a drives API",
        context={"sandbox_type": type(sandbox).__name__},
    )


async def _detach_drive(sandbox: Any, mount_path: str) -> None:
    """Detach a Blaxel Drive from a sandbox (best-effort)."""
    drives = getattr(sandbox, "drives", None)
    if drives is not None and hasattr(drives, "unmount"):
        try:
            await drives.unmount(mount_path)
        except Exception as e:
            logger.warning("drive detach failed for %s (non-fatal): %s", mount_path, e)


__all__ = [
    "BlaxelCloudBucketMountConfig",
    "BlaxelCloudBucketMountStrategy",
    "BlaxelDriveMountConfig",
    "BlaxelDriveMountStrategy",
]

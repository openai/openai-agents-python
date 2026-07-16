"""Mount strategy for Daytona sandboxes.

Provides ``DaytonaCloudBucketMountStrategy``, a wrapper around the generic
:class:`InContainerMountStrategy` that ensures ``rclone`` is installed inside
the sandbox before delegating to :class:`RcloneMountPattern`.

Supports S3, R2, GCS, Azure Blob, and Box mounts through a single code path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from ....sandbox.entries.mounts.base import InContainerMountStrategy, Mount, MountStrategyBase
from ....sandbox.entries.mounts.patterns import RcloneMountPattern
from ....sandbox.errors import MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession

logger = logging.getLogger(__name__)

_INSTALL_RETRIES = 3


# ---------------------------------------------------------------------------
# Tool provisioning helpers
# ---------------------------------------------------------------------------


async def _has_command(session: BaseSandboxSession, cmd: str) -> bool:
    """Return True if *cmd* is on PATH or at a well-known location."""
    check = await session.exec(
        "sh",
        "-lc",
        f"command -v {cmd} >/dev/null 2>&1 || test -x /usr/local/bin/{cmd}",
        shell=False,
    )
    return check.ok()


async def _pkg_install(
    session: BaseSandboxSession,
    package: str,
    *,
    what: str,
) -> None:
    """Install *package* via apt-get or apk with retries.

    Detects the available package manager (apt-get for Debian/Ubuntu, apk for
    Alpine) and installs the package.  Raises :class:`MountConfigError` with an
    actionable message if neither is available or all install attempts fail.
    """
    if await _has_command(session, "apt-get"):
        install_cmd = (
            f"apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {package}"
        )
    elif await _has_command(session, "apk"):
        install_cmd = f"apk add --no-cache {package}"
    else:
        raise MountConfigError(
            message=(
                f"{what} is not installed and cannot be auto-installed "
                f"(no supported package manager found). Preinstall {package} in your Daytona image."
            ),
            context={"package": package},
        )

    for attempt in range(_INSTALL_RETRIES):
        result = await session.exec("sh", "-lc", install_cmd, shell=False, timeout=180, user="root")
        if result.ok():
            return
        logger.warning(
            "%s install attempt %d/%d failed (exit %d)",
            package,
            attempt + 1,
            _INSTALL_RETRIES,
            result.exit_code,
        )

    raise MountConfigError(
        message=f"failed to install {package} after {_INSTALL_RETRIES} attempts",
        context={"package": package, "exit_code": result.exit_code},
    )


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------


async def _ensure_fuse_support(session: BaseSandboxSession) -> None:
    """Verify the sandbox environment supports FUSE mounts.

    Checks for /dev/fuse, the fuse kernel module, and fusermount userspace
    tooling.  If the kernel bits are present but fusermount is missing, attempts
    to install ``fuse3`` via apt.  Non-apt images must preinstall fuse3.
    """
    # Kernel-level requirements (cannot be installed).
    dev_fuse = await session.exec("sh", "-lc", "test -c /dev/fuse", shell=False)
    if not dev_fuse.ok():
        raise MountConfigError(
            message="/dev/fuse not available in this sandbox",
            context={"missing": "/dev/fuse"},
        )
    kmod = await session.exec("sh", "-lc", "grep -qw fuse /proc/filesystems", shell=False)
    if not kmod.ok():
        raise MountConfigError(
            message="FUSE kernel module not loaded in this sandbox",
            context={"missing": "fuse in /proc/filesystems"},
        )

    # Userspace tooling — install if missing, re-verify after install.
    if await _has_command(session, "fusermount3") or await _has_command(session, "fusermount"):
        return

    logger.info("fusermount not found; installing fuse3")
    await _pkg_install(session, "fuse3", what="fusermount")

    if not (
        await _has_command(session, "fusermount3") or await _has_command(session, "fusermount")
    ):
        raise MountConfigError(
            message="fuse3 was installed but fusermount is still not available",
            context={"package": "fuse3"},
        )


async def _ensure_rclone(session: BaseSandboxSession) -> None:
    """Install rclone inside the sandbox if it is not already available."""
    if await _has_command(session, "rclone"):
        return

    logger.info("rclone not found in sandbox; installing via apt")
    await _pkg_install(session, "rclone", what="rclone")

    if not await _has_command(session, "rclone"):
        raise MountConfigError(
            message="rclone was installed but is still not available on PATH",
            context={"package": "rclone"},
        )


# ---------------------------------------------------------------------------
# Session guard
# ---------------------------------------------------------------------------


def _assert_daytona_session(session: BaseSandboxSession) -> None:
    if type(session).__name__ != "DaytonaSandboxSession":
        raise MountConfigError(
            message="daytona cloud bucket mounts require a DaytonaSandboxSession",
            context={"session_type": type(session).__name__},
        )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class DaytonaCloudBucketMountStrategy(MountStrategyBase):
    """Mount rclone-backed cloud storage in Daytona sandboxes.

    Wraps :class:`InContainerMountStrategy` with automatic ``rclone``
    provisioning.  Use with any rclone-backed provider mount (``S3Mount``,
    ``R2Mount``, ``GCSMount``, ``AzureBlobMount``, ``BoxMount``) and let the
    generic framework handle config generation and mount execution.

    Usage::

        from agents.extensions.sandbox.daytona import DaytonaCloudBucketMountStrategy
        from agents.sandbox.entries import S3Mount

        mount = S3Mount(
            bucket="my-bucket",
            access_key_id="...",
            secret_access_key="...",
            mount_path=Path("/mnt/bucket"),
            mount_strategy=DaytonaCloudBucketMountStrategy(),
        )
    """

    type: Literal["daytona_cloud_bucket"] = "daytona_cloud_bucket"
    pattern: RcloneMountPattern = RcloneMountPattern(mode="fuse")

    def _delegate(self) -> InContainerMountStrategy:
        return InContainerMountStrategy(pattern=self.pattern)

    def validate_mount(self, mount: Mount) -> None:
        self._delegate().validate_mount(mount)

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _assert_daytona_session(session)
        if self.pattern.mode == "fuse":
            await _ensure_fuse_support(session)
        await _ensure_rclone(session)
        return await self._delegate().activate(mount, session, dest, base_dir)

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _assert_daytona_session(session)
        await self._delegate().deactivate(mount, session, dest, base_dir)

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_daytona_session(session)
        await self._delegate().teardown_for_snapshot(mount, session, path)

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_daytona_session(session)
        if self.pattern.mode == "fuse":
            await _ensure_fuse_support(session)
        await _ensure_rclone(session)
        await self._delegate().restore_after_snapshot(mount, session, path)

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        return None


__all__ = [
    "DaytonaCloudBucketMountStrategy",
]

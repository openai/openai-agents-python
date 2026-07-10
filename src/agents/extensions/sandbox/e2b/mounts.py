"""Mount strategy for E2B sandboxes."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ....sandbox.entries.mounts.base import InContainerMountStrategy, Mount, MountStrategyBase
from ....sandbox.entries.mounts.patterns import RcloneMountPattern
from ....sandbox.errors import MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from .._rclone import (
    ensure_rclone as _ensure_rclone,
    rclone_pattern_for_session as _rclone_pattern_for_session,
)

_FUSE_ALLOW_OTHER = (
    "chmod a+rw /dev/fuse && "
    "touch /etc/fuse.conf && "
    "(grep -qxF user_allow_other /etc/fuse.conf || "
    "printf '\\nuser_allow_other\\n' >> /etc/fuse.conf)"
)


async def _ensure_fuse_support(session: BaseSandboxSession) -> None:
    check = await session.exec(
        "sh",
        "-lc",
        "test -c /dev/fuse && grep -qw fuse /proc/filesystems && "
        "(command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1)",
        shell=False,
    )
    if not check.ok():
        raise MountConfigError(
            message="E2B cloud bucket mounts require FUSE support and fusermount",
            context={"missing": "fuse"},
        )

    chmod_result = await session.exec(
        "sh",
        "-lc",
        _FUSE_ALLOW_OTHER,
        shell=False,
        timeout=30,
        user="root",
    )
    if not chmod_result.ok():
        raise MountConfigError(
            message="failed to make /dev/fuse accessible",
            context={"exit_code": chmod_result.exit_code},
        )


def _assert_e2b_session(session: BaseSandboxSession) -> None:
    if type(session).__name__ != "E2BSandboxSession":
        raise MountConfigError(
            message="e2b cloud bucket mounts require an E2BSandboxSession",
            context={"session_type": type(session).__name__},
        )


class E2BCloudBucketMountStrategy(MountStrategyBase):
    """Mount rclone-backed cloud storage in E2B sandboxes."""

    type: Literal["e2b_cloud_bucket"] = "e2b_cloud_bucket"
    pattern: RcloneMountPattern = RcloneMountPattern(mode="fuse")

    def _delegate(self) -> InContainerMountStrategy:
        return InContainerMountStrategy(pattern=self.pattern)

    async def _delegate_for_session(self, session: BaseSandboxSession) -> InContainerMountStrategy:
        return InContainerMountStrategy(
            pattern=await _rclone_pattern_for_session(session, self.pattern)
        )

    def validate_mount(self, mount: Mount) -> None:
        self._delegate().validate_mount(mount)

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _assert_e2b_session(session)
        if self.pattern.mode == "fuse":
            await _ensure_fuse_support(session)
        await _ensure_rclone(session)
        delegate = await self._delegate_for_session(session)
        return await delegate.activate(mount, session, dest, base_dir)

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _assert_e2b_session(session)
        await self._delegate().deactivate(mount, session, dest, base_dir)

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_e2b_session(session)
        await self._delegate().teardown_for_snapshot(mount, session, path)

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_e2b_session(session)
        if self.pattern.mode == "fuse":
            await _ensure_fuse_support(session)
        await _ensure_rclone(session)
        delegate = await self._delegate_for_session(session)
        await delegate.restore_after_snapshot(mount, session, path)

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        return None


__all__ = [
    "E2BCloudBucketMountStrategy",
]

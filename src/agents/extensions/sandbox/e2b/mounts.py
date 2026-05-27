"""Mount strategy for E2B sandboxes."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ....sandbox.entries.mounts.base import InContainerMountStrategy, Mount, MountStrategyBase
from ....sandbox.entries.mounts.patterns import RcloneMountPattern
from ....sandbox.errors import MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession

_APT = "DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0"
_RCLONE_CHECK = "command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone"
_INSTALL_RCLONE_COMMANDS = (
    f"{_APT} update -qq",
    f"{_APT} install -y -qq curl unzip ca-certificates",
    "curl -fsSL https://rclone.org/install.sh | bash",
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


async def _ensure_rclone(session: BaseSandboxSession) -> None:
    rclone = await session.exec("sh", "-lc", _RCLONE_CHECK, shell=False)
    if rclone.ok():
        return

    apt = await session.exec("sh", "-lc", "command -v apt-get >/dev/null 2>&1", shell=False)
    if not apt.ok():
        raise MountConfigError(
            message="rclone is not installed and apt-get is unavailable; preinstall rclone",
            context={"package": "rclone"},
        )

    for command in _INSTALL_RCLONE_COMMANDS:
        install = await session.exec("sh", "-lc", command, shell=False, timeout=300, user="root")
        if not install.ok():
            raise MountConfigError(
                message="failed to install rclone",
                context={"package": "rclone", "exit_code": install.exit_code},
            )

    rclone = await session.exec("sh", "-lc", _RCLONE_CHECK, shell=False)
    if not rclone.ok():
        raise MountConfigError(
            message="rclone was installed but is still not available on PATH",
            context={"package": "rclone"},
        )


async def _default_user_ids(session: BaseSandboxSession) -> tuple[str, str] | None:
    result = await session.exec("sh", "-lc", "id -u; id -g", shell=False, timeout=30)
    if not result.ok():
        return None

    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2 or not lines[0].isdigit() or not lines[1].isdigit():
        return None
    return lines[0], lines[1]


def _append_option(args: list[str], option: str, *values: str) -> None:
    if option not in args:
        args.extend([option, *values])


async def _rclone_pattern_for_session(
    session: BaseSandboxSession,
    pattern: RcloneMountPattern,
) -> RcloneMountPattern:
    if pattern.mode != "fuse":
        return pattern

    extra_args = list(pattern.extra_args)
    _append_option(extra_args, "--allow-other")
    user_ids = await _default_user_ids(session)
    if user_ids is not None:
        uid, gid = user_ids
        _append_option(extra_args, "--uid", uid)
        _append_option(extra_args, "--gid", gid)

    return pattern.model_copy(update={"extra_args": extra_args})


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

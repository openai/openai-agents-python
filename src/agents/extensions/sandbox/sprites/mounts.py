"""Mount strategy for Sprites sandboxes."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Literal

from ....sandbox.entries.mounts.base import InContainerMountStrategy, Mount, MountStrategyBase
from ....sandbox.entries.mounts.patterns import RcloneMountPattern
from ....sandbox.errors import MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession

# Sprite VMs run as the unprivileged ``sprite`` user with passwordless sudo.
# ``SpritesSandboxSession.exec`` rejects ``user=`` kwargs, so we prefix privileged
# commands with ``sudo -n`` instead of escalating through the framework.
_SUDO = "sudo -n"
_APT = (
    f"{_SUDO} env DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0"
)

# Detection commands echo a sentinel into stdout based on the *local* shell's
# evaluation of the conditional. We rely on stdout instead of ``ExecResult.ok()``
# because the sprite-env WS control protocol currently drops exec exit codes
# (the OP_COMPLETE envelope ships ``{"ok": true}`` with no exit-code field, so
# the Python client defaults to 0 for every command). Stdout sentinels are
# also more robust against tools that exit non-zero on benign warnings.
_PRESENT = "__SPRITES_PRESENT__"
_MISSING = "__SPRITES_MISSING__"
_MOUNTED = "__SPRITES_MOUNTED__"
_NOT_MOUNTED = "__SPRITES_NOT_MOUNTED__"


def _detect_cmd(condition: str) -> str:
    """Return a shell snippet that prints _PRESENT or _MISSING based on `condition`."""

    return f"if {condition}; then echo {_PRESENT}; else echo {_MISSING}; fi"


_RCLONE_CHECK = _detect_cmd("command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone")
_FUSERMOUNT_CHECK = _detect_cmd(
    "command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1"
)
_FUSE_KERNEL_CHECK = _detect_cmd("test -c /dev/fuse && grep -qw fuse /proc/filesystems")
_APT_CHECK = _detect_cmd("command -v apt-get >/dev/null 2>&1")
_INSTALL_RCLONE_COMMANDS = (
    f"{_APT} update -qq",
    f"{_APT} install -y -qq curl unzip ca-certificates fuse",
    f"curl -fsSL https://rclone.org/install.sh | {_SUDO} bash",
)
# fuse package brings ``fusermount`` along — install it together with rclone
# so the FUSE-mode mount path works out-of-the-box on stock sprite images.
_INSTALL_FUSE_COMMANDS = (
    f"{_APT} update -qq",
    f"{_APT} install -y -qq fuse",
)
_FUSE_ALLOW_OTHER = (
    f"{_SUDO} chmod a+rw /dev/fuse && "
    f"{_SUDO} touch /etc/fuse.conf && "
    "(grep -qxF user_allow_other /etc/fuse.conf || "
    f"printf '\\nuser_allow_other\\n' | {_SUDO} tee -a /etc/fuse.conf >/dev/null)"
)


def _stdout_says(result: object, sentinel: str) -> bool:
    stdout = getattr(result, "stdout", b"") or b""
    return sentinel.encode("ascii") in stdout


async def _ensure_fuse_support(session: BaseSandboxSession) -> None:
    kernel = await session.exec("sh", "-lc", _FUSE_KERNEL_CHECK, shell=False)
    if not _stdout_says(kernel, _PRESENT):
        raise MountConfigError(
            message="Sprites cloud bucket mounts require FUSE support in the kernel",
            context={"missing": "fuse"},
        )

    fusermount = await session.exec("sh", "-lc", _FUSERMOUNT_CHECK, shell=False)
    if not _stdout_says(fusermount, _PRESENT):
        apt = await session.exec("sh", "-lc", _APT_CHECK, shell=False)
        if not _stdout_says(apt, _PRESENT):
            raise MountConfigError(
                message="fusermount is not installed and apt-get is unavailable; "
                "preinstall the fuse package",
                context={"package": "fuse"},
            )
        for command in _INSTALL_FUSE_COMMANDS:
            await session.exec("sh", "-lc", command, shell=False, timeout=300)
        recheck = await session.exec("sh", "-lc", _FUSERMOUNT_CHECK, shell=False)
        if not _stdout_says(recheck, _PRESENT):
            raise MountConfigError(
                message="fuse install attempt completed but fusermount is still not on PATH",
                context={"package": "fuse"},
            )

    # /dev/fuse must be accessible to the unprivileged user and ``user_allow_other``
    # has to be enabled for ``--allow-other``. Failures here would be surfaced by
    # the rclone mount itself; we don't gate on this exec's exit code because the
    # control-WS protocol drops it.
    await session.exec("sh", "-lc", _FUSE_ALLOW_OTHER, shell=False, timeout=30)


async def _ensure_rclone(session: BaseSandboxSession) -> None:
    rclone = await session.exec("sh", "-lc", _RCLONE_CHECK, shell=False)
    if _stdout_says(rclone, _PRESENT):
        return

    apt = await session.exec("sh", "-lc", _APT_CHECK, shell=False)
    if not _stdout_says(apt, _PRESENT):
        raise MountConfigError(
            message="rclone is not installed and apt-get is unavailable; preinstall rclone",
            context={"package": "rclone"},
        )

    for command in _INSTALL_RCLONE_COMMANDS:
        await session.exec("sh", "-lc", command, shell=False, timeout=300)

    rclone = await session.exec("sh", "-lc", _RCLONE_CHECK, shell=False)
    if not _stdout_says(rclone, _PRESENT):
        raise MountConfigError(
            message="rclone install attempt completed but rclone is still not on PATH",
            context={"package": "rclone"},
        )


async def _verify_mount_active(session: BaseSandboxSession, mount_path: Path) -> None:
    """Confirm ``mount_path`` is a live mountpoint after activation.

    Without reliable exit codes from the platform we can't detect a failed
    rclone mount via ``rclone mount``'s return value. Probe the kernel's view
    of the path instead: ``mountpoint -q`` returns 0 iff the path is a mount
    boundary. The shell wraps the conditional and emits a stdout sentinel so
    the verification is transport-independent. ``rclone mount --daemon`` forks
    and the parent returns immediately, so we poll briefly to give the daemon
    time to bind.
    """

    # ``Path.as_posix()`` gives forward-slash form regardless of host OS,
    # which matters because the mount target lives inside the sprite VM
    # (POSIX) — running on a Windows agent would otherwise emit
    # ``\workspace\tigris`` here.
    posix_path = mount_path.as_posix()
    quoted = shlex.quote(posix_path)
    probe_cmd = (
        f"for _ in 1 2 3 4 5 6 7 8 9 10; do "
        f"if mountpoint -q {quoted}; then echo {_MOUNTED}; exit 0; fi; "
        "sleep 0.5; "
        f"done; echo {_NOT_MOUNTED}"
    )
    probe = await session.exec("sh", "-lc", probe_cmd, shell=False, timeout=30)
    if not _stdout_says(probe, _MOUNTED):
        raise MountConfigError(
            message="rclone mount completed but the path is not a live mountpoint",
            context={"path": posix_path},
        )

    # Force rclone to materialize the root directory listing before we hand
    # control back to the caller. Without this, the next ``readdir`` from the
    # agent races the daemon's first listing fetch and can briefly observe an
    # empty directory. The exit code is irrelevant here — we just want the
    # side effect of priming rclone's dir cache.
    await session.exec("sh", "-lc", f"ls {quoted} >/dev/null 2>&1", shell=False, timeout=15)


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


def _assert_sprites_session(session: BaseSandboxSession) -> None:
    if type(session).__name__ != "SpritesSandboxSession":
        raise MountConfigError(
            message="sprites cloud bucket mounts require a SpritesSandboxSession",
            context={"session_type": type(session).__name__},
        )


class SpritesCloudBucketMountStrategy(MountStrategyBase):
    """Mount rclone-backed cloud storage in Sprites sandboxes."""

    type: Literal["sprites_cloud_bucket"] = "sprites_cloud_bucket"
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
        _assert_sprites_session(session)
        if self.pattern.mode == "fuse":
            await _ensure_fuse_support(session)
        await _ensure_rclone(session)
        delegate = await self._delegate_for_session(session)
        files = await delegate.activate(mount, session, dest, base_dir)
        if self.pattern.mode == "fuse":
            mount_path = mount._resolve_mount_path(session, dest)
            await _verify_mount_active(session, mount_path)
        return files

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _assert_sprites_session(session)
        await self._delegate().deactivate(mount, session, dest, base_dir)

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_sprites_session(session)
        await self._delegate().teardown_for_snapshot(mount, session, path)

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _assert_sprites_session(session)
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
    "SpritesCloudBucketMountStrategy",
]

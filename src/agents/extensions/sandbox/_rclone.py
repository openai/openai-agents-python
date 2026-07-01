from __future__ import annotations

from ...sandbox.entries.mounts.patterns import RcloneMountPattern
from ...sandbox.errors import MountConfigError
from ...sandbox.session.base_sandbox_session import BaseSandboxSession

_APT = "DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0"
_RCLONE_CHECK = "command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone"
_INSTALL_RCLONE_COMMANDS = (
    f"{_APT} update -qq",
    f"{_APT} install -y -qq curl unzip ca-certificates",
    "curl -fsSL https://rclone.org/install.sh | bash",
)


async def ensure_rclone(session: BaseSandboxSession) -> None:
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
        install = await session.exec(
            "sh",
            "-lc",
            command,
            shell=False,
            timeout=300,
            user="root",
        )
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


async def rclone_pattern_for_session(
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

from __future__ import annotations

from ...sandbox.entries.mounts.patterns import RcloneMountPattern
from ...sandbox.errors import MountConfigError
from ...sandbox.session.base_sandbox_session import BaseSandboxSession

_APT = "DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0"
_RCLONE_CHECK = "command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone"
_RCLONE_CHECKSUM_MISMATCH_EXIT = 86

# BEGIN RCLONE RELEASE PIN
_RCLONE_VERSION = "1.74.4"
_RCLONE_SHA256_BY_ARCH = {
    "386": "7feee086d7ff72652c5a91ef4b4a576941ccd33b2929772a2d70471904e516f0",
    "amd64": "fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d",
    "arm": "8135524b9b85111fa512f10a3fa191736a8d4d6ac3b3169af0763503744e95c9",
    "arm-v6": "c9e1048feb597938884c0fff314d5d9a002599933cb94ce17fee19599cbfa3f1",
    "arm-v7": "75844809d25d2534da96220727e7746a300e30ec8c676ca98c47affe5a752e7b",
    "arm64": "97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419",
}
# END RCLONE RELEASE PIN

_INSTALL_RCLONE_PREREQUISITES = (
    f"{_APT} update -qq",
    f"{_APT} install -y -qq ca-certificates coreutils curl unzip",
)


def _rclone_arch(machine: str) -> str | None:
    normalized = machine.strip().lower()
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    if normalized == "x86" or (
        len(normalized) == 4
        and normalized[0] == "i"
        and normalized[1] in {"3", "4", "5", "6"}
        and normalized[2:] == "86"
    ):
        return "386"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    if normalized.startswith("armv7"):
        return "arm-v7"
    if normalized.startswith("armv6"):
        return "arm-v6"
    if normalized.startswith("arm"):
        return "arm"
    return None


def _rclone_install_command(arch: str, sha256: str) -> str:
    archive = f"rclone-v{_RCLONE_VERSION}-linux-{arch}.zip"
    url = f"https://downloads.rclone.org/v{_RCLONE_VERSION}/{archive}"
    return "\n".join(
        [
            "set -eu",
            'tmp_dir="$(mktemp -d)"',
            'target_tmp=""',
            "cleanup() {",
            '    rm -rf "$tmp_dir"',
            '    if [ -n "$target_tmp" ]; then rm -f "$target_tmp"; fi',
            "}",
            "trap cleanup EXIT",
            "trap 'exit 1' HUP INT TERM",
            f"archive='{archive}'",
            f"expected_sha256='{sha256}'",
            f"url='{url}'",
            (
                "curl --fail --location --silent --show-error --proto '=https' "
                '--tlsv1.2 --output "$tmp_dir/$archive" "$url"'
            ),
            (
                'if ! printf \'%s  %s\\n\' "$expected_sha256" "$tmp_dir/$archive" '
                "| sha256sum --check --strict -; then"
            ),
            f"    exit {_RCLONE_CHECKSUM_MISMATCH_EXIT}",
            "fi",
            'unzip -q "$tmp_dir/$archive" -d "$tmp_dir/unpacked"',
            "install -d -m 0755 /usr/local/bin",
            'target_tmp="$(mktemp /usr/local/bin/.rclone.XXXXXX)"',
            ('install -m 0755 "$tmp_dir/unpacked/${archive%.zip}/rclone" "$target_tmp"'),
            'version_output="$("$target_tmp" version)"',
            (
                f"printf '%s\\n' \"$version_output\" | head -n 1 "
                f"| grep -Fx 'rclone v{_RCLONE_VERSION}'"
            ),
            'mv -f "$target_tmp" /usr/local/bin/rclone',
            'target_tmp=""',
        ]
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

    machine_result = await session.exec("uname", "-m", shell=False, timeout=30)
    machine = machine_result.stdout.decode("utf-8", errors="replace").strip()
    arch = _rclone_arch(machine) if machine_result.ok() else None
    if arch is None:
        raise MountConfigError(
            message="rclone is not installed and this architecture is unsupported",
            context={"package": "rclone", "architecture": machine or "unknown"},
        )

    for command in _INSTALL_RCLONE_PREREQUISITES:
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

    install = await session.exec(
        "sh",
        "-lc",
        _rclone_install_command(arch, _RCLONE_SHA256_BY_ARCH[arch]),
        shell=False,
        timeout=300,
        user="root",
    )
    if install.exit_code == _RCLONE_CHECKSUM_MISMATCH_EXIT:
        raise MountConfigError(
            message="rclone archive checksum verification failed",
            context={"package": "rclone", "version": _RCLONE_VERSION, "architecture": arch},
        )
    if not install.ok():
        raise MountConfigError(
            message="failed to install rclone",
            context={
                "package": "rclone",
                "version": _RCLONE_VERSION,
                "architecture": arch,
                "exit_code": install.exit_code,
            },
        )

    rclone = await session.exec("sh", "-lc", _RCLONE_CHECK, shell=False)
    if not rclone.ok():
        raise MountConfigError(
            message="rclone was installed but is still not available on PATH",
            context={"package": "rclone", "version": _RCLONE_VERSION, "architecture": arch},
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

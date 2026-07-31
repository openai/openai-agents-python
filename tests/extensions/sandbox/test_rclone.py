from __future__ import annotations

import pytest

from agents.extensions.sandbox._rclone import (
    _RCLONE_CHECKSUM_MISMATCH_EXIT,
    _RCLONE_SHA256_BY_ARCH,
    _RCLONE_VERSION,
    _rclone_arch,
    _rclone_install_command,
    ensure_rclone,
)
from agents.sandbox.errors import MountConfigError
from agents.sandbox.types import ExecResult


def _result(*, exit_code: int = 0, stdout: bytes = b"") -> ExecResult:
    return ExecResult(stdout=stdout, stderr=b"", exit_code=exit_code)


class _FakeSession:
    def __init__(self, results: list[ExecResult]) -> None:
        self.results = results
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def exec(self, *command: str, **kwargs: object) -> ExecResult:
        self.calls.append((command, kwargs))
        return self.results.pop(0)


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "amd64"),
        ("amd64", "amd64"),
        ("i386", "386"),
        ("i686", "386"),
        ("x86", "386"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("armv7l", "arm-v7"),
        ("armv6l", "arm-v6"),
        ("armv5l", "arm"),
    ],
)
def test_rclone_arch_maps_upstream_linux_archives(machine: str, expected: str) -> None:
    assert _rclone_arch(machine) == expected


def test_rclone_arch_rejects_unknown_machine() -> None:
    assert _rclone_arch("mips64") is None


def test_rclone_install_command_pins_and_verifies_archive() -> None:
    sha256 = _RCLONE_SHA256_BY_ARCH["amd64"]

    command = _rclone_install_command("amd64", sha256)

    archive = f"rclone-v{_RCLONE_VERSION}-linux-amd64.zip"
    assert f"https://downloads.rclone.org/v{_RCLONE_VERSION}/{archive}" in command
    assert "github.com" not in command
    assert f"expected_sha256='{sha256}'" in command
    assert "sha256sum --check --strict -" in command
    assert "mktemp /usr/local/bin/.rclone.XXXXXX" in command
    verify_execution = 'version_output="$("$target_tmp" version)"'
    verify_version = f"grep -Fx 'rclone v{_RCLONE_VERSION}'"
    atomic_replace = 'mv -f "$target_tmp" /usr/local/bin/rclone'
    assert command.index(verify_execution) < command.index(verify_version)
    assert command.index(verify_version) < command.index(atomic_replace)
    assert 'mv -f "$target_tmp" /usr/local/bin/rclone' in command
    assert "curl -fsSL https://rclone.org/install.sh | bash" not in command


@pytest.mark.asyncio
async def test_ensure_rclone_preserves_preinstalled_binary() -> None:
    session = _FakeSession([_result()])

    await ensure_rclone(session)  # type: ignore[arg-type]

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_ensure_rclone_rejects_unsupported_architecture_before_install() -> None:
    session = _FakeSession(
        [
            _result(exit_code=1),
            _result(),
            _result(stdout=b"mips64\n"),
        ]
    )

    with pytest.raises(MountConfigError, match="architecture is unsupported") as exc_info:
        await ensure_rclone(session)  # type: ignore[arg-type]

    assert exc_info.value.context["architecture"] == "mips64"
    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_ensure_rclone_reports_checksum_mismatch() -> None:
    session = _FakeSession(
        [
            _result(exit_code=1),
            _result(),
            _result(stdout=b"x86_64\n"),
            _result(),
            _result(),
            _result(exit_code=_RCLONE_CHECKSUM_MISMATCH_EXIT),
        ]
    )

    with pytest.raises(MountConfigError, match="checksum verification failed") as exc_info:
        await ensure_rclone(session)  # type: ignore[arg-type]

    assert exc_info.value.context == {
        "package": "rclone",
        "version": _RCLONE_VERSION,
        "architecture": "amd64",
    }
    assert len(session.calls) == 6

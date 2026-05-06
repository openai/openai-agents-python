from __future__ import annotations

import io
import types
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import RcloneMountPattern, S3Mount
from agents.sandbox.errors import MountConfigError
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.types import ExecResult


class _FakeRunloopMountSession(BaseSandboxSession):
    def __init__(self, results: list[ExecResult] | None = None) -> None:
        self.state = cast(
            Any,
            types.SimpleNamespace(
                session_id=uuid.uuid4(),
                manifest=Manifest(root="/workspace"),
            ),
        )
        self._results = list(results or [])
        self.exec_calls: list[str] = []

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd_str = " ".join(str(c) for c in command)
        self.exec_calls.append(cmd_str)
        if self._results:
            return self._results.pop(0)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def read(self, path: Path, *, user: object = None) -> io.IOBase:
        _ = (path, user)
        return io.BytesIO(b"")

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("not expected")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise AssertionError("not expected")

    async def running(self) -> bool:
        return True


_FakeRunloopMountSession.__name__ = "RunloopSandboxSession"


def _exec_ok(stdout: bytes = b"") -> ExecResult:
    return ExecResult(stdout=stdout, stderr=b"", exit_code=0)


def _exec_fail() -> ExecResult:
    return ExecResult(stdout=b"", stderr=b"", exit_code=1)


def test_runloop_package_re_exports_cloud_bucket_strategy() -> None:
    package_module = __import__(
        "agents.extensions.sandbox.runloop",
        fromlist=["RunloopCloudBucketMountStrategy"],
    )

    assert hasattr(package_module, "RunloopCloudBucketMountStrategy")


def test_runloop_extension_re_exports_cloud_bucket_strategy() -> None:
    package_module = __import__(
        "agents.extensions.sandbox",
        fromlist=["RunloopCloudBucketMountStrategy"],
    )

    assert hasattr(package_module, "RunloopCloudBucketMountStrategy")


def test_runloop_mount_strategy_type_and_default_pattern() -> None:
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    strategy = RunloopCloudBucketMountStrategy()

    assert strategy.type == "runloop_cloud_bucket"
    assert isinstance(strategy.pattern, RcloneMountPattern)
    assert strategy.pattern.mode == "fuse"


def test_runloop_mount_strategy_round_trips_through_manifest() -> None:
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    manifest = Manifest.model_validate(
        {
            "root": "/workspace",
            "entries": {
                "bucket": {
                    "type": "s3_mount",
                    "bucket": "my-bucket",
                    "mount_strategy": {"type": "runloop_cloud_bucket"},
                }
            },
        }
    )

    mount = manifest.entries["bucket"]
    assert isinstance(mount, S3Mount)
    assert isinstance(mount.mount_strategy, RunloopCloudBucketMountStrategy)


def test_runloop_session_guard_rejects_wrong_type() -> None:
    from agents.extensions.sandbox.runloop.mounts import _assert_runloop_session

    class _WrongSession:
        pass

    with pytest.raises(MountConfigError, match="RunloopSandboxSession"):
        _assert_runloop_session(_WrongSession())  # type: ignore[arg-type]


def test_runloop_session_guard_accepts_correct_type() -> None:
    from agents.extensions.sandbox.runloop.mounts import _assert_runloop_session

    _assert_runloop_session(_FakeRunloopMountSession())


@pytest.mark.asyncio
async def test_runloop_ensure_rclone_installs_with_root_apt() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_rclone

    session = _FakeRunloopMountSession(
        [
            _exec_fail(),
            _exec_ok(),
            _exec_ok(),
            _exec_ok(),
            _exec_ok(),
        ]
    )

    await _ensure_rclone(session)

    assert session.exec_calls[:2] == [
        "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone",
        "sh -lc command -v apt-get >/dev/null 2>&1",
    ]
    assert session.exec_calls[2] == (
        "sudo -u root -- sh -lc DEBIAN_FRONTEND=noninteractive "
        "DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0 update -qq"
    )
    assert session.exec_calls[3] == (
        "sudo -u root -- sh -lc DEBIAN_FRONTEND=noninteractive "
        "DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0 install -y -qq "
        "curl unzip ca-certificates"
    )
    assert (
        session.exec_calls[4]
        == "sudo -u root -- sh -lc curl -fsSL https://rclone.org/install.sh | bash"
    )
    assert session.exec_calls[5] == (
        "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone"
    )


@pytest.mark.asyncio
async def test_runloop_ensure_fuse_installs_missing_fusermount() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_fuse_support

    session = _FakeRunloopMountSession(
        [
            _exec_ok(),
            _exec_ok(),
            _exec_fail(),
            _exec_ok(),
            _exec_ok(),
            _exec_ok(),
            _exec_ok(),
            _exec_ok(),
        ]
    )

    await _ensure_fuse_support(session)

    assert session.exec_calls == [
        "sh -lc test -c /dev/fuse",
        "sh -lc grep -qw fuse /proc/filesystems",
        "sh -lc command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1",
        "sh -lc command -v apt-get >/dev/null 2>&1",
        (
            "sudo -u root -- sh -lc DEBIAN_FRONTEND=noninteractive "
            "DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0 update -qq"
        ),
        (
            "sudo -u root -- sh -lc DEBIAN_FRONTEND=noninteractive "
            "DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0 install -y -qq fuse3"
        ),
        "sh -lc command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1",
        (
            "sudo -u root -- sh -lc chmod a+rw /dev/fuse && "
            "touch /etc/fuse.conf && "
            "(grep -qxF user_allow_other /etc/fuse.conf || "
            "printf '\\nuser_allow_other\\n' >> /etc/fuse.conf)"
        ),
    ]


@pytest.mark.asyncio
async def test_runloop_rclone_pattern_adds_fuse_access_args() -> None:
    from agents.extensions.sandbox.runloop.mounts import _rclone_pattern_for_session

    session = _FakeRunloopMountSession([_exec_ok(stdout=b"1000\n1000\n")])

    pattern = await _rclone_pattern_for_session(session, RcloneMountPattern(mode="fuse"))

    assert pattern.extra_args == ["--allow-other", "--uid", "1000", "--gid", "1000"]

from __future__ import annotations

import io
import types
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from agents.sandbox import Manifest
from agents.sandbox.entries import RcloneMountPattern, S3Mount
from agents.sandbox.entries.mounts.base import InContainerMountStrategy
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
        # A single ordered timeline shared with the delegate recorder below, so that
        # sequencing between sandbox preparation and delegation can actually be asserted.
        self.events: list[str] = []

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd_str = " ".join(str(c) for c in command)
        self.exec_calls.append(cmd_str)
        self.events.append(f"exec:{cmd_str}")
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
async def test_runloop_ensure_rclone_installs_verified_release() -> None:
    from agents.extensions.sandbox._rclone import _RCLONE_VERSION, ensure_rclone

    session = _FakeRunloopMountSession(
        [
            _exec_fail(),
            _exec_ok(),
            _exec_ok(stdout=b"aarch64\n"),
            _exec_ok(),
            _exec_ok(),
            _exec_ok(),
            _exec_ok(),
        ]
    )

    await ensure_rclone(session)

    assert session.exec_calls[:2] == [
        "sh -lc command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone",
        "sh -lc command -v apt-get >/dev/null 2>&1",
    ]
    assert session.exec_calls[2] == "uname -m"
    assert session.exec_calls[3] == (
        "sudo -u root -- sh -lc DEBIAN_FRONTEND=noninteractive "
        "DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0 update -qq"
    )
    assert session.exec_calls[4] == (
        "sudo -u root -- sh -lc DEBIAN_FRONTEND=noninteractive "
        "DEBCONF_NOWARNINGS=yes apt-get -o Dpkg::Use-Pty=0 install -y -qq "
        "ca-certificates coreutils curl unzip"
    )
    assert session.exec_calls[5].startswith("sudo -u root -- sh -lc set -eu\n")
    assert f"rclone-v{_RCLONE_VERSION}-linux-arm64.zip" in session.exec_calls[5]
    assert "sha256sum --check --strict -" in session.exec_calls[5]
    assert session.exec_calls[6] == (
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
    from agents.extensions.sandbox._rclone import rclone_pattern_for_session

    session = _FakeRunloopMountSession([_exec_ok(stdout=b"1000\n1000\n")])

    pattern = await rclone_pattern_for_session(session, RcloneMountPattern(mode="fuse"))

    assert pattern.extra_args == ["--allow-other", "--uid", "1000", "--gid", "1000"]


def _bucket_mount() -> S3Mount:
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    return S3Mount(bucket="my-bucket", mount_strategy=RunloopCloudBucketMountStrategy())


class _RecordingDelegate:
    """Records delegate calls onto the session's own timeline.

    Recording into ``session.events`` rather than a separate list is what makes the
    ordering assertions real: if delegation were moved ahead of FUSE or rclone
    preparation, the delegate entry would no longer be last in the timeline.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, RcloneMountPattern]] = []
        recorder = self

        def _make(name: str) -> Any:
            async def _hook(
                self: Any,
                mount: Any,
                session: Any,
                *args: Any,
            ) -> list[Any]:
                _ = (mount, args)
                session.events.append(f"delegate:{name}")
                recorder.calls.append((name, self.pattern))
                return []

            return _hook

        for name in ("activate", "deactivate", "teardown_for_snapshot", "restore_after_snapshot"):
            monkeypatch.setattr(InContainerMountStrategy, name, _make(name))


@pytest.mark.asyncio
async def test_runloop_ensure_fuse_requires_dev_fuse() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_fuse_support

    session = _FakeRunloopMountSession([_exec_fail()])

    with pytest.raises(MountConfigError, match="FUSE support") as exc_info:
        await _ensure_fuse_support(session)

    assert exc_info.value.context["missing"] == "/dev/fuse"
    assert len(session.exec_calls) == 1


@pytest.mark.asyncio
async def test_runloop_ensure_fuse_requires_the_kernel_module() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_fuse_support

    session = _FakeRunloopMountSession([_exec_ok(), _exec_fail()])

    with pytest.raises(MountConfigError, match="FUSE support") as exc_info:
        await _ensure_fuse_support(session)

    assert exc_info.value.context["missing"] == "fuse in /proc/filesystems"


@pytest.mark.asyncio
async def test_runloop_ensure_fuse_requires_apt_when_fusermount_is_missing() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_fuse_support

    session = _FakeRunloopMountSession([_exec_ok(), _exec_ok(), _exec_fail(), _exec_fail()])

    with pytest.raises(MountConfigError, match="apt-get is unavailable") as exc_info:
        await _ensure_fuse_support(session)

    assert exc_info.value.context["package"] == "fuse3"


@pytest.mark.asyncio
async def test_runloop_ensure_fuse_reports_a_failed_install() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_fuse_support

    session = _FakeRunloopMountSession(
        [_exec_ok(), _exec_ok(), _exec_fail(), _exec_ok(), _exec_fail()]
    )

    with pytest.raises(MountConfigError, match="failed to install fuse3") as exc_info:
        await _ensure_fuse_support(session)

    assert exc_info.value.context["exit_code"] == 1


@pytest.mark.asyncio
async def test_runloop_ensure_fuse_raises_when_fusermount_missing_after_install() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_fuse_support

    session = _FakeRunloopMountSession(
        [_exec_ok(), _exec_ok(), _exec_fail(), _exec_ok(), _exec_ok(), _exec_ok(), _exec_fail()]
    )

    with pytest.raises(MountConfigError, match="still not available"):
        await _ensure_fuse_support(session)


@pytest.mark.asyncio
async def test_runloop_ensure_fuse_reports_a_failed_chmod() -> None:
    from agents.extensions.sandbox.runloop.mounts import _ensure_fuse_support

    session = _FakeRunloopMountSession(
        [_exec_ok(), _exec_ok(), _exec_ok(), _exec_ok(), _exec_fail()]
    )

    with pytest.raises(MountConfigError, match="/dev/fuse accessible") as exc_info:
        await _ensure_fuse_support(session)

    assert exc_info.value.context["exit_code"] == 1


@pytest.mark.asyncio
async def test_runloop_activate_prepares_fuse_and_rclone_before_delegating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    delegate = _RecordingDelegate(monkeypatch)
    session = _FakeRunloopMountSession()

    await RunloopCloudBucketMountStrategy().activate(
        _bucket_mount(), session, Path("/workspace/b"), Path("/tmp")
    )

    # Delegation is the final step, after every preparation command.
    assert session.events[-1] == "delegate:activate"
    assert session.events.count("delegate:activate") == 1
    prepared = session.events[:-1]
    assert any("/dev/fuse" in event for event in prepared)
    assert any("rclone" in event for event in prepared)
    # The delegate receives the session-resolved pattern, not the raw one.
    assert "--allow-other" in delegate.calls[0][1].extra_args


@pytest.mark.asyncio
async def test_runloop_activate_skips_fuse_setup_for_nfs_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    delegate = _RecordingDelegate(monkeypatch)
    session = _FakeRunloopMountSession()
    strategy = RunloopCloudBucketMountStrategy(pattern=RcloneMountPattern(mode="nfs"))

    await strategy.activate(_bucket_mount(), session, Path("/workspace/b"), Path("/tmp"))

    assert not any("/dev/fuse" in event for event in session.events)
    assert any("rclone" in event for event in session.events)
    # nfs still reaches the delegate, carrying the nfs pattern untouched.
    assert session.events[-1] == "delegate:activate"
    assert [name for name, _ in delegate.calls] == ["activate"]
    delegated_pattern = delegate.calls[0][1]
    assert delegated_pattern.mode == "nfs"
    assert "--allow-other" not in delegated_pattern.extra_args


@pytest.mark.asyncio
async def test_runloop_restore_after_snapshot_reprepares_the_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restored devbox may be a fresh container, so setup has to run again."""
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    delegate = _RecordingDelegate(monkeypatch)
    session = _FakeRunloopMountSession()

    await RunloopCloudBucketMountStrategy().restore_after_snapshot(
        _bucket_mount(), session, Path("/workspace/b")
    )

    assert session.events[-1] == "delegate:restore_after_snapshot"
    prepared = session.events[:-1]
    assert any("/dev/fuse" in event for event in prepared)
    assert any("rclone" in event for event in prepared)
    assert "--allow-other" in delegate.calls[0][1].extra_args


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["deactivate", "teardown_for_snapshot"])
async def test_runloop_teardown_paths_do_not_reinstall_tooling(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Unmounting must not try to install FUSE or rclone on the way out."""
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    delegate = _RecordingDelegate(monkeypatch)
    session = _FakeRunloopMountSession()
    strategy = RunloopCloudBucketMountStrategy()
    mount = _bucket_mount()

    if method == "deactivate":
        await strategy.deactivate(mount, session, Path("/workspace/b"), Path("/tmp"))
    else:
        await strategy.teardown_for_snapshot(mount, session, Path("/workspace/b"))

    assert session.events == [f"delegate:{method}"]
    assert [name for name, _ in delegate.calls] == [method]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["activate", "deactivate", "teardown_for_snapshot", "restore_after_snapshot"]
)
async def test_runloop_strategy_rejects_a_foreign_session_before_touching_it(method: str) -> None:
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    class _WrongSession(_FakeRunloopMountSession):
        pass

    _WrongSession.__name__ = "NotARunloopSession"
    session = _WrongSession()
    strategy = RunloopCloudBucketMountStrategy()
    mount = _bucket_mount()

    with pytest.raises(MountConfigError, match="RunloopSandboxSession"):
        if method == "activate":
            await strategy.activate(mount, session, Path("/w/b"), Path("/tmp"))
        elif method == "deactivate":
            await strategy.deactivate(mount, session, Path("/w/b"), Path("/tmp"))
        elif method == "teardown_for_snapshot":
            await strategy.teardown_for_snapshot(mount, session, Path("/w/b"))
        else:
            await strategy.restore_after_snapshot(mount, session, Path("/w/b"))

    # The guard runs before anything is issued to the foreign sandbox.
    assert session.events == []


def test_runloop_strategy_has_no_docker_volume_driver_config() -> None:
    from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy

    assert RunloopCloudBucketMountStrategy().build_docker_volume_driver_config(_bucket_mount()) is (
        None
    )

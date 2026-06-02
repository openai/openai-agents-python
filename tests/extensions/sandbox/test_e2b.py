from __future__ import annotations

import asyncio
import base64
import builtins
import inspect
import io
import logging
import shlex
import tarfile
import uuid
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import Field, PrivateAttr

import agents.extensions.sandbox.e2b.sandbox as e2b_module
from agents.extensions.sandbox.e2b.mounts import (
    E2BCloudBucketMountStrategy,
    _assert_e2b_session,
    _ensure_fuse_support,
    _ensure_rclone,
    _rclone_pattern_for_session,
)
from agents.extensions.sandbox.e2b.sandbox import (
    E2BSandboxClient,
    E2BSandboxClientOptions,
    E2BSandboxSession,
    E2BSandboxSessionState,
)
from agents.sandbox import Manifest
from agents.sandbox.entries import (
    Dir,
    InContainerMountStrategy,
    Mount,
    MountpointMountPattern,
    RcloneMountPattern,
    S3Mount,
)
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    InvalidManifestPathError,
    MountConfigError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceStartError,
)
from agents.sandbox.files import EntryKind
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.session.runtime_helpers import (
    RESOLVE_WORKSPACE_PATH_HELPER,
    WORKSPACE_FINGERPRINT_HELPER,
)
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExecResult, User


def test_e2b_package_re_exports_backend_symbols() -> None:
    package_module = __import__(
        "agents.extensions.sandbox.e2b",
        fromlist=["E2BCloudBucketMountStrategy", "E2BSandboxClient"],
    )

    assert package_module.E2BCloudBucketMountStrategy is E2BCloudBucketMountStrategy
    assert package_module.E2BSandboxClient is E2BSandboxClient


def test_e2b_extension_re_exports_cloud_bucket_strategy() -> None:
    package_module = __import__(
        "agents.extensions.sandbox",
        fromlist=["E2BCloudBucketMountStrategy"],
    )

    assert package_module.E2BCloudBucketMountStrategy is E2BCloudBucketMountStrategy


def test_e2b_mount_strategy_type_and_default_pattern() -> None:
    strategy = E2BCloudBucketMountStrategy()

    assert strategy.type == "e2b_cloud_bucket"
    assert isinstance(strategy.pattern, RcloneMountPattern)
    assert strategy.pattern.mode == "fuse"


def test_e2b_mount_strategy_round_trips_through_manifest() -> None:
    manifest = Manifest.model_validate(
        {
            "root": "/workspace",
            "entries": {
                "bucket": {
                    "type": "s3_mount",
                    "bucket": "my-bucket",
                    "mount_strategy": {"type": "e2b_cloud_bucket"},
                }
            },
        }
    )

    mount = manifest.entries["bucket"]
    assert isinstance(mount, S3Mount)
    assert isinstance(mount.mount_strategy, E2BCloudBucketMountStrategy)


def test_e2b_session_guard_rejects_wrong_type() -> None:
    class _WrongSession:
        pass

    with pytest.raises(MountConfigError, match="E2BSandboxSession"):
        _assert_e2b_session(_WrongSession())  # type: ignore[arg-type]


def test_e2b_session_guard_accepts_correct_type() -> None:
    _assert_e2b_session(_FakeMountSession())


@pytest.mark.asyncio
async def test_e2b_ensure_fuse_uses_root_chmod() -> None:
    session = _FakeMountSession([_exec_ok(), _exec_ok()])

    await _ensure_fuse_support(session)

    assert session.exec_calls == [
        (
            "sh -lc test -c /dev/fuse && grep -qw fuse /proc/filesystems && "
            "(command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1)"
        ),
        (
            "sudo -u root -- sh -lc chmod a+rw /dev/fuse && "
            "touch /etc/fuse.conf && "
            "(grep -qxF user_allow_other /etc/fuse.conf || "
            "printf '\\nuser_allow_other\\n' >> /etc/fuse.conf)"
        ),
    ]


@pytest.mark.asyncio
async def test_e2b_ensure_rclone_installs_with_root_apt() -> None:
    session = _FakeMountSession(
        [
            _exec_fail(),  # rclone missing
            _exec_ok(),  # apt-get present
            _exec_ok(),  # apt-get update succeeds
            _exec_ok(),  # package install succeeds
            _exec_ok(),  # upstream rclone install succeeds
            _exec_ok(),  # rclone now present
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
async def test_e2b_rclone_pattern_adds_fuse_access_args() -> None:
    session = _FakeMountSession([_exec_ok(stdout=b"1000\n1000\n")])

    pattern = await _rclone_pattern_for_session(session, RcloneMountPattern(mode="fuse"))

    assert pattern.extra_args == ["--allow-other", "--uid", "1000", "--gid", "1000"]


@pytest.mark.asyncio
async def test_e2b_rclone_pattern_preserves_explicit_access_args() -> None:
    session = _FakeMountSession([_exec_ok(stdout=b"1000\n1000\n")])
    source_pattern = RcloneMountPattern(
        mode="fuse",
        extra_args=["--allow-other", "--uid", "123", "--gid", "456", "--buffer-size", "0"],
    )

    pattern = await _rclone_pattern_for_session(session, source_pattern)

    assert pattern.extra_args == [
        "--allow-other",
        "--uid",
        "123",
        "--gid",
        "456",
        "--buffer-size",
        "0",
    ]


class _FakeE2BResult:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _FakeE2BFiles:
    def __init__(self) -> None:
        self.make_dir_calls: list[tuple[str, float | None]] = []

    async def write(
        self,
        path: str,
        data: bytes,
        request_timeout: float | None = None,
    ) -> None:
        _ = (path, data, request_timeout)

    async def remove(self, path: str, request_timeout: float | None = None) -> None:
        _ = (path, request_timeout)

    async def make_dir(self, path: str, request_timeout: float | None = None) -> bool:
        self.make_dir_calls.append((path, request_timeout))
        return True

    async def read(self, path: str, format: str = "bytes") -> bytes:
        _ = (path, format)
        return b""


class _FakeE2BCommands:
    def __init__(self) -> None:
        self.exec_root_ready = False
        self.calls: list[dict[str, object]] = []
        self.mkdir_result: _FakeE2BResult | None = None
        self.next_result = _FakeE2BResult()
        self.background_calls: list[dict[str, object]] = []
        self.background_error: BaseException | None = None

    async def run(
        self,
        command: str,
        background: bool | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: object | None = None,
        on_stderr: object | None = None,
        stdin: bool | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> _FakeE2BResult:
        _ = request_timeout
        if background:
            if self.background_error is not None:
                raise self.background_error
            _ = on_stderr
            self.background_calls.append(
                {
                    "command": command,
                    "timeout": timeout,
                    "cwd": cwd,
                    "envs": envs,
                    "stdin": stdin,
                    "background": background,
                }
            )
            if callable(on_stdout):
                result = on_stdout("started\n")
                if inspect.isawaitable(result):
                    await result

            class _Handle:
                exit_code = 0

                async def kill(self) -> None:
                    return None

            return cast(_FakeE2BResult, _Handle())

        self.calls.append(
            {
                "command": command,
                "timeout": timeout,
                "cwd": cwd,
                "envs": envs,
                "user": user,
            }
        )
        parts = shlex.split(command)
        if _is_helper_install_command(command):
            return _FakeE2BResult()
        if _is_helper_present_command(command):
            return _FakeE2BResult()
        if parts and parts[0] == str(RESOLVE_WORKSPACE_PATH_HELPER.install_path):
            return _FakeE2BResult(stdout=parts[2])
        if parts and parts[0] == str(WORKSPACE_FINGERPRINT_HELPER.install_path):
            return _FakeE2BResult(
                stdout='{"fingerprint":"fake-workspace-fingerprint","version":"workspace_tar_sha256_v1"}\n'
            )
        if command == "test -d /workspace" and cwd in (None, "/"):
            exit_code = 0 if self.exec_root_ready else 1
            return _FakeE2BResult(exit_code=exit_code)
        if command == "mkdir -p -- /workspace" and cwd == "/":
            result = self.mkdir_result or _FakeE2BResult()
            if result.exit_code == 0:
                self.exec_root_ready = True
            self.mkdir_result = None
            return result
        if cwd == "/workspace" and not self.exec_root_ready:
            raise ValueError("cwd '/workspace' does not exist")
        result = self.next_result
        self.next_result = _FakeE2BResult()
        return result


class _FakeE2BPtyHandle:
    def __init__(self) -> None:
        self.pid = "pty-123"
        self.exit_code: int | None = None
        self.stdin_payloads: list[bytes] = []

    async def kill(self) -> None:
        self.exit_code = 0


class _FakeE2BPty:
    def __init__(self) -> None:
        self.handle = _FakeE2BPtyHandle()
        self.on_data: object | None = None
        self.create_error: BaseException | None = None
        self.send_stdin_error: BaseException | None = None

    async def create(
        self,
        *,
        size: object,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        on_data: object | None = None,
    ) -> _FakeE2BPtyHandle:
        _ = (size, cwd, envs, timeout)
        if self.create_error is not None:
            raise self.create_error
        self.on_data = on_data
        return self.handle

    async def send_stdin(
        self,
        pid: object,
        data: bytes,
        request_timeout: float | None = None,
    ) -> None:
        _ = (pid, request_timeout)
        if self.send_stdin_error is not None:
            raise self.send_stdin_error
        self.handle.stdin_payloads.append(data)
        if callable(self.on_data):
            payload = b">>> " if len(self.handle.stdin_payloads) == 1 else b"10\n"
            result = self.on_data(payload)
            if inspect.isawaitable(result):
                await result


class _FakeE2BSandbox:
    def __init__(self) -> None:
        self.sandbox_id = "sb-123"
        self.files = _FakeE2BFiles()
        self.commands = _FakeE2BCommands()
        self.pty = _FakeE2BPty()
        self.created_snapshot_id = "snap-123"
        self.pause_error: BaseException | None = None
        self.kill_error: BaseException | None = None
        self.pause_calls = 0
        self.kill_calls = 0

    async def pause(self) -> None:
        self.pause_calls += 1
        if self.pause_error is not None:
            raise self.pause_error
        return

    async def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        return

    async def is_running(self, request_timeout: float | None = None) -> bool:
        _ = request_timeout
        return True

    def get_host(self, port: int) -> str:
        return f"{port}-{self.sandbox_id}.sandbox.example.test"

    async def create_snapshot(self) -> object:
        return type("SnapshotInfo", (), {"snapshot_id": self.created_snapshot_id})()


class _FakeMountSession(BaseSandboxSession):
    __name__ = "E2BSandboxSession"

    def __init__(self, results: list[ExecResult] | None = None) -> None:
        self.state = E2BSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id="sb-123",
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

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        _ = (path, user)
        return io.BytesIO(b"")

    async def write(self, path: Path, data: io.IOBase, *, user: str | User | None = None) -> None:
        _ = (path, data, user)

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("not expected")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise AssertionError("not expected")

    async def running(self) -> bool:
        return True


_FakeMountSession.__name__ = "E2BSandboxSession"


def _exec_ok(stdout: bytes = b"") -> ExecResult:
    return ExecResult(stdout=stdout, stderr=b"", exit_code=0)


def _exec_fail() -> ExecResult:
    return ExecResult(stdout=b"", stderr=b"", exit_code=1)


class _RestorableSnapshot(SnapshotBase):
    type: Literal["test-restorable-e2b"] = "test-restorable-e2b"
    payload: bytes = b"restored"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO(self.payload)

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return True


class _RecordingMount(Mount):
    type: str = "recording_mount"
    mount_strategy: InContainerMountStrategy = Field(
        default_factory=lambda: InContainerMountStrategy(pattern=MountpointMountPattern())
    )
    _mounted_paths: list[Path] = PrivateAttr(default_factory=list)
    _unmounted_paths: list[Path] = PrivateAttr(default_factory=list)
    _events: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    def bind_events(self, events: list[tuple[str, str]]) -> _RecordingMount:
        self._events = events
        return self

    def supported_in_container_patterns(
        self,
    ) -> tuple[builtins.type[MountpointMountPattern], ...]:
        return (MountpointMountPattern,)

    def build_docker_volume_driver_config(
        self,
        strategy: object,
    ) -> tuple[str, dict[str, str], bool]:
        _ = strategy
        raise MountConfigError(
            message="docker-volume mounts are not supported for this mount type",
            context={"mount_type": self.type},
        )

    def in_container_adapter(self) -> InContainerMountAdapter:
        mount = self

        class _Adapter(InContainerMountAdapter):
            def validate(self, strategy: InContainerMountStrategy) -> None:
                _ = strategy

            async def activate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> list[MaterializedFile]:
                _ = (strategy, session, base_dir)
                path = mount._resolve_mount_path(session, dest)
                mount._events.append(("mount", path.as_posix()))
                mount._mounted_paths.append(path)
                return []

            async def deactivate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> None:
                _ = (strategy, session, base_dir)
                path = mount._resolve_mount_path(session, dest)
                mount._events.append(("unmount", path.as_posix()))
                mount._unmounted_paths.append(path)

            async def teardown_for_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                mount._events.append(("unmount", path.as_posix()))
                mount._unmounted_paths.append(path)

            async def restore_after_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                mount._events.append(("mount", path.as_posix()))
                mount._mounted_paths.append(path)

        return _Adapter(self)


class _FailingUnmountMount(_RecordingMount):
    type: str = "failing_unmount_mount"

    def in_container_adapter(self) -> InContainerMountAdapter:
        mount = self
        base_adapter = super().in_container_adapter()

        class _Adapter(InContainerMountAdapter):
            def validate(self, strategy: InContainerMountStrategy) -> None:
                base_adapter.validate(strategy)

            async def activate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> list[MaterializedFile]:
                return await base_adapter.activate(strategy, session, dest, base_dir)

            async def deactivate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> None:
                _ = (strategy, session, base_dir)
                path = mount._resolve_mount_path(session, dest)
                mount._events.append(("unmount_fail", path.as_posix()))
                raise RuntimeError("boom while unmounting second mount")

            async def teardown_for_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                mount._events.append(("unmount_fail", path.as_posix()))
                raise RuntimeError("boom while unmounting second mount")

            async def restore_after_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                await base_adapter.restore_after_snapshot(strategy, session, path)

        return _Adapter(self)


class _FailingRemountMount(_RecordingMount):
    type: str = "failing_remount_mount"

    def in_container_adapter(self) -> InContainerMountAdapter:
        mount = self
        base_adapter = super().in_container_adapter()

        class _Adapter(InContainerMountAdapter):
            def validate(self, strategy: InContainerMountStrategy) -> None:
                base_adapter.validate(strategy)

            async def activate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> list[MaterializedFile]:
                _ = (strategy, session, base_dir)
                path = mount._resolve_mount_path(session, dest)
                mount._events.append(("mount_fail", path.as_posix()))
                raise RuntimeError("boom while remounting second mount")

            async def deactivate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> None:
                return await base_adapter.deactivate(strategy, session, dest, base_dir)

            async def teardown_for_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                await base_adapter.teardown_for_snapshot(strategy, session, path)

            async def restore_after_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                mount._events.append(("mount_fail", path.as_posix()))
                raise RuntimeError("boom while remounting second mount")

        return _Adapter(self)


def _session(
    *,
    workspace_root_ready: bool = False,
    exposed_ports: tuple[int, ...] = (),
) -> tuple[E2BSandboxSession, _FakeE2BSandbox]:
    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=workspace_root_ready,
        exposed_ports=exposed_ports,
    )
    return E2BSandboxSession.from_state(state, sandbox=sandbox), sandbox


def _tar_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("note.txt")
        payload = b"hello"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_e2b_sandbox_connect_prefers_full_sandbox_wrapper() -> None:
    class _FakeSandboxClass:
        calls: list[tuple[str, str, int | None]] = []

        @classmethod
        async def connect(cls, *, sandbox_id: str, timeout: int | None = None) -> str:
            cls.calls.append(("connect", sandbox_id, timeout))
            return "full-sandbox-wrapper"

        @classmethod
        async def _cls_connect_sandbox(cls, *, sandbox_id: str, timeout: int | None = None) -> str:
            cls.calls.append(("_cls_connect_sandbox", sandbox_id, timeout))
            return "private-full-sandbox-wrapper"

        @classmethod
        async def _cls_connect(cls, *, sandbox_id: str, timeout: int | None = None) -> str:
            cls.calls.append(("_cls_connect", sandbox_id, timeout))
            return "low-level-api-model"

    connected = await e2b_module._sandbox_connect(
        cast(e2b_module._E2BSandboxFactoryAPI, _FakeSandboxClass),
        sandbox_id="sb-123",
        timeout=300,
    )

    assert connected == "full-sandbox-wrapper"
    assert _FakeSandboxClass.calls == [("connect", "sb-123", 300)]


def test_e2b_import_resolves_sdk_sandbox_classes_for_canonical_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports: list[str] = []

    real_import = builtins.__import__

    def _fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "e2b_code_interpreter":
            imports.append(name)
            return type("FakeCodeInterpreterModule", (), {"AsyncSandbox": object()})()
        if name == "e2b":
            imports.append(name)
            return type("FakeE2BModule", (), {"AsyncSandbox": object()})()
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    assert e2b_module._import_sandbox_class(e2b_module.E2BSandboxType.CODE_INTERPRETER) is not None
    assert e2b_module._import_sandbox_class(e2b_module.E2BSandboxType.E2B) is not None
    assert imports == ["e2b_code_interpreter", "e2b"]


def _visible_command_calls(sandbox: _FakeE2BSandbox) -> list[dict[str, object]]:
    return [
        call
        for call in sandbox.commands.calls
        if not _is_helper_install_command(str(call["command"]))
        and not _is_helper_present_command(str(call["command"]))
        and not _is_helper_invoke_command(str(call["command"]))
    ]


def _is_helper_install_command(command: str) -> bool:
    return RESOLVE_WORKSPACE_PATH_HELPER.install_marker in command


def _is_helper_invoke_command(command: str) -> bool:
    parts = shlex.split(command)
    return bool(parts) and parts[0].startswith("/tmp/openai-agents/bin/")


def _is_helper_present_command(command: str) -> bool:
    parts = shlex.split(command)
    return (
        len(parts) == 3
        and parts[:2] == ["test", "-x"]
        and parts[2].startswith("/tmp/openai-agents/bin/")
    )


@pytest.mark.asyncio
async def test_e2b_exec_omits_cwd_until_workspace_ready() -> None:
    session, sandbox = _session(workspace_root_ready=False)

    result = await session._exec_internal("find", ".", timeout=0.01)  # noqa: SLF001

    assert result.ok()
    assert sandbox.commands.calls == [
        {
            "command": "find .",
            "timeout": 0.01,
            "cwd": None,
            "envs": {},
            "user": None,
        }
    ]


@pytest.mark.asyncio
async def test_e2b_exec_uses_manifest_root_after_workspace_ready() -> None:
    session, sandbox = _session(workspace_root_ready=True)
    sandbox.commands.exec_root_ready = True

    result = await session._exec_internal("find", ".", timeout=0.01)  # noqa: SLF001

    assert result.ok()
    assert sandbox.commands.calls == [
        {
            "command": "find .",
            "timeout": 0.01,
            "cwd": "/workspace",
            "envs": {},
            "user": None,
        }
    ]


@pytest.mark.asyncio
async def test_e2b_start_prepares_workspace_root_for_command_cwd() -> None:
    session, sandbox = _session(workspace_root_ready=False)

    await session.start()
    result = await session._exec_internal("pwd", timeout=0.01)  # noqa: SLF001

    assert result.ok()
    assert session.state.workspace_root_ready is True
    assert session._workspace_root_ready is True  # noqa: SLF001
    assert _visible_command_calls(sandbox) == [
        {
            "command": "mkdir -p -- /workspace",
            "timeout": 10,
            "cwd": "/",
            "envs": {},
            "user": None,
        },
        {
            "command": "pwd",
            "timeout": 0.01,
            "cwd": "/workspace",
            "envs": {},
            "user": None,
        },
    ]


@pytest.mark.asyncio
async def test_e2b_start_installs_runtime_helpers() -> None:
    session, sandbox = _session(workspace_root_ready=False)

    await session.start()

    assert any(_is_helper_install_command(str(call["command"])) for call in sandbox.commands.calls)


@pytest.mark.asyncio
async def test_e2b_start_raises_on_nonzero_workspace_root_setup_exit() -> None:
    session, sandbox = _session(workspace_root_ready=False)
    sandbox.commands.mkdir_result = _FakeE2BResult(stderr="mkdir failed", exit_code=2)

    with pytest.raises(WorkspaceStartError) as exc_info:
        await session.start()

    assert exc_info.value.context["reason"] == "workspace_root_nonzero_exit"
    assert exc_info.value.context["exit_code"] == 2
    assert session.state.workspace_root_ready is False
    assert session._workspace_root_ready is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_e2b_preserved_start_still_prepares_workspace_root_for_resumed_exec_cwd() -> None:
    session, sandbox = _session(workspace_root_ready=False)
    session._set_start_state_preserved(True)  # noqa: SLF001

    await session.start()
    result = await session._exec_internal("pwd", timeout=0.01)  # noqa: SLF001

    assert result.ok()
    assert session.state.workspace_root_ready is True
    assert session._workspace_root_ready is True  # noqa: SLF001
    assert session._can_reuse_preserved_workspace_on_resume() is False  # noqa: SLF001
    assert session.should_provision_manifest_accounts_on_resume() is False
    assert _visible_command_calls(sandbox) == [
        {
            "command": "test -d /workspace",
            "timeout": 10.0,
            "cwd": None,
            "envs": {},
            "user": None,
        },
        {
            "command": "mkdir -p -- /workspace",
            "timeout": 10,
            "cwd": "/",
            "envs": {},
            "user": None,
        },
        {
            "command": "pwd",
            "timeout": 0.01,
            "cwd": "/workspace",
            "envs": {},
            "user": None,
        },
    ]


@pytest.mark.asyncio
async def test_e2b_preserved_start_uses_shared_resume_gate_for_restore() -> None:
    session, _sandbox = _session(workspace_root_ready=True)
    session.state.snapshot = _RestorableSnapshot(id="snapshot")
    session._set_start_state_preserved(True)  # noqa: SLF001
    events: list[object] = []

    async def _gate(*, is_running: bool) -> bool:
        events.append(("gate", is_running))
        return False

    async def _restore() -> None:
        events.append("restore")

    async def _reapply() -> None:
        events.append("reapply")

    session._can_skip_snapshot_restore_on_resume = _gate  # type: ignore[method-assign]
    session._restore_snapshot_into_workspace_on_resume = _restore  # type: ignore[method-assign]
    session._reapply_ephemeral_manifest_on_resume = _reapply  # type: ignore[method-assign]

    await session.start()

    assert session.state.workspace_root_ready is True
    assert session._workspace_root_ready is True  # noqa: SLF001
    assert events == [("gate", True), "restore", "reapply"]


@pytest.mark.asyncio
async def test_e2b_running_requires_workspace_root_ready() -> None:
    session, _sandbox = _session(workspace_root_ready=False)

    assert await session.running() is False


@pytest.mark.asyncio
async def test_e2b_running_checks_remote_after_workspace_ready() -> None:
    session, sandbox = _session(workspace_root_ready=True)
    sandbox.commands.exec_root_ready = True

    assert await session.running() is True


@pytest.mark.asyncio
async def test_e2b_resolve_exposed_port_uses_backend_host() -> None:
    session, _sandbox = _session(workspace_root_ready=True, exposed_ports=(8765,))

    endpoint = await session.resolve_exposed_port(8765)

    assert endpoint.host == "8765-sb-123.sandbox.example.test"
    assert endpoint.port == 443
    assert endpoint.tls is True


@pytest.mark.asyncio
async def test_e2b_client_create_enables_public_traffic_for_exposed_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[dict[str, object]] = []

    class _FakeSandboxFactory:
        @staticmethod
        async def create(
            *,
            template: str | None = None,
            timeout: int | None = None,
            metadata: dict[str, str] | None = None,
            envs: dict[str, str] | None = None,
            secure: bool = True,
            allow_internet_access: bool = True,
            network: dict[str, object] | None = None,
            lifecycle: dict[str, object] | None = None,
            mcp: dict[str, dict[str, str]] | None = None,
        ) -> _FakeE2BSandbox:
            _ = (
                template,
                timeout,
                metadata,
                envs,
                secure,
                allow_internet_access,
                network,
                lifecycle,
                mcp,
            )
            create_calls.append(
                {
                    "template": template,
                    "timeout": timeout,
                    "metadata": metadata,
                    "envs": envs,
                    "secure": secure,
                    "allow_internet_access": allow_internet_access,
                    "network": network,
                    "lifecycle": lifecycle,
                    "mcp": mcp,
                }
            )
            return _FakeE2BSandbox()

    monkeypatch.setattr(
        e2b_module, "_import_sandbox_class", lambda _sandbox_type: _FakeSandboxFactory
    )

    client = E2BSandboxClient()
    session = await client.create(
        options=E2BSandboxClientOptions(
            sandbox_type="e2b",
            exposed_ports=(8765,),
        )
    )

    assert create_calls
    assert create_calls[0]["network"] == {"allow_public_traffic": True}
    assert create_calls[0]["lifecycle"] == {"on_timeout": "pause", "auto_resume": True}
    assert isinstance(session.state, E2BSandboxSessionState)
    assert session.state.exposed_ports == (8765,)
    assert session.state.on_timeout == "pause"
    assert session.state.auto_resume is True


@pytest.mark.asyncio
async def test_e2b_client_create_omits_auto_resume_for_kill_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[dict[str, object]] = []

    class _FakeSandboxFactory:
        @staticmethod
        async def create(
            *,
            template: str | None = None,
            timeout: int | None = None,
            metadata: dict[str, str] | None = None,
            envs: dict[str, str] | None = None,
            secure: bool = True,
            allow_internet_access: bool = True,
            network: dict[str, object] | None = None,
            lifecycle: dict[str, object] | None = None,
            mcp: dict[str, dict[str, str]] | None = None,
        ) -> _FakeE2BSandbox:
            _ = (
                template,
                timeout,
                metadata,
                envs,
                secure,
                allow_internet_access,
                network,
                lifecycle,
                mcp,
            )
            create_calls.append({"lifecycle": lifecycle})
            return _FakeE2BSandbox()

    monkeypatch.setattr(
        e2b_module, "_import_sandbox_class", lambda _sandbox_type: _FakeSandboxFactory
    )

    client = E2BSandboxClient()
    session = await client.create(
        options=E2BSandboxClientOptions(
            sandbox_type="e2b",
            on_timeout="kill",
        )
    )

    assert create_calls == [{"lifecycle": {"on_timeout": "kill"}}]
    assert isinstance(session.state, E2BSandboxSessionState)
    assert session.state.on_timeout == "kill"
    assert session.state.auto_resume is True


@pytest.mark.asyncio
async def test_e2b_client_create_passes_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[dict[str, object]] = []

    class _FakeSandboxFactory:
        @staticmethod
        async def create(
            *,
            template: str | None = None,
            timeout: int | None = None,
            metadata: dict[str, str] | None = None,
            envs: dict[str, str] | None = None,
            secure: bool = True,
            allow_internet_access: bool = True,
            network: dict[str, object] | None = None,
            lifecycle: dict[str, object] | None = None,
            mcp: dict[str, dict[str, str]] | None = None,
        ) -> _FakeE2BSandbox:
            _ = (
                template,
                timeout,
                metadata,
                envs,
                secure,
                allow_internet_access,
                network,
                lifecycle,
                mcp,
            )
            create_calls.append({"mcp": mcp})
            return _FakeE2BSandbox()

    monkeypatch.setattr(
        e2b_module, "_import_sandbox_class", lambda _sandbox_type: _FakeSandboxFactory
    )

    client = E2BSandboxClient()
    await client.create(
        options=E2BSandboxClientOptions(
            sandbox_type="e2b",
            mcp={
                "exa": {"apiKey": "exa-key"},
                "browserbase": {
                    "apiKey": "browserbase-key",
                    "geminiApiKey": "gemini-key",
                    "projectId": "project-id",
                },
            },
        )
    )

    assert create_calls == [
        {
            "mcp": {
                "exa": {"apiKey": "exa-key"},
                "browserbase": {
                    "apiKey": "browserbase-key",
                    "geminiApiKey": "gemini-key",
                    "projectId": "project-id",
                },
            }
        }
    ]


def test_e2b_deserialize_session_state_defaults_missing_mcp() -> None:
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sb-123",
        mcp={"exa": {"apiKey": "exa-key"}},
    )
    payload = state.model_dump(mode="python")
    payload.pop("mcp")

    restored = E2BSandboxClient().deserialize_session_state(cast(dict[str, object], payload))

    assert isinstance(restored, E2BSandboxSessionState)
    assert restored.mcp is None


def test_e2b_client_options_preserves_positional_exposed_ports() -> None:
    options = E2BSandboxClientOptions(
        "e2b",
        None,
        None,
        None,
        None,
        True,
        True,
        None,
        False,
        (8765,),
    )

    assert options.exposed_ports == (8765,)
    assert options.workspace_persistence == "tar"
    assert options.on_timeout == "pause"
    assert options.auto_resume is True


@pytest.mark.asyncio
async def test_e2b_resume_reuses_paused_timeout_lifecycle_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    connected: list[tuple[str, int | None]] = []

    class _FakeSandboxFactory:
        @staticmethod
        async def create(**kwargs: object) -> _FakeE2BSandbox:
            created.append(dict(kwargs))
            return _FakeE2BSandbox()

        @staticmethod
        async def connect(*, sandbox_id: str, timeout: int | None = None) -> _FakeE2BSandbox:
            connected.append((sandbox_id, timeout))
            sandbox = _FakeE2BSandbox()
            sandbox.sandbox_id = sandbox_id
            return sandbox

    monkeypatch.setattr(
        e2b_module, "_import_sandbox_class", lambda _sandbox_type: _FakeSandboxFactory
    )

    client = E2BSandboxClient()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sb-paused",
        sandbox_timeout=15,
        on_timeout="pause",
        auto_resume=True,
        pause_on_exit=False,
    )

    resumed = await client.resume(state)

    assert connected == [("sb-paused", 15)]
    assert created == []
    assert isinstance(resumed.state, E2BSandboxSessionState)
    assert resumed.state.sandbox_id == "sb-paused"
    assert isinstance(resumed._inner, E2BSandboxSession)
    assert resumed._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
    assert resumed._inner._system_state_preserved_on_start() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_e2b_resume_reuses_live_kill_timeout_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    connected: list[tuple[str, int | None]] = []

    class _LiveSandbox(_FakeE2BSandbox):
        async def is_running(self, request_timeout: float | None = None) -> bool:
            _ = request_timeout
            return True

    class _FakeSandboxFactory:
        @staticmethod
        async def create(**kwargs: object) -> _FakeE2BSandbox:
            created.append(dict(kwargs))
            return _FakeE2BSandbox()

        @staticmethod
        async def connect(*, sandbox_id: str, timeout: int | None = None) -> _LiveSandbox:
            connected.append((sandbox_id, timeout))
            sandbox = _LiveSandbox()
            sandbox.sandbox_id = sandbox_id
            return sandbox

    monkeypatch.setattr(
        e2b_module, "_import_sandbox_class", lambda _sandbox_type: _FakeSandboxFactory
    )

    client = E2BSandboxClient()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sb-live",
        sandbox_timeout=15,
        workspace_root_ready=True,
        on_timeout="kill",
        auto_resume=True,
        pause_on_exit=False,
    )

    resumed = await client.resume(state)

    assert connected == [("sb-live", 15)]
    assert created == []
    assert isinstance(resumed.state, E2BSandboxSessionState)
    assert resumed.state.sandbox_id == "sb-live"
    assert resumed._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
    assert resumed._inner._system_state_preserved_on_start() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_e2b_resume_recreates_dead_kill_timeout_sandbox_and_preserves_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    connected: list[tuple[str, int | None]] = []

    class _DeadSandbox(_FakeE2BSandbox):
        async def is_running(self, request_timeout: float | None = None) -> bool:
            _ = request_timeout
            return False

    class _CreatedSandbox(_FakeE2BSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.sandbox_id = "sb-recreated"

    class _FakeSandboxFactory:
        @staticmethod
        async def create(
            *,
            template: str | None = None,
            timeout: int | None = None,
            metadata: dict[str, str] | None = None,
            envs: dict[str, str] | None = None,
            secure: bool = True,
            allow_internet_access: bool = True,
            network: dict[str, object] | None = None,
            lifecycle: dict[str, object] | None = None,
            mcp: dict[str, dict[str, str]] | None = None,
        ) -> _CreatedSandbox:
            _ = (
                template,
                timeout,
                metadata,
                envs,
                secure,
                allow_internet_access,
                network,
                lifecycle,
                mcp,
            )
            created.append(
                {
                    "template": template,
                    "timeout": timeout,
                    "metadata": metadata,
                    "envs": envs,
                    "secure": secure,
                    "allow_internet_access": allow_internet_access,
                    "network": network,
                    "lifecycle": lifecycle,
                    "mcp": mcp,
                }
            )
            return _CreatedSandbox()

        @staticmethod
        async def connect(*, sandbox_id: str, timeout: int | None = None) -> _DeadSandbox:
            connected.append((sandbox_id, timeout))
            sandbox = _DeadSandbox()
            sandbox.sandbox_id = sandbox_id
            return sandbox

    monkeypatch.setattr(
        e2b_module, "_import_sandbox_class", lambda _sandbox_type: _FakeSandboxFactory
    )

    client = E2BSandboxClient()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id="sb-dead",
        sandbox_timeout=15,
        workspace_root_ready=True,
        on_timeout="kill",
        auto_resume=True,
        pause_on_exit=False,
        mcp={"exa": {"apiKey": "exa-key"}},
    )

    resumed = await client.resume(state)

    assert connected == [("sb-dead", 15)]
    assert created == [
        {
            "template": None,
            "timeout": 15,
            "metadata": None,
            "envs": None,
            "secure": True,
            "allow_internet_access": True,
            "network": None,
            "lifecycle": {"on_timeout": "kill"},
            "mcp": {"exa": {"apiKey": "exa-key"}},
        }
    ]
    assert isinstance(resumed.state, E2BSandboxSessionState)
    assert resumed.state.sandbox_id == "sb-recreated"
    assert resumed.state.workspace_root_ready is False
    assert resumed._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001
    assert resumed._inner._system_state_preserved_on_start() is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_e2b_normalize_path_preserves_safe_leaf_symlink_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _sandbox = _session(workspace_root_ready=True)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        if (
            rendered[:2] == ["sh", "-c"]
            and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in rendered[2]
        ):
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered and rendered[0] == str(RESOLVE_WORKSPACE_PATH_HELPER.install_path):
            return ExecResult(stdout=b"/workspace/target.txt", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    normalized = await session._validate_path_access("link.txt")  # noqa: SLF001

    assert normalized == Path("/workspace/link.txt")


@pytest.mark.asyncio
async def test_e2b_normalize_path_rejects_symlink_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _sandbox = _session(workspace_root_ready=True)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        if (
            rendered[:2] == ["sh", "-c"]
            and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in rendered[2]
        ):
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered and rendered[0] == str(RESOLVE_WORKSPACE_PATH_HELPER.install_path):
            return ExecResult(stdout=b"", stderr=b"workspace escape", exit_code=111)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session._validate_path_access("link/secret.txt")  # noqa: SLF001


@pytest.mark.asyncio
async def test_e2b_persist_workspace_raises_on_nonzero_snapshot_exit() -> None:
    session, sandbox = _session(workspace_root_ready=True)
    sandbox.commands.exec_root_ready = True
    sandbox.commands.next_result = _FakeE2BResult(stderr="tar failed", exit_code=2)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context["reason"] == "snapshot_nonzero_exit"
    assert exc_info.value.context["exit_code"] == 2


@pytest.mark.asyncio
async def test_e2b_persist_workspace_excludes_runtime_skip_paths() -> None:
    session, sandbox = _session(workspace_root_ready=True)
    sandbox.commands.exec_root_ready = True
    session.register_persist_workspace_skip_path(Path("logs/events.jsonl"))
    sandbox.commands.next_result = _FakeE2BResult(
        stdout=base64.b64encode(b"fake-tar-bytes").decode("ascii")
    )

    archive = await session.persist_workspace()

    assert archive.read() == b"fake-tar-bytes"
    expected_command = (
        "tar --exclude=logs/events.jsonl --exclude=./logs/events.jsonl "
        "-C /workspace -cf - . | base64 -w0"
    )
    assert sandbox.commands.calls == [
        {
            "command": expected_command,
            "timeout": session.state.timeouts.snapshot_tar_s,
            "cwd": "/",
            "envs": {},
            "user": None,
        }
    ]


@pytest.mark.asyncio
async def test_e2b_persist_workspace_native_snapshot_returns_snapshot_ref() -> None:
    session, sandbox = _session(workspace_root_ready=True)
    session.state.workspace_persistence = "snapshot"

    archive = await session.persist_workspace()

    assert archive.read() == e2b_module._encode_e2b_snapshot_ref(snapshot_id="snap-123")
    assert sandbox.commands.calls == []


@pytest.mark.asyncio
async def test_e2b_persist_workspace_native_snapshot_times_out_and_remounts_mounts() -> None:
    events: list[tuple[str, str]] = []
    mount = _RecordingMount().bind_events(events)

    class _SlowSnapshotSandbox(_FakeE2BSandbox):
        async def create_snapshot(self) -> object:
            await asyncio.sleep(0.2)
            return await super().create_snapshot()

    sandbox = _SlowSnapshotSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace", entries={"mount": mount}),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
        workspace_persistence="snapshot",
    )
    state.timeouts.snapshot_tar_s = 0.01
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context["reason"] == "native_snapshot_failed"
    assert type(exc_info.value.cause).__name__ == "TimeoutError"
    assert events == [
        ("unmount", "/workspace/mount"),
        ("mount", "/workspace/mount"),
    ]


@pytest.mark.asyncio
async def test_e2b_persist_workspace_native_snapshot_falls_back_to_tar_for_plain_skip_paths() -> (
    None
):
    session, sandbox = _session(workspace_root_ready=True)
    session.state.workspace_persistence = "snapshot"
    session.register_persist_workspace_skip_path(Path("logs/events.jsonl"))
    sandbox.commands.exec_root_ready = True
    sandbox.commands.next_result = _FakeE2BResult(
        stdout=base64.b64encode(b"fake-tar-bytes").decode("ascii")
    )

    archive = await session.persist_workspace()

    assert archive.read() == b"fake-tar-bytes"
    assert sandbox.commands.calls


@pytest.mark.asyncio
async def test_e2b_hydrate_workspace_native_snapshot_recreates_from_snapshot_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, sandbox = _session(workspace_root_ready=True)
    session.state.workspace_persistence = "snapshot"
    session.state.mcp = {"exa": {"apiKey": "exa-key"}}

    created: list[dict[str, object]] = []

    class _CreatedSandbox(_FakeE2BSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.sandbox_id = "sb-from-snapshot"

    class _FakeSandboxFactory:
        @staticmethod
        async def create(**kwargs: object) -> _CreatedSandbox:
            created.append(dict(kwargs))
            return _CreatedSandbox()

    monkeypatch.setattr(
        e2b_module, "_import_sandbox_class", lambda _sandbox_type: _FakeSandboxFactory
    )

    payload = io.BytesIO(e2b_module._encode_e2b_snapshot_ref(snapshot_id="snap-123"))

    await session.hydrate_workspace(payload)

    assert created == [
        {
            "template": "snap-123",
            "timeout": session.state.sandbox_timeout,
            "metadata": session.state.metadata,
            "envs": None,
            "secure": session.state.secure,
            "allow_internet_access": session.state.allow_internet_access,
            "network": None,
            "lifecycle": {"on_timeout": "pause", "auto_resume": True},
            "mcp": {"exa": {"apiKey": "exa-key"}},
        }
    ]
    assert session.state.sandbox_id == "sb-from-snapshot"
    assert session.state.workspace_root_ready is True


@pytest.mark.asyncio
async def test_e2b_hydrate_workspace_raises_on_nonzero_extract_exit() -> None:
    session, sandbox = _session(workspace_root_ready=False)
    sandbox.commands.next_result = _FakeE2BResult(stderr="tar failed", exit_code=2)

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.hydrate_workspace(io.BytesIO(_tar_bytes()))

    assert exc_info.value.context["reason"] == "hydrate_nonzero_exit"
    assert exc_info.value.context["exit_code"] == 2
    assert session.state.workspace_root_ready is False
    assert session._workspace_root_ready is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_e2b_persist_workspace_remounts_mounts_after_snapshot() -> None:
    mount = _RecordingMount()
    sandbox = _FakeE2BSandbox()
    sandbox.commands.exec_root_ready = True
    sandbox.commands.next_result = _FakeE2BResult(
        stdout=base64.b64encode(b"fake-tar-bytes").decode("ascii")
    )
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace", entries={"mount": mount}),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    archive = await session.persist_workspace()

    assert archive.read() == b"fake-tar-bytes"
    assert mount._unmounted_paths == [Path("/workspace/mount")]
    assert mount._mounted_paths == [Path("/workspace/mount")]


@pytest.mark.asyncio
async def test_e2b_persist_workspace_uses_nested_mount_targets_and_resolved_excludes() -> None:
    parent_mount = _RecordingMount(mount_path=Path("repo"))
    child_mount = _RecordingMount(mount_path=Path("repo/sub"))
    events: list[tuple[str, str]] = []
    sandbox = _FakeE2BSandbox()
    sandbox.commands.exec_root_ready = True
    sandbox.commands.next_result = _FakeE2BResult(
        stdout=base64.b64encode(b"fake-tar-bytes").decode("ascii")
    )
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(
            root="/workspace",
            entries={
                "parent": parent_mount.bind_events(events),
                "nested": Dir(children={"child": child_mount.bind_events(events)}),
            },
        ),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    archive = await session.persist_workspace()

    assert archive.read() == b"fake-tar-bytes"
    assert [path for kind, path in events if kind == "unmount"] == [
        "/workspace/repo/sub",
        "/workspace/repo",
    ]
    assert [path for kind, path in events if kind == "mount"] == [
        "/workspace/repo",
        "/workspace/repo/sub",
    ]
    tar_command = str(sandbox.commands.calls[-1]["command"])
    assert "--exclude=repo" in tar_command
    assert "--exclude=./repo" in tar_command
    assert "--exclude=repo/sub" in tar_command
    assert "--exclude=./repo/sub" in tar_command


@pytest.mark.asyncio
async def test_e2b_persist_workspace_remounts_prior_mounts_after_unmount_failure() -> None:
    events: list[tuple[str, str]] = []
    sandbox = _FakeE2BSandbox()
    sandbox.commands.exec_root_ready = True
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(
            root="/workspace",
            entries={
                "repo": Dir(
                    children={
                        "mount1": _RecordingMount().bind_events(events),
                        "mount2": _FailingUnmountMount().bind_events(events),
                    }
                )
            },
        ),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(WorkspaceArchiveReadError):
        await session.persist_workspace()

    assert [kind for kind, _path in events] == [
        "unmount",
        "unmount_fail",
        "mount",
    ]
    assert sandbox.commands.calls == []


@pytest.mark.asyncio
async def test_e2b_persist_workspace_keeps_remounting_and_raises_remount_error_first() -> None:
    events: list[tuple[str, str]] = []
    sandbox = _FakeE2BSandbox()
    sandbox.commands.exec_root_ready = True
    sandbox.commands.next_result = _FakeE2BResult(stderr="tar failed", exit_code=2)
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(
            root="/workspace",
            entries={
                "repo": Dir(
                    children={
                        "a": _RecordingMount().bind_events(events),
                        "b": _FailingRemountMount().bind_events(events),
                    }
                )
            },
        ),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert isinstance(exc_info.value.cause, RuntimeError)
    assert str(exc_info.value.cause) == "boom while remounting second mount"
    assert exc_info.value.context["snapshot_error_before_remount_corruption"] == {
        "message": "failed to read archive for path: /workspace",
    }
    assert [kind for kind, _path in events] == [
        "unmount",
        "unmount",
        "mount_fail",
        "mount",
    ]


@pytest.mark.asyncio
async def test_e2b_clear_workspace_root_on_resume_preserves_nested_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _sandbox = _session()
    session.state.manifest = Manifest(
        root="/workspace",
        entries={
            "a/b": _RecordingMount(),
        },
    )
    ls_calls: list[Path] = []
    rm_calls: list[tuple[Path, bool]] = []

    async def _fake_ls(path: Path | str) -> list[object]:
        rendered = Path(path)
        ls_calls.append(rendered)
        if rendered == Path("/workspace"):
            return [
                type("Entry", (), {"path": "/workspace/a", "kind": EntryKind.DIRECTORY})(),
                type("Entry", (), {"path": "/workspace/root.txt", "kind": EntryKind.FILE})(),
            ]
        if rendered == Path("/workspace/a"):
            return [
                type("Entry", (), {"path": "/workspace/a/b", "kind": EntryKind.DIRECTORY})(),
                type("Entry", (), {"path": "/workspace/a/local.txt", "kind": EntryKind.FILE})(),
            ]
        raise AssertionError(f"unexpected ls path: {rendered}")

    async def _fake_rm(path: Path | str, *, recursive: bool = False) -> None:
        rm_calls.append((Path(path), recursive))

    monkeypatch.setattr(session, "ls", _fake_ls)
    monkeypatch.setattr(session, "rm", _fake_rm)

    await session._clear_workspace_root_on_resume()  # noqa: SLF001

    assert ls_calls == [Path("/workspace"), Path("/workspace/a")]
    assert rm_calls == [
        (Path("/workspace/a/local.txt"), True),
        (Path("/workspace/root.txt"), True),
    ]


@pytest.mark.asyncio
async def test_e2b_pty_start_and_write_stdin() -> None:
    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    started = await session.pty_exec_start("python3", shell=False, tty=True, yield_time_s=0.05)

    assert started.process_id is not None
    assert b">>>" in started.output

    updated = await session.pty_write_stdin(
        session_id=started.process_id,
        chars="5 + 5\n",
        yield_time_s=0.05,
    )

    assert updated.process_id == started.process_id
    assert b"10" in updated.output
    assert sandbox.pty.handle.stdin_payloads == [b"python3\n", b"5 + 5\n"]


@pytest.mark.asyncio
async def test_e2b_pty_start_non_tty_uses_commands_run_in_background() -> None:
    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    started = await session.pty_exec_start("python3", shell=False, tty=False, yield_time_s=0.05)

    assert started.process_id is None
    assert b"started" in started.output
    assert sandbox.commands.background_calls == [
        {
            "command": "python3",
            "timeout": float(session.state.timeouts.exec_timeout_unbounded_s),
            "cwd": "/workspace",
            "envs": {},
            "stdin": False,
            "background": True,
        }
    ]


@pytest.mark.asyncio
async def test_e2b_pty_start_non_tty_wraps_background_run_failures() -> None:
    sandbox = _FakeE2BSandbox()
    sandbox.commands.background_error = RuntimeError("background failed")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(ExecTransportError) as exc_info:
        await session.pty_exec_start("python3", shell=False, tty=False)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "background failed"


@pytest.mark.asyncio
async def test_e2b_stop_terminates_live_pty_sessions() -> None:
    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    started = await session.pty_exec_start("python3", shell=False, tty=True, yield_time_s=0.05)
    assert started.process_id is not None

    await session.stop()

    assert sandbox.pty.handle.exit_code == 0


@pytest.mark.asyncio
async def test_e2b_shutdown_logs_pause_failure_and_falls_back_to_kill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sandbox = _FakeE2BSandbox()
    sandbox.pause_error = RuntimeError("pause failed")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
        pause_on_exit=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    caplog.set_level(logging.WARNING, logger=e2b_module.__name__)

    await session.shutdown()

    assert sandbox.pause_calls == 1
    assert sandbox.kill_calls == 1
    assert "Failed to pause E2B sandbox on shutdown; falling back to kill." in caplog.text


@pytest.mark.asyncio
async def test_e2b_shutdown_logs_kill_failure_after_pause_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sandbox = _FakeE2BSandbox()
    sandbox.pause_error = RuntimeError("pause failed")
    sandbox.kill_error = RuntimeError("kill failed")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
        pause_on_exit=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    caplog.set_level(logging.WARNING, logger=e2b_module.__name__)

    await session.shutdown()

    assert sandbox.pause_calls == 1
    assert sandbox.kill_calls == 1
    assert "Failed to kill E2B sandbox after pause fallback failure." in caplog.text


@pytest.mark.asyncio
async def test_e2b_shutdown_logs_direct_kill_failure(caplog: pytest.LogCaptureFixture) -> None:
    sandbox = _FakeE2BSandbox()
    sandbox.kill_error = RuntimeError("kill failed")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
        pause_on_exit=False,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    caplog.set_level(logging.WARNING, logger=e2b_module.__name__)

    await session.shutdown()

    assert sandbox.pause_calls == 0
    assert sandbox.kill_calls == 1
    assert "Failed to kill E2B sandbox on shutdown." in caplog.text


@pytest.mark.asyncio
async def test_e2b_pty_start_wraps_startup_failures() -> None:
    sandbox = _FakeE2BSandbox()
    sandbox.pty.create_error = FileNotFoundError("missing-shell")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(ExecTransportError):
        await session.pty_exec_start("python3", shell=False, tty=True)


@pytest.mark.asyncio
async def test_e2b_pty_start_cleans_up_partially_created_session_on_failure() -> None:
    sandbox = _FakeE2BSandbox()
    sandbox.pty.send_stdin_error = RuntimeError("send failed")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(ExecTransportError):
        await session.pty_exec_start("python3", shell=False, tty=True)

    assert sandbox.pty.handle.exit_code == 0


@pytest.mark.asyncio
async def test_e2b_pty_start_cleans_up_partially_created_session_on_cancellation() -> None:
    sandbox = _FakeE2BSandbox()
    sandbox.pty.send_stdin_error = asyncio.CancelledError()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(asyncio.CancelledError):
        await session.pty_exec_start("python3", shell=False, tty=True)

    assert sandbox.pty.handle.exit_code == 0
    assert session._pty_processes == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_e2b_pty_start_maps_timeout_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeE2BSandbox()
    timeout_exc = e2b_module._import_e2b_exceptions().get("timeout")
    if timeout_exc is None:

        class _FakeTimeout(Exception):
            pass

        timeout_exc = _FakeTimeout
        monkeypatch.setattr(
            e2b_module,
            "_import_e2b_exceptions",
            lambda: {"timeout": _FakeTimeout},
        )
    sandbox.pty.create_error = timeout_exc("timed out")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(ExecTimeoutError):
        await session.pty_exec_start("python3", shell=False, tty=True, timeout=2.0)


@pytest.mark.asyncio
async def test_e2b_exec_timeout_preserves_provider_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTimeout(Exception):
        def __init__(self) -> None:
            super().__init__("context deadline exceeded")
            self.stderr = "chrome stderr"

    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    monkeypatch.setattr(
        e2b_module,
        "_import_e2b_exceptions",
        lambda: {"timeout": _FakeTimeout},
    )

    async def _raise_timeout(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        raise _FakeTimeout()

    monkeypatch.setattr(e2b_module, "_sandbox_run_command", _raise_timeout)

    with pytest.raises(ExecTimeoutError) as exc_info:
        await session._exec_internal("python3", "build.py", timeout=2.0)  # noqa: SLF001

    assert exc_info.value.context["provider_error"] == "context deadline exceeded"
    assert exc_info.value.context["stderr"] == "chrome stderr"


@pytest.mark.asyncio
async def test_e2b_exec_maps_httpcore_read_timeout_to_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadTimeout(Exception):
        pass

    ReadTimeout.__module__ = "httpcore"

    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    async def _raise_timeout(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        raise ReadTimeout()

    monkeypatch.setattr(e2b_module, "_sandbox_run_command", _raise_timeout)

    with pytest.raises(ExecTimeoutError) as exc_info:
        await session._exec_internal("python3", "build.py", timeout=2.0)  # noqa: SLF001

    assert exc_info.value.context["reason"] == "stream_read_timeout"
    assert exc_info.value.context["provider_error"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_e2b_exec_maps_missing_sandbox_timeout_to_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTimeout(Exception):
        pass

    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    monkeypatch.setattr(
        e2b_module,
        "_import_e2b_exceptions",
        lambda: {"timeout": _FakeTimeout},
    )

    async def _raise_timeout(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        raise _FakeTimeout("The sandbox was not found: request failed")

    monkeypatch.setattr(e2b_module, "_sandbox_run_command", _raise_timeout)

    with pytest.raises(ExecTransportError) as exc_info:
        await session._exec_internal("python3", "build.py", timeout=2.0)  # noqa: SLF001

    assert exc_info.value.context["provider_error"] == "The sandbox was not found: request failed"
    assert exc_info.value.context["reason"] == "sandbox_not_found"


@pytest.mark.asyncio
async def test_e2b_exec_transport_preserves_provider_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    async def _raise_transport(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        raise RuntimeError("connection closed while reading HTTP status line")

    monkeypatch.setattr(e2b_module, "_sandbox_run_command", _raise_transport)

    with pytest.raises(ExecTransportError) as exc_info:
        await session._exec_internal("python3", "build.py", timeout=2.0)  # noqa: SLF001

    assert (
        exc_info.value.context["provider_error"]
        == "connection closed while reading HTTP status line"
    )


@pytest.mark.asyncio
async def test_e2b_pty_start_maps_httpcore_read_timeout_to_timeout_error() -> None:
    class ReadTimeout(Exception):
        pass

    ReadTimeout.__module__ = "httpcore"

    sandbox = _FakeE2BSandbox()
    sandbox.pty.create_error = ReadTimeout()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(ExecTimeoutError) as exc_info:
        await session.pty_exec_start("python3", shell=False, tty=True, timeout=2.0)

    assert exc_info.value.context["reason"] == "stream_read_timeout"
    assert exc_info.value.context["provider_error"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_e2b_pty_start_maps_missing_sandbox_timeout_to_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTimeout(Exception):
        pass

    monkeypatch.setattr(
        e2b_module,
        "_import_e2b_exceptions",
        lambda: {"timeout": _FakeTimeout},
    )

    sandbox = _FakeE2BSandbox()
    sandbox.pty.create_error = _FakeTimeout("The sandbox was not found: request failed")
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=True,
    )
    session = E2BSandboxSession.from_state(state, sandbox=sandbox)

    with pytest.raises(ExecTransportError) as exc_info:
        await session.pty_exec_start("python3", shell=False, tty=True, timeout=2.0)

    assert exc_info.value.context["provider_error"] == "The sandbox was not found: request failed"
    assert exc_info.value.context["reason"] == "sandbox_not_found"

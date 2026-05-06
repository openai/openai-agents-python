from __future__ import annotations

import asyncio
import builtins
import importlib
import io
import shlex
import sys
import types
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field, PrivateAttr

import agents.extensions.sandbox.daytona.mounts as _daytona_mounts
from agents.extensions.sandbox.daytona.mounts import (
    DaytonaCloudBucketMountStrategy,
    _assert_daytona_session,
    _ensure_fuse_support,
    _ensure_rclone,
    _has_command,
    _pkg_install,
)
from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.entries import (
    Dir,
    InContainerMountStrategy,
    Mount,
    MountpointMountPattern,
    RcloneMountPattern,
    S3Mount,
)
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.errors import ExecTimeoutError, ExecTransportError, MountConfigError
from agents.sandbox.files import EntryKind
from agents.sandbox.manifest import Environment
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.base_sandbox_session import (
    _MKDIR_ACCESS_CHECK_SCRIPT,
    BaseSandboxSession,
)
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExecResult, ExposedPortEndpoint, User
from tests._fake_workspace_paths import resolve_fake_workspace_path
from tests.utils.factories import TestSessionState


class _RestorableSnapshot(SnapshotBase):
    type: Literal["test-restorable-daytona"] = "test-restorable-daytona"
    payload: bytes = b"restored"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO(self.payload)

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return True


class _FakeExecResult:
    def __init__(self, *, exit_code: int = 0, result: str = "") -> None:
        self.exit_code = exit_code
        self.result = result


class _FakePtyHandle:
    def __init__(self, on_data: object) -> None:
        self._on_data = on_data
        self.exit_code: int | None = None
        self._done = asyncio.Event()

    async def wait_for_connection(self) -> None:
        return None

    async def send_input(self, chars: str) -> None:
        if chars.endswith("\n") and "python3" in chars:
            await cast(Any, self._on_data)(b">>> ")
        elif chars == "5 + 5\n":
            await cast(Any, self._on_data)(b"10\n")
        elif chars == "exit\n":
            self.exit_code = 0
            self._done.set()

    async def wait(self) -> None:
        await self._done.wait()


class _FakeProcess:
    def __init__(self) -> None:
        self.exec_calls: list[tuple[str, dict[str, object]]] = []
        self.next_result = _FakeExecResult()
        self.next_session_command_result = types.SimpleNamespace(
            cmd_id="cmd-123",
            exit_code=0,
            stdout="",
            stderr="",
            output="",
        )
        self.create_pty_session_calls: list[dict[str, object]] = []
        self.create_session_calls: list[str] = []
        self.create_session_error: BaseException | None = None
        self.create_session_delay_s: float = 0.0
        self.kill_pty_session_calls: list[str] = []
        self.delete_session_calls: list[str] = []
        self.execute_session_command_calls: list[tuple[str, object, dict[str, object]]] = []
        self.get_session_command_logs_error: BaseException | None = None
        self.session_command_exit_code: int | None = 0
        self._pty_handles: dict[str, _FakePtyHandle] = {}
        self.create_pty_session_error: BaseException | None = None
        self.symlinks: dict[str, str] = {}
        self.workspace_roots: set[str] = set()
        self.require_workspace_root_for_cd = False

    async def exec(self, cmd: str, **kwargs: object) -> _FakeExecResult:
        self.exec_calls.append((cmd, dict(kwargs)))
        parts = shlex.split(cmd)
        if len(parts) >= 4 and parts[:3] == ["mkdir", "-p", "--"]:
            self.workspace_roots.add(parts[3])
        if "sleep 0.5" in cmd:
            await asyncio.sleep(0.5)
        result = self.next_result
        self.next_result = _FakeExecResult()
        return result

    async def create_pty_session(self, **kwargs: object) -> _FakePtyHandle:
        if self.create_pty_session_error is not None:
            raise self.create_pty_session_error
        self.create_pty_session_calls.append(dict(kwargs))
        session_id = cast(str, kwargs["id"])
        handle = _FakePtyHandle(kwargs["on_data"])
        self._pty_handles[session_id] = handle
        return handle

    async def kill_pty_session(self, session_id: str) -> None:
        self.kill_pty_session_calls.append(session_id)

    async def create_session(self, session_id: str) -> None:
        self.create_session_calls.append(session_id)
        if self.create_session_delay_s:
            await asyncio.sleep(self.create_session_delay_s)
        if self.create_session_error is not None:
            raise self.create_session_error

    async def execute_session_command(
        self, session_id: str, request: object, **kwargs: object
    ) -> object:
        self.execute_session_command_calls.append((session_id, request, dict(kwargs)))
        command = cast(str, getattr(request, "command", ""))
        parts = shlex.split(command)
        if (
            self.require_workspace_root_for_cd
            and len(parts) >= 3
            and parts[0] == "cd"
            and parts[2] == "&&"
            and parts[1] not in self.workspace_roots
        ):
            return types.SimpleNamespace(
                cmd_id="cmd-123",
                exit_code=1,
                stdout="",
                stderr=f"cd: no such file or directory: {parts[1]}",
                output=f"cd: no such file or directory: {parts[1]}",
            )
        resolved = resolve_fake_workspace_path(
            command,
            symlinks=self.symlinks,
            home_dir="/home/daytona/workspace",
        )
        if resolved is not None:
            return types.SimpleNamespace(
                exit_code=resolved.exit_code,
                stdout=resolved.stdout,
                stderr=resolved.stderr,
                output=resolved.stdout,
            )
        if "sleep 0.5" in command:
            await asyncio.sleep(0.5)
        if getattr(request, "run_async", None):
            return types.SimpleNamespace(cmd_id="cmd-123")
        result = self.next_session_command_result
        self.next_session_command_result = types.SimpleNamespace(
            cmd_id="cmd-123",
            exit_code=0,
            stdout="",
            stderr="",
            output="",
        )
        return result

    async def get_session_command_logs_async(
        self,
        session_id: str,
        cmd_id: str,
        on_stdout: object,
        on_stderr: object,
    ) -> None:
        _ = (session_id, cmd_id, on_stderr)
        if self.get_session_command_logs_error is not None:
            raise self.get_session_command_logs_error
        await cast(Any, on_stdout)("started\n")

    async def get_session_command(self, session_id: str, cmd_id: str) -> object:
        _ = (session_id, cmd_id)
        return types.SimpleNamespace(exit_code=self.session_command_exit_code)

    async def delete_session(self, session_id: str) -> None:
        self.delete_session_calls.append(session_id)


class _FakeFs:
    def __init__(self) -> None:
        self.create_folder_calls: list[tuple[str, str]] = []
        self.download_file_calls: list[tuple[str, float | None]] = []
        self.upload_file_calls: list[tuple[bytes, str, float | None]] = []
        self.download_value: bytes = b""

    async def create_folder(self, path: str, mode: str) -> None:
        self.create_folder_calls.append((path, mode))

    async def download_file(self, path: str, timeout: float | None = None) -> bytes:
        self.download_file_calls.append((path, timeout))
        return self.download_value

    async def upload_file(self, data: bytes, path: str, *, timeout: float | None = None) -> None:
        self.upload_file_calls.append((data, path, timeout))


class _FakeDaytonaSandbox:
    def __init__(self, *, sandbox_id: str = "sandbox-123") -> None:
        self.id = sandbox_id
        self.state = "started"
        self.process = _FakeProcess()
        self.fs = _FakeFs()
        self.start_calls: list[int | None] = []
        self.stop_calls = 0
        self.delete_calls = 0
        self.signed_preview_url_calls: list[tuple[int, int | None]] = []

    async def refresh_data(self) -> None:
        return None

    async def start(self, *, timeout: int | None = None) -> None:
        self.start_calls.append(timeout)
        self.state = "started"

    async def stop(self) -> None:
        self.stop_calls += 1

    async def delete(self) -> None:
        self.delete_calls += 1

    async def create_signed_preview_url(
        self,
        port: int,
        expires_in_seconds: int | None = None,
    ) -> object:
        self.signed_preview_url_calls.append((port, expires_in_seconds))
        return types.SimpleNamespace(
            url=f"https://{port}-signed-token.daytonaproxy01.net",
            token="signed-token",
        )


class _FakeAsyncDaytona:
    create_calls: list[tuple[object, int | None]] = []
    get_calls: list[str] = []
    current_sandbox: _FakeDaytonaSandbox | None = None
    get_error: BaseException | None = None

    def __init__(self, config: object | None = None) -> None:
        _ = config

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []
        cls.get_calls = []
        cls.current_sandbox = None
        cls.get_error = None

    async def create(self, params: object, timeout: int | None = None) -> _FakeDaytonaSandbox:
        type(self).create_calls.append((params, timeout))
        sandbox = _FakeDaytonaSandbox()
        type(self).current_sandbox = sandbox
        return sandbox

    async def get(self, sandbox_id: str) -> _FakeDaytonaSandbox:
        type(self).get_calls.append(sandbox_id)
        get_error = type(self).get_error
        if get_error is not None:
            raise get_error
        if type(self).current_sandbox is None:
            type(self).current_sandbox = _FakeDaytonaSandbox(sandbox_id=sandbox_id)
        sandbox = type(self).current_sandbox
        assert sandbox is not None
        return sandbox

    async def close(self) -> None:
        return None


def _load_daytona_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeAsyncDaytona.reset()

    class _FakeParams:
        def __init__(self, **kwargs: object) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _FakeDaytonaConfig:
        def __init__(self, api_key: str | None = None, api_url: str | None = None) -> None:
            self.api_key = api_key
            self.api_url = api_url

    class _FakePtySize:
        def __init__(self, *, cols: int, rows: int) -> None:
            self.cols = cols
            self.rows = rows

    class _FakeResources:
        def __init__(
            self,
            *,
            cpu: int | None = None,
            memory: int | None = None,
            disk: int | None = None,
        ) -> None:
            self.cpu = cpu
            self.memory = memory
            self.disk = disk

    fake_daytona: Any = types.ModuleType("daytona")
    fake_daytona.AsyncDaytona = _FakeAsyncDaytona
    fake_daytona.DaytonaConfig = _FakeDaytonaConfig
    fake_daytona.CreateSandboxFromSnapshotParams = _FakeParams
    fake_daytona.CreateSandboxFromImageParams = _FakeParams
    fake_daytona.SessionExecuteRequest = _FakeParams
    fake_daytona.Resources = _FakeResources
    fake_daytona.SandboxState = types.SimpleNamespace(STARTED="started")

    fake_daytona_common: Any = types.ModuleType("daytona.common")
    fake_daytona_common_pty: Any = types.ModuleType("daytona.common.pty")
    fake_daytona_common_pty.PtySize = _FakePtySize

    monkeypatch.setitem(sys.modules, "daytona", fake_daytona)
    monkeypatch.setitem(sys.modules, "daytona.common", fake_daytona_common)
    monkeypatch.setitem(sys.modules, "daytona.common.pty", fake_daytona_common_pty)
    sys.modules.pop("agents.extensions.sandbox.daytona.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.daytona", None)
    return importlib.import_module("agents.extensions.sandbox.daytona.sandbox")


def test_daytona_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    daytona_module = _load_daytona_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.daytona")

    assert package_module.DaytonaSandboxClient is daytona_module.DaytonaSandboxClient


class _RecordingMount(Mount):
    type: str = "daytona_recording_mount"
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

    async def mount(self, session: object, path: Path) -> None:
        _ = session
        self._events.append(("mount", path.as_posix()))
        self._mounted_paths.append(path)

    async def unmount_path(self, session: object, path: Path) -> None:
        _ = session
        self._events.append(("unmount", path.as_posix()))
        self._unmounted_paths.append(path)


class _FailingUnmountMount(_RecordingMount):
    type: str = "daytona_failing_unmount_mount"

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

    async def unmount_path(self, session: object, path: Path) -> None:
        _ = session
        self._events.append(("unmount_fail", path.as_posix()))
        raise RuntimeError("boom while unmounting second mount")


class TestDaytonaSandbox:
    @pytest.mark.asyncio
    async def test_create_uses_daytona_safe_default_workspace_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify omitted manifests default to a writable Daytona workspace root."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())

        assert session.state.manifest.root == daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT

    @pytest.mark.asyncio
    async def test_start_prepares_workspace_root_before_runtime_helpers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify Daytona creates the root before exec uses it as cwd."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None
            sandbox.process.require_workspace_root_for_cd = True

            await session.start()

        root = daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT
        assert root in sandbox.process.workspace_roots
        assert sandbox.process.exec_calls[0][0] == f"mkdir -p -- {root}"
        assert sandbox.process.execute_session_command_calls
        _session_id, request, _kwargs = sandbox.process.execute_session_command_calls[0]
        assert cast(str, cast(Any, request).command).startswith(f"cd {root} && ")
        assert session.state.workspace_root_ready is True

    @pytest.mark.asyncio
    async def test_start_wraps_workspace_root_prepare_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify Daytona surfaces root preparation failures as start errors."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None
            sandbox.process.next_result = _FakeExecResult(exit_code=2, result="mkdir failed")

            with pytest.raises(daytona_module.WorkspaceStartError) as exc_info:
                await session.start()

        assert exc_info.value.context == {
            "path": daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
            "reason": "workspace_root_nonzero_exit",
            "exit_code": 2,
            "output": "mkdir failed",
        }
        assert sandbox.process.execute_session_command_calls == []
        assert session.state.workspace_root_ready is False

    @pytest.mark.asyncio
    async def test_create_passes_only_option_env_vars_to_daytona(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify manifest env vars are not passed into Daytona's create-time env shell."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            await client.create(
                manifest=Manifest(
                    root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
                    environment=Environment(value={"SHARED": "manifest", "ONLY_MANIFEST": "1"}),
                ),
                options=daytona_module.DaytonaSandboxClientOptions(
                    env_vars={"SHARED": "option", "ONLY_OPTION": "1"},
                ),
            )

        assert _FakeAsyncDaytona.create_calls
        params, _timeout = _FakeAsyncDaytona.create_calls[0]
        assert cast(Any, params).env_vars == {
            "SHARED": "option",
            "ONLY_OPTION": "1",
        }

    @pytest.mark.asyncio
    async def test_exec_enforces_subsecond_caller_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify a sub-second user timeout fails even though the SDK timeout is ceiled."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())

            with pytest.raises(ExecTimeoutError):
                await session.exec("sleep 0.5", shell=False, timeout=0.1)

        sandbox = _FakeAsyncDaytona.current_sandbox
        assert sandbox is not None
        _session_id, _request, kwargs = sandbox.process.execute_session_command_calls[0]
        assert kwargs["timeout"] == 2

    @pytest.mark.asyncio
    async def test_exec_timeout_budget_includes_session_create(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None
            sandbox.process.create_session_delay_s = 0.2

            await session.exec("echo", "done", shell=False, timeout=1.1)

        assert sandbox.process.create_session_calls
        _session_id, _request, kwargs = sandbox.process.execute_session_command_calls[0]
        assert kwargs["timeout"] == 2

    @pytest.mark.asyncio
    async def test_exec_delete_session_cleanup_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)
        real_wait_for = asyncio.wait_for
        cleanup_timeouts: list[float | None] = []

        async def _record_cleanup_wait_for(awaitable: Any, timeout: float | None = None) -> Any:
            code = getattr(awaitable, "cr_code", None)
            if getattr(code, "co_name", None) == "delete_session":
                awaitable.close()
                cleanup_timeouts.append(timeout)
                return None
            return await real_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(daytona_module.asyncio, "wait_for", _record_cleanup_wait_for)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(
                options=daytona_module.DaytonaSandboxClientOptions(
                    timeouts=daytona_module.DaytonaSandboxTimeouts(cleanup_s=7)
                )
            )
            await session.exec("echo", "done", shell=False, timeout=5.0)

        assert cleanup_timeouts == [7]

    @pytest.mark.asyncio
    async def test_exec_merges_manifest_env_with_option_precedence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify manifest env vars are applied through the adapter-controlled exec path."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(
                    root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
                    environment=Environment(value={"SHARED": "manifest", "ONLY_MANIFEST": "1"}),
                ),
                options=daytona_module.DaytonaSandboxClientOptions(
                    env_vars={"SHARED": "option", "ONLY_OPTION": "1"},
                ),
            )
            await session.exec("printenv", "SHARED", shell=False, timeout=5.0)

        sandbox = _FakeAsyncDaytona.current_sandbox
        assert sandbox is not None
        _session_id, request, _kwargs = sandbox.process.execute_session_command_calls[0]
        command = cast(str, cast(Any, request).command)
        assert "env --" in command
        assert "SHARED=manifest" in command
        assert "ONLY_MANIFEST=1" in command
        assert "ONLY_OPTION=1" in command

    @pytest.mark.asyncio
    async def test_exec_preserves_session_command_stdout_and_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None
            sandbox.process.next_session_command_result = types.SimpleNamespace(
                cmd_id="cmd-123",
                exit_code=7,
                stdout="hello stdout",
                stderr="hello stderr",
                output="hello stdouthello stderr",
            )
            result = await session.exec("sh", "-c", "printf out; printf err >&2", shell=False)

        assert result.exit_code == 7
        assert result.stdout == b"hello stdout"
        assert result.stderr == b"hello stderr"

    @pytest.mark.asyncio
    async def test_resume_reconnects_paused_sandbox_and_preserves_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify pause-on-exit resumes an existing sandbox instead of creating a new one."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(
                options=daytona_module.DaytonaSandboxClientOptions(pause_on_exit=True),
            )
            state = session.state
            _FakeAsyncDaytona.create_calls.clear()

            resumed = await client.resume(state)

        assert _FakeAsyncDaytona.get_calls == [state.sandbox_id]
        assert _FakeAsyncDaytona.create_calls == []
        assert resumed._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
        assert resumed._inner._system_state_preserved_on_start() is True  # noqa: SLF001
        assert resumed._inner._can_reuse_preserved_workspace_on_resume() is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_resume_reconnects_unpaused_live_sandbox_after_unclean_worker_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify resume reconnects to a live sandbox that was never cleanly deleted."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            state = session.state
            _FakeAsyncDaytona.create_calls.clear()

            resumed = await client.resume(state)

        assert _FakeAsyncDaytona.get_calls == [state.sandbox_id]
        assert _FakeAsyncDaytona.create_calls == []
        assert resumed.state.sandbox_id == state.sandbox_id
        assert resumed._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
        assert resumed._inner._system_state_preserved_on_start() is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_resume_recreates_unpaused_sandbox_when_reconnect_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify resume falls back to a fresh Daytona sandbox when the old id is gone."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            state = session.state
            old_sandbox_id = state.sandbox_id
            _FakeAsyncDaytona.create_calls.clear()
            _FakeAsyncDaytona.get_error = RuntimeError("sandbox_not_found")

            resumed = await client.resume(state)

        assert _FakeAsyncDaytona.get_calls == [old_sandbox_id]
        assert len(_FakeAsyncDaytona.create_calls) == 1
        assert resumed.state.sandbox_id == "sandbox-123"
        assert resumed._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001
        assert resumed._inner._system_state_preserved_on_start() is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_preserved_start_rehydrates_when_snapshot_gate_requests_restore(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify resumed paused sandboxes can still rehydrate when the fingerprint gate fails."""

        daytona_module = _load_daytona_module(monkeypatch)
        session = daytona_module.DaytonaSandboxSession.from_state(
            daytona_module.DaytonaSandboxSessionState(
                manifest=Manifest(root="/workspace"),
                snapshot=_RestorableSnapshot(id="snapshot"),
                sandbox_id="sandbox-123",
                pause_on_exit=True,
                workspace_root_ready=True,
            ),
            sandbox=_FakeDaytonaSandbox(),
        )
        session._set_start_state_preserved(True)  # noqa: SLF001

        events: list[object] = []

        async def _running() -> bool:
            return True

        async def _gate(*, is_running: bool) -> bool:
            events.append(("gate", is_running))
            return False

        async def _restore() -> None:
            events.append("restore")

        async def _reapply() -> None:
            events.append("reapply")

        monkeypatch.setattr(session, "running", _running)
        session._can_skip_snapshot_restore_on_resume = _gate
        monkeypatch.setattr(session, "_restore_snapshot_into_workspace_on_resume", _restore)
        monkeypatch.setattr(session, "_reapply_ephemeral_manifest_on_resume", _reapply)

        await session.start()

        assert events == [("gate", True), "restore", "reapply"]

    @pytest.mark.asyncio
    async def test_resolve_exposed_port_uses_signed_preview_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify Daytona maps signed preview URLs to the shared exposed-port endpoint shape."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(
                options=daytona_module.DaytonaSandboxClientOptions(
                    exposed_ports=(4500,),
                    exposed_port_url_ttl_s=1800,
                ),
            )

            endpoint = await session.resolve_exposed_port(4500)

        assert endpoint == ExposedPortEndpoint(
            host="4500-signed-token.daytonaproxy01.net",
            port=443,
            tls=True,
        )
        sandbox = _FakeAsyncDaytona.current_sandbox
        assert sandbox is not None
        assert sandbox.signed_preview_url_calls == [(4500, 1800)]

    @pytest.mark.asyncio
    async def test_resolve_exposed_port_rejects_invalid_preview_urls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify malformed Daytona preview URLs become ExposedPortUnavailableError."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(
                options=daytona_module.DaytonaSandboxClientOptions(exposed_ports=(4500,)),
            )
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None

            async def _bad_preview_url(
                port: int,
                expires_in_seconds: int | None = None,
            ) -> object:
                _ = (port, expires_in_seconds)
                return types.SimpleNamespace(url=":", token="bad")

            sandbox.create_signed_preview_url = _bad_preview_url  # type: ignore[method-assign]

            with pytest.raises(daytona_module.ExposedPortUnavailableError) as exc_info:
                await session.resolve_exposed_port(4500)

        assert exc_info.value.context["detail"] == "invalid_preview_url"

    @pytest.mark.asyncio
    async def test_normalize_path_rejects_workspace_escape_and_allows_absolute_in_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify Daytona normalizes paths without host resolution and enforces the root."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            inner = session._inner  # noqa: SLF001

            with pytest.raises(daytona_module.InvalidManifestPathError):
                inner.normalize_path("../outside")
            with pytest.raises(daytona_module.InvalidManifestPathError):
                inner.normalize_path("/etc/passwd")

            assert inner.normalize_path(
                f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/nested/file.txt"
            ) == Path(f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/nested/file.txt")

    @pytest.mark.asyncio
    async def test_read_and_write_reject_paths_outside_workspace_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify Daytona read/write reject absolute and traversal paths before remote FS calls."""

        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())

            with pytest.raises(daytona_module.InvalidManifestPathError):
                await session.read("../outside.txt")
            with pytest.raises(daytona_module.InvalidManifestPathError):
                await session.write("/etc/passwd", io.BytesIO(b"nope"))

    @pytest.mark.asyncio
    async def test_read_rejects_workspace_symlink_to_ungranted_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None
            sandbox.process.symlinks[f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/link"] = (
                "/private"
            )

            with pytest.raises(daytona_module.InvalidManifestPathError) as exc_info:
                await session.read("link/secret.txt")

        assert sandbox.fs.download_file_calls == []
        assert str(exc_info.value) == "manifest path must not escape root: link/secret.txt"
        assert exc_info.value.context == {
            "rel": "link/secret.txt",
            "reason": "escape_root",
            "resolved_path": "workspace escape: /private/secret.txt",
        }

    @pytest.mark.asyncio
    async def test_write_rejects_workspace_symlink_to_read_only_extra_path_grant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(
                    root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
                    extra_path_grants=(SandboxPathGrant(path="/tmp/protected", read_only=True),),
                ),
                options=daytona_module.DaytonaSandboxClientOptions(),
            )
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None
            sandbox.process.symlinks[f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/link"] = (
                "/tmp/protected"
            )

            with pytest.raises(daytona_module.WorkspaceArchiveWriteError) as exc_info:
                await session.write("link/out.txt", io.BytesIO(b"blocked"))

        assert sandbox.fs.upload_file_calls == []
        assert str(exc_info.value) == (
            "failed to write archive for path: "
            f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/link/out.txt"
        )
        assert exc_info.value.context == {
            "path": f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/link/out.txt",
            "reason": "read_only_extra_path_grant",
            "grant_path": "/tmp/protected",
            "resolved_path": "/tmp/protected/out.txt",
        }

    @pytest.mark.asyncio
    async def test_mkdir_rejects_workspace_symlink_to_read_only_extra_path_grant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(
                    root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
                    extra_path_grants=(SandboxPathGrant(path="/tmp/protected", read_only=True),),
                ),
                options=daytona_module.DaytonaSandboxClientOptions(),
            )
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None
            sandbox.process.symlinks[f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/link"] = (
                "/tmp/protected"
            )

            with pytest.raises(daytona_module.WorkspaceArchiveWriteError) as exc_info:
                await session.mkdir("link/newdir")

        assert sandbox.fs.create_folder_calls == []
        assert str(exc_info.value) == (
            "failed to write archive for path: "
            f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/link/newdir"
        )
        assert exc_info.value.context == {
            "path": f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/link/newdir",
            "reason": "read_only_extra_path_grant",
            "grant_path": "/tmp/protected",
            "resolved_path": "/tmp/protected/newdir",
        }

    @pytest.mark.asyncio
    async def test_mkdir_as_user_checks_permissions_then_uses_files_api(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)

        async with daytona_module.DaytonaSandboxClient() as client:
            session = await client.create(options=daytona_module.DaytonaSandboxClientOptions())
            sandbox = _FakeAsyncDaytona.current_sandbox
            assert sandbox is not None

            await session.mkdir("nested", user=User(name="sandbox-user"))

        assert sandbox.fs.create_folder_calls == [
            (f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/nested", "755")
        ]
        commands = [
            cast(str, cast(Any, request).command)
            for _session_id, request, _kwargs in sandbox.process.execute_session_command_calls
        ]
        expected_cmd = f"cd {daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT} && " + shlex.join(
            [
                "sudo",
                "-u",
                "sandbox-user",
                "--",
                "sh",
                "-lc",
                _MKDIR_ACCESS_CHECK_SCRIPT,
                "sh",
                f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/nested",
                "0",
            ]
        )
        assert commands[-1] == expected_cmd

    @pytest.mark.asyncio
    async def test_persist_workspace_remounts_mounts_after_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify mounts are restored after a Daytona workspace snapshot completes."""

        daytona_module = _load_daytona_module(monkeypatch)
        mount = _RecordingMount()
        sandbox = _FakeDaytonaSandbox()
        sandbox.fs.download_value = b"fake-tar-bytes"
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(
                root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
                entries={"mount": mount},
            ),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)

        archive = await session.persist_workspace()

        assert archive.read() == b"fake-tar-bytes"
        mount_path = Path(f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/mount")
        assert mount._unmounted_paths == [mount_path]
        assert mount._mounted_paths == [mount_path]

    @pytest.mark.asyncio
    async def test_persist_workspace_uses_nested_mount_targets_and_runtime_skip_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify Daytona excludes nested mount targets and runtime-registered skip paths."""

        daytona_module = _load_daytona_module(monkeypatch)
        parent_mount = _RecordingMount(mount_path=Path("repo"))
        child_mount = _RecordingMount(mount_path=Path("repo/sub"))
        events: list[tuple[str, str]] = []
        sandbox = _FakeDaytonaSandbox()
        sandbox.fs.download_value = b"fake-tar-bytes"
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(
                root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
                entries={
                    "parent": parent_mount.bind_events(events),
                    "nested": Dir(children={"child": child_mount.bind_events(events)}),
                },
            ),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)
        session.register_persist_workspace_skip_path("runtime.tmp")

        archive = await session.persist_workspace()

        assert archive.read() == b"fake-tar-bytes"
        assert {path for kind, path in events if kind == "unmount"} == {
            f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/repo",
            f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/repo/sub",
        }
        assert {path for kind, path in events if kind == "mount"} == {
            f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/repo",
            f"{daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT}/repo/sub",
        }
        tar_command = sandbox.process.exec_calls[0][0]
        assert "--exclude=repo" in tar_command
        assert "--exclude=./repo" in tar_command
        assert "--exclude=repo/sub" in tar_command
        assert "--exclude=./repo/sub" in tar_command
        assert "--exclude=runtime.tmp" in tar_command

    @pytest.mark.asyncio
    async def test_persist_workspace_remounts_prior_mounts_after_unmount_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify a partial Daytona unmount failure remounts earlier mounts before raising."""

        daytona_module = _load_daytona_module(monkeypatch)
        events: list[tuple[str, str]] = []
        sandbox = _FakeDaytonaSandbox()
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(
                root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
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
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)

        with pytest.raises(daytona_module.WorkspaceArchiveReadError):
            await session.persist_workspace()

        assert [kind for kind, _path in events] == [
            "unmount",
            "unmount_fail",
            "mount",
        ]
        assert sandbox.process.exec_calls == []

    @pytest.mark.asyncio
    async def test_clear_workspace_root_on_resume_preserves_nested_mounts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify inherited resume cleanup skips mounted directories."""

        daytona_module = _load_daytona_module(monkeypatch)
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(
                root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT,
                entries={
                    "a/b": _RecordingMount(),
                },
            ),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id="sandbox-123",
        )
        session = daytona_module.DaytonaSandboxSession.from_state(
            state,
            sandbox=_FakeDaytonaSandbox(),
        )
        workspace_root = Path(daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT)
        ls_calls: list[Path] = []
        rm_calls: list[tuple[Path, bool]] = []

        async def _fake_ls(path: Path | str) -> list[object]:
            rendered = Path(path)
            ls_calls.append(rendered)
            if rendered == workspace_root:
                return [
                    types.SimpleNamespace(
                        path=str(workspace_root / "a"),
                        kind=EntryKind.DIRECTORY,
                    ),
                    types.SimpleNamespace(
                        path=str(workspace_root / "root.txt"),
                        kind=EntryKind.FILE,
                    ),
                ]
            if rendered == workspace_root / "a":
                return [
                    types.SimpleNamespace(
                        path=str(workspace_root / "a/b"),
                        kind=EntryKind.DIRECTORY,
                    ),
                    types.SimpleNamespace(
                        path=str(workspace_root / "a/local.txt"),
                        kind=EntryKind.FILE,
                    ),
                ]
            raise AssertionError(f"unexpected ls path: {rendered}")

        async def _fake_rm(path: Path | str, *, recursive: bool = False) -> None:
            rm_calls.append((Path(path), recursive))

        monkeypatch.setattr(session, "ls", _fake_ls)
        monkeypatch.setattr(session, "rm", _fake_rm)

        await session._clear_workspace_root_on_resume()  # noqa: SLF001

        assert ls_calls == [workspace_root, workspace_root / "a"]
        assert rm_calls == [
            (workspace_root / "a/local.txt", True),
            (workspace_root / "root.txt", True),
        ]

    @pytest.mark.asyncio
    async def test_pty_start_write_and_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        daytona_module = _load_daytona_module(monkeypatch)
        sandbox = _FakeDaytonaSandbox()
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)

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

        finished = await session.pty_write_stdin(
            session_id=started.process_id,
            chars="exit\n",
            yield_time_s=0.05,
        )
        assert finished.process_id is None
        assert finished.exit_code == 0

    @pytest.mark.asyncio
    async def test_stop_terminates_live_pty_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        daytona_module = _load_daytona_module(monkeypatch)
        sandbox = _FakeDaytonaSandbox()
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)

        started = await session.pty_exec_start("python3", shell=False, tty=True, yield_time_s=0.05)
        assert started.process_id is not None

        await session.stop()

        assert sandbox.process.kill_pty_session_calls

    @pytest.mark.asyncio
    async def test_pty_start_wraps_startup_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)
        sandbox = _FakeDaytonaSandbox()
        sandbox.process.create_pty_session_error = FileNotFoundError("missing-shell")
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)

        with pytest.raises(ExecTransportError):
            await session.pty_exec_start("python3", shell=False, tty=True)

    @pytest.mark.asyncio
    async def test_pty_start_maps_sdk_timeout_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)

        class _FakeTimeout(Exception):
            pass

        monkeypatch.setattr(
            daytona_module,
            "_import_daytona_exceptions",
            lambda: {"timeout": _FakeTimeout},
        )

        sandbox = _FakeDaytonaSandbox()
        sandbox.process.create_session_error = _FakeTimeout("timed out")
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)

        with pytest.raises(ExecTimeoutError):
            await session.pty_exec_start("python3", shell=False, tty=False, timeout=2.0)

    @pytest.mark.asyncio
    async def test_session_reader_keeps_entry_live_when_logs_fail_without_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daytona_module = _load_daytona_module(monkeypatch)
        sandbox = _FakeDaytonaSandbox()
        sandbox.process.get_session_command_logs_error = RuntimeError("logs failed")
        sandbox.process.session_command_exit_code = None
        state = daytona_module.DaytonaSandboxSessionState(
            manifest=Manifest(root=daytona_module.DEFAULT_DAYTONA_WORKSPACE_ROOT),
            snapshot=NoopSnapshot(id="snapshot"),
            sandbox_id=sandbox.id,
        )
        session = daytona_module.DaytonaSandboxSession.from_state(state, sandbox=sandbox)
        entry = daytona_module._DaytonaPtySessionEntry(  # noqa: SLF001
            daytona_session_id="session-123",
            pty_handle=object(),
            tty=False,
            cmd_id="cmd-123",
        )

        await session._run_session_reader(  # noqa: SLF001
            entry,
            "session-123",
            "cmd-123",
            lambda _chunk: None,
        )

        assert entry.done is False
        assert entry.exit_code is None


# ---------------------------------------------------------------------------
# DaytonaCloudBucketMountStrategy tests
# ---------------------------------------------------------------------------


class _FakePreflightSession(BaseSandboxSession):
    """Fake session for testing mount preflights with queued exec results."""

    # Make type(instance).__name__ return "DaytonaSandboxSession" so the session guard passes.
    __name__ = "DaytonaSandboxSession"

    def __init__(self, results: list[ExecResult] | None = None) -> None:
        self.state = TestSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="test"),
        )
        self._results: deque[ExecResult] = deque(results or [])
        self.exec_calls: list[str] = []

    def _ok(self) -> ExecResult:
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    def _fail(self) -> ExecResult:
        return ExecResult(stdout=b"", stderr=b"", exit_code=1)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd_str = " ".join(str(c) for c in command)
        self.exec_calls.append(cmd_str)
        if self._results:
            return self._results.popleft()
        return self._ok()

    async def read(self, path: Path, *, user: object = None) -> io.IOBase:
        _ = (path, user)
        return io.BytesIO(b"")

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)

    async def running(self) -> bool:
        return True

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("not expected")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raise AssertionError("not expected")


# Override __name__ at the class level so type(instance).__name__ == "DaytonaSandboxSession".
_FakePreflightSession.__name__ = "DaytonaSandboxSession"


def _ok() -> ExecResult:
    return ExecResult(stdout=b"", stderr=b"", exit_code=0)


def _fail() -> ExecResult:
    return ExecResult(stdout=b"", stderr=b"", exit_code=1)


# --- Export & Construction ---


def test_daytona_mount_strategy_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_daytona_module(monkeypatch)
    package = importlib.import_module("agents.extensions.sandbox.daytona")
    assert hasattr(package, "DaytonaCloudBucketMountStrategy")
    assert package.DaytonaCloudBucketMountStrategy is DaytonaCloudBucketMountStrategy


def test_daytona_mount_strategy_type_and_default_pattern() -> None:
    strategy = DaytonaCloudBucketMountStrategy()
    assert strategy.type == "daytona_cloud_bucket"
    assert isinstance(strategy.pattern, RcloneMountPattern)
    assert strategy.pattern.mode == "fuse"


def test_daytona_mount_strategy_round_trips_through_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_daytona_module(monkeypatch)

    manifest = Manifest.model_validate(
        {
            "root": "/workspace",
            "entries": {
                "bucket": {
                    "type": "s3_mount",
                    "bucket": "my-bucket",
                    "mount_strategy": {"type": "daytona_cloud_bucket"},
                }
            },
        }
    )
    mount = manifest.entries["bucket"]
    assert isinstance(mount, S3Mount)
    assert isinstance(mount.mount_strategy, DaytonaCloudBucketMountStrategy)


# --- Session Guard ---


def test_daytona_session_guard_rejects_wrong_type() -> None:
    class _WrongSession:
        pass

    with pytest.raises(MountConfigError, match="DaytonaSandboxSession"):
        _assert_daytona_session(_WrongSession())  # type: ignore[arg-type]


def test_daytona_session_guard_accepts_correct_type() -> None:
    session = _FakePreflightSession()
    _assert_daytona_session(session)  # should not raise


# --- _has_command ---


@pytest.mark.asyncio
async def test_has_command_found() -> None:
    session = _FakePreflightSession([_ok()])
    assert await _has_command(session, "rclone") is True
    assert len(session.exec_calls) == 1
    assert "command -v rclone" in session.exec_calls[0]


@pytest.mark.asyncio
async def test_has_command_not_found() -> None:
    session = _FakePreflightSession([_fail()])
    assert await _has_command(session, "rclone") is False


# --- _pkg_install ---


@pytest.mark.asyncio
async def test_pkg_install_via_apt() -> None:
    session = _FakePreflightSession(
        [
            _ok(),  # _has_command("apt-get") → found
            _ok(),  # install succeeds
        ]
    )
    await _pkg_install(session, "rclone", what="rclone")
    assert any("apt-get" in c and "rclone" in c for c in session.exec_calls)
    assert any(c.startswith("sudo -u root --") and "apt-get" in c for c in session.exec_calls)


@pytest.mark.asyncio
async def test_pkg_install_via_apk() -> None:
    session = _FakePreflightSession(
        [
            _fail(),  # _has_command("apt-get") → not found
            _ok(),  # _has_command("apk") → found
            _ok(),  # install succeeds
        ]
    )
    await _pkg_install(session, "fuse3", what="fusermount")
    assert any("apk add" in c and "fuse3" in c for c in session.exec_calls)
    assert any(c.startswith("sudo -u root --") and "apk add" in c for c in session.exec_calls)


@pytest.mark.asyncio
async def test_pkg_install_no_package_manager() -> None:
    session = _FakePreflightSession(
        [
            _fail(),  # _has_command("apt-get") → not found
            _fail(),  # _has_command("apk") → not found
        ]
    )
    with pytest.raises(MountConfigError, match="no supported package manager"):
        await _pkg_install(session, "rclone", what="rclone")


@pytest.mark.asyncio
async def test_pkg_install_retries_then_fails() -> None:
    session = _FakePreflightSession(
        [
            _ok(),  # _has_command("apt-get") → found
            _fail(),  # install attempt 1
            _fail(),  # install attempt 2
            _fail(),  # install attempt 3
        ]
    )
    with pytest.raises(MountConfigError, match="after 3 attempts"):
        await _pkg_install(session, "rclone", what="rclone")
    # 1 check + 3 install attempts = 4 exec calls.
    assert len(session.exec_calls) == 4
    assert all(c.startswith("sudo -u root --") for c in session.exec_calls[1:])


# --- _ensure_fuse_support ---


@pytest.mark.asyncio
async def test_ensure_fuse_dev_fuse_missing() -> None:
    session = _FakePreflightSession([_fail()])
    with pytest.raises(MountConfigError, match="/dev/fuse not available"):
        await _ensure_fuse_support(session)


@pytest.mark.asyncio
async def test_ensure_fuse_kernel_module_missing() -> None:
    session = _FakePreflightSession(
        [
            _ok(),  # /dev/fuse exists
            _fail(),  # fuse not in /proc/filesystems
        ]
    )
    with pytest.raises(MountConfigError, match="FUSE kernel module not loaded"):
        await _ensure_fuse_support(session)


@pytest.mark.asyncio
async def test_ensure_fuse_fusermount_present() -> None:
    session = _FakePreflightSession(
        [
            _ok(),  # /dev/fuse
            _ok(),  # /proc/filesystems
            _ok(),  # _has_command("fusermount3") → found
        ]
    )
    await _ensure_fuse_support(session)
    assert len(session.exec_calls) == 3


@pytest.mark.asyncio
async def test_ensure_fuse_installs_when_missing() -> None:
    session = _FakePreflightSession(
        [
            _ok(),  # /dev/fuse
            _ok(),  # /proc/filesystems
            _fail(),  # _has_command("fusermount3") → not found
            _fail(),  # _has_command("fusermount") → not found
            _ok(),  # _has_command("apt-get") → found (inside _pkg_install)
            _ok(),  # apt-get install fuse3 → success
            _ok(),  # re-check: _has_command("fusermount3") → found
        ]
    )
    await _ensure_fuse_support(session)
    assert any("fuse3" in c for c in session.exec_calls)
    assert len(session.exec_calls) == 7


# --- _ensure_rclone ---


@pytest.mark.asyncio
async def test_ensure_rclone_present() -> None:
    session = _FakePreflightSession([_ok()])
    await _ensure_rclone(session)
    assert len(session.exec_calls) == 1


@pytest.mark.asyncio
async def test_ensure_rclone_installs_when_missing() -> None:
    session = _FakePreflightSession(
        [
            _fail(),  # _has_command("rclone") → not found
            _ok(),  # _has_command("apt-get") → found (inside _pkg_install)
            _ok(),  # apt-get install rclone → success
            _ok(),  # re-check: _has_command("rclone") → found
        ]
    )
    await _ensure_rclone(session)
    assert any("rclone" in c for c in session.exec_calls)
    assert len(session.exec_calls) == 4


# --- Strategy lifecycle ---


@pytest.mark.asyncio
async def test_activate_calls_preflights_and_delegates() -> None:
    strategy = DaytonaCloudBucketMountStrategy()
    mount = MagicMock()
    session = _FakePreflightSession()
    dest = Path("/workspace")
    base_dir = Path("/workspace")

    with (
        patch.object(_daytona_mounts, "_ensure_fuse_support", new_callable=AsyncMock) as fuse_mock,
        patch.object(_daytona_mounts, "_ensure_rclone", new_callable=AsyncMock) as rclone_mock,
        patch.object(
            InContainerMountStrategy, "activate", new_callable=AsyncMock, return_value=[]
        ) as delegate_mock,
    ):
        await strategy.activate(mount, session, dest, base_dir)
        fuse_mock.assert_awaited_once_with(session)
        rclone_mock.assert_awaited_once_with(session)
        delegate_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_deactivate_delegates_without_preflights() -> None:
    strategy = DaytonaCloudBucketMountStrategy()
    mount = MagicMock()
    session = _FakePreflightSession()
    dest = Path("/workspace")
    base_dir = Path("/workspace")

    with (
        patch.object(_daytona_mounts, "_ensure_fuse_support", new_callable=AsyncMock) as fuse_mock,
        patch.object(_daytona_mounts, "_ensure_rclone", new_callable=AsyncMock) as rclone_mock,
        patch.object(
            InContainerMountStrategy, "deactivate", new_callable=AsyncMock
        ) as delegate_mock,
    ):
        await strategy.deactivate(mount, session, dest, base_dir)
        fuse_mock.assert_not_awaited()
        rclone_mock.assert_not_awaited()
        delegate_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_delegates_without_preflights() -> None:
    strategy = DaytonaCloudBucketMountStrategy()
    mount = MagicMock()
    session = _FakePreflightSession()
    path = Path("/workspace/bucket")

    with (
        patch.object(_daytona_mounts, "_ensure_fuse_support", new_callable=AsyncMock) as fuse_mock,
        patch.object(_daytona_mounts, "_ensure_rclone", new_callable=AsyncMock) as rclone_mock,
        patch.object(
            InContainerMountStrategy, "teardown_for_snapshot", new_callable=AsyncMock
        ) as delegate_mock,
    ):
        await strategy.teardown_for_snapshot(mount, session, path)
        fuse_mock.assert_not_awaited()
        rclone_mock.assert_not_awaited()
        delegate_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_after_snapshot_reruns_preflights() -> None:
    strategy = DaytonaCloudBucketMountStrategy()
    mount = MagicMock()
    session = _FakePreflightSession()
    path = Path("/workspace/bucket")

    with (
        patch.object(_daytona_mounts, "_ensure_fuse_support", new_callable=AsyncMock) as fuse_mock,
        patch.object(_daytona_mounts, "_ensure_rclone", new_callable=AsyncMock) as rclone_mock,
        patch.object(
            InContainerMountStrategy, "restore_after_snapshot", new_callable=AsyncMock
        ) as delegate_mock,
    ):
        await strategy.restore_after_snapshot(mount, session, path)
        fuse_mock.assert_awaited_once_with(session)
        rclone_mock.assert_awaited_once_with(session)
        delegate_mock.assert_awaited_once()


def test_build_docker_volume_driver_config_returns_none() -> None:
    strategy = DaytonaCloudBucketMountStrategy()
    mount = MagicMock()
    assert strategy.build_docker_volume_driver_config(mount) is None

from __future__ import annotations

import base64
import io
import tarfile
import uuid
from pathlib import Path

import pytest
from pydantic import PrivateAttr

from agents.extensions.sandbox.sandboxes.e2b import E2BSandboxSession, E2BSandboxSessionState
from agents.sandbox import Manifest
from agents.sandbox.entries import Dir, Mount
from agents.sandbox.errors import (
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceStartError,
)
from agents.sandbox.snapshot import NoopSnapshot


class _FakeE2BResult:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class _FakeE2BFiles:
    def __init__(self) -> None:
        self.make_dir_calls: list[tuple[str, float | None]] = []

    def write(
        self,
        path: str,
        data: bytes,
        request_timeout: float | None = None,
    ) -> None:
        _ = (path, data, request_timeout)

    def remove(self, path: str, request_timeout: float | None = None) -> None:
        _ = (path, request_timeout)

    def make_dir(self, path: str, request_timeout: float | None = None) -> bool:
        self.make_dir_calls.append((path, request_timeout))
        return True

    def read(self, path: str, format: str = "bytes") -> bytes:
        _ = (path, format)
        return b""


class _FakeE2BCommands:
    def __init__(self) -> None:
        self.exec_root_ready = False
        self.calls: list[dict[str, object]] = []
        self.mkdir_result: _FakeE2BResult | None = None
        self.next_result = _FakeE2BResult()

    def run(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
    ) -> _FakeE2BResult:
        self.calls.append(
            {
                "command": command,
                "timeout": timeout,
                "cwd": cwd,
                "envs": envs,
                "user": user,
            }
        )
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


class _FakeE2BSandbox:
    def __init__(self) -> None:
        self.sandbox_id = "sb-123"
        self.files = _FakeE2BFiles()
        self.commands = _FakeE2BCommands()

    def beta_pause(self) -> None:
        return

    def kill(self) -> None:
        return

    def is_running(self, request_timeout: float | None = None) -> bool:
        _ = request_timeout
        return True


class _RecordingMount(Mount):
    type: str = "recording_mount"
    _mounted_paths: list[Path] = PrivateAttr(default_factory=list)
    _unmounted_paths: list[Path] = PrivateAttr(default_factory=list)
    _events: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    def bind_events(self, events: list[tuple[str, str]]) -> _RecordingMount:
        self._events = events
        return self

    async def _mount(self, session: object, path: Path) -> None:
        _ = session
        self._events.append(("mount", str(path)))
        self._mounted_paths.append(path)

    async def _unmount(self, session: object, path: Path) -> None:
        _ = session
        self._events.append(("unmount", str(path)))
        self._unmounted_paths.append(path)


class _FailingUnmountMount(_RecordingMount):
    type: str = "failing_unmount_mount"

    async def _unmount(self, session: object, path: Path) -> None:
        _ = session
        self._events.append(("unmount_fail", str(path)))
        raise RuntimeError("boom while unmounting second mount")


class _FailingRemountMount(_RecordingMount):
    type: str = "failing_remount_mount"

    async def _mount(self, session: object, path: Path) -> None:
        _ = session
        self._events.append(("mount_fail", str(path)))
        raise RuntimeError("boom while remounting second mount")


def _session(*, workspace_root_ready: bool = False) -> tuple[E2BSandboxSession, _FakeE2BSandbox]:
    sandbox = _FakeE2BSandbox()
    state = E2BSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        sandbox_id=sandbox.sandbox_id,
        workspace_root_ready=workspace_root_ready,
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
    assert sandbox.files.make_dir_calls == [("/workspace", 10), ("/workspace", 10)]
    assert sandbox.commands.calls == [
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
async def test_e2b_skip_start_still_prepares_workspace_root_for_resumed_exec_cwd() -> None:
    session, sandbox = _session(workspace_root_ready=False)
    session._skip_start = True  # noqa: SLF001

    await session.start()
    result = await session._exec_internal("pwd", timeout=0.01)  # noqa: SLF001

    assert result.ok()
    assert session.state.workspace_root_ready is True
    assert session._workspace_root_ready is True  # noqa: SLF001
    assert sandbox.commands.calls == [
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
async def test_e2b_running_requires_workspace_root_ready() -> None:
    session, _sandbox = _session(workspace_root_ready=False)

    assert await session.running() is False


@pytest.mark.asyncio
async def test_e2b_running_checks_remote_after_workspace_ready() -> None:
    session, sandbox = _session(workspace_root_ready=True)
    sandbox.commands.exec_root_ready = True

    assert await session.running() is True


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
    session._register_persist_workspace_skip_relpath(Path("logs/events.jsonl"))  # noqa: SLF001
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

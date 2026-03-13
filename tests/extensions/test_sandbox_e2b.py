from __future__ import annotations

import base64
import io
import tarfile
import uuid
from pathlib import Path
from typing import Literal

import pytest
from pydantic import PrivateAttr

from agents.extensions.sandbox.sandboxes.e2b import E2BSandboxSession, E2BSandboxSessionState
from agents.sandbox import Manifest
from agents.sandbox.entries import Mount
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
    def write(
        self,
        path: str,
        data: bytes,
        request_timeout: float | None = None,
    ) -> None:
        _ = (path, data, request_timeout)

    def remove(self, path: str, request_timeout: float | None = None) -> None:
        _ = (path, request_timeout)

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
    type: Literal["recording_mount"] = "recording_mount"
    _mounted_paths: list[Path] = PrivateAttr(default_factory=list)
    _unmounted_paths: list[Path] = PrivateAttr(default_factory=list)

    async def _mount(self, session: object, path: Path) -> None:
        _ = session
        self._mounted_paths.append(path)

    async def _unmount(self, session: object, path: Path) -> None:
        _ = session
        self._unmounted_paths.append(path)


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

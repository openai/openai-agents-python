"""Exercise user file operations with in-memory OS and subprocess boundaries."""

from __future__ import annotations

import asyncio
import errno
import io
import os
import stat
import subprocess
import sys
import threading
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agents.sandbox.errors import (
    ExecNonZeroError,
    InvalidManifestPathError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.manifest import Manifest, SandboxPathGrant
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import User

if TYPE_CHECKING or sys.platform != "win32":
    from agents.sandbox.sandboxes import _unix_local_file_ops as ops, unix_local

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix only")


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> unix_local.UnixLocalSandboxSession:
    # Paths are synthetic; permission and lookup outcomes are supplied at the OS boundary.
    monkeypatch.setattr(Path, "resolve", lambda self, **kwargs: self)
    for name in ("open", "close", "mkdir", "unlink", "rmdir", "scandir", "fdopen"):
        monkeypatch.setattr(
            os, name, Mock(side_effect=AssertionError(f"Unexpected OS call: {name}"))
        )
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("Unexpected subprocess"))
    )
    monkeypatch.setattr(unix_local.shutil, "which", lambda command: "/usr/bin/sudo")
    return unix_local.UnixLocalSandboxSession(
        state=unix_local.UnixLocalSandboxSessionState(
            manifest=Manifest(root="/workspace"), snapshot=NoopSnapshot(id="mock-user-files")
        )
    )


def _worker(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    """Execute the shipped worker code with mocked OS calls, never start a process."""
    assert command[:4] == ["/usr/bin/sudo", "-u", "example-user", "--"]
    assert command[4:8] == ["python3", "-I", "-S", "-c"]
    assert kwargs["env"] == {"PATH": os.defpath}
    assert kwargs["cwd"] == "/"
    namespace: dict[str, Any] = {"__name__": "mock_worker"}
    exec(compile(command[8], "<trusted-user-file-worker>", "exec"), namespace)
    stdout = io.StringIO()
    # The child boundary owns these streams; the SDK caller's stream is a separate object.
    with pytest.MonkeyPatch.context() as patch, redirect_stdout(stdout):
        patch.setattr(sys, "argv", ["-c", *command[9:]])
        patch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(kwargs["input"])))
        try:
            namespace["_main"]()
        except OSError as error:
            return subprocess.CompletedProcess(command, 1, b"", str(error).encode())
    return subprocess.CompletedProcess(command, 0, stdout.getvalue().encode(), b"")


@pytest.mark.asyncio
async def test_user_write_uses_shared_traversal_and_preserves_input(
    session: unix_local.UnixLocalSandboxSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = Mock(side_effect=[10, 11, 12, 13])
    closed = Mock()
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    written: list[bytes] = []

    class Output(io.BytesIO):
        def close(self) -> None:
            written.append(self.getvalue())
            super().close()

    output = Output()
    fdopen = Mock(return_value=output)
    monkeypatch.setattr(os, "fdopen", fdopen)
    dispatch = Mock(side_effect=_worker)
    monkeypatch.setattr(subprocess, "run", dispatch)
    data = io.BytesIO(b"a\x00b\xff")
    await session.write(Path("parent/file"), data, user=User(name="example-user"))
    assert written == [b"a\x00b\xff"]
    assert not data.closed
    assert dispatch.call_count == 1
    assert [call.args[0] for call in opened.call_args_list] == ["/", "workspace", "parent", "file"]
    assert [call.kwargs for call in opened.call_args_list] == [
        {},
        {"dir_fd": 10},
        {"dir_fd": 11},
        {"dir_fd": 12},
    ]
    assert all(call.args[1] & os.O_NOFOLLOW for call in opened.call_args_list)
    assert opened.call_args.args[1] & os.O_TRUNC
    fdopen.assert_called_once_with(13, "wb")
    assert [call.args[0] for call in closed.call_args_list] == [10, 11, 12]
    assert output.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [errno.EACCES, errno.ELOOP])
@pytest.mark.parametrize("operation", ["ls", "write"])
async def test_user_lookup_failure_stops_before_file_io(
    session: unix_local.UnixLocalSandboxSession,
    monkeypatch: pytest.MonkeyPatch,
    failure: int,
    operation: str,
) -> None:
    opened = Mock(side_effect=[10, 11, OSError(failure, "Lookup refused")])
    closed = Mock()
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    dispatch = Mock(side_effect=_worker)
    monkeypatch.setattr(subprocess, "run", dispatch)
    if operation == "ls":
        with pytest.raises(ExecNonZeroError):
            await session.ls("parent/file", user="example-user")
    else:
        with pytest.raises(WorkspaceArchiveWriteError):
            await session.write(Path("parent/file"), io.BytesIO(b"value"), user="example-user")
    assert dispatch.call_count == 1
    assert [call.args[0] for call in opened.call_args_list] == ["/", "workspace", "parent"]
    assert [call.args[0] for call in closed.call_args_list] == [10, 11]


@pytest.mark.asyncio
async def test_user_leaf_permission_failure_does_not_write(
    session: unix_local.UnixLocalSandboxSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = Mock(side_effect=[10, 11, PermissionError("File is not writable")])
    closed = Mock()
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    dispatch = Mock(side_effect=_worker)
    monkeypatch.setattr(subprocess, "run", dispatch)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.write(Path("file"), io.BytesIO(b"value"), user="example-user")
    assert dispatch.call_count == 1
    assert opened.call_args.args[0] == "file"
    assert [call.args[0] for call in closed.call_args_list] == [10, 11]


@pytest.mark.asyncio
async def test_user_operation_rechecks_captured_authority(
    session: unix_local.UnixLocalSandboxSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Model a different result from symlink normalization without changing any real path.
    monkeypatch.setattr(session, "normalize_path", lambda *args, **kwargs: Path("/ungranted/file"))
    with pytest.raises(InvalidManifestPathError):
        await session.write(Path("file"), io.BytesIO(b"value"), user="example-user")
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "kind", "permissions"),
    [
        pytest.param(stat.S_IFLNK, "symlink", "-rwxrwxrwx", id="symlink"),
        pytest.param(stat.S_IFSOCK, "other", "-rwxrwxrwx", id="socket"),
        pytest.param(stat.S_IFDIR, "directory", "drwxrwxrwx", id="directory"),
    ],
)
async def test_user_listing_preserves_metadata_and_names(
    session: unix_local.UnixLocalSandboxSession,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    kind: str,
    permissions: str,
) -> None:
    monkeypatch.setattr(os, "open", Mock(side_effect=[10, 11, 12]))
    closed = Mock()
    monkeypatch.setattr(os, "close", closed)
    directory = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 501, 20, 0, 0, 0, 0))
    entry_stat = os.stat_result((mode | 0o777, 0, 0, 1, 501, 20, 12, 0, 0, 0))
    monkeypatch.setattr(os, "stat", Mock(return_value=directory))
    entry = SimpleNamespace(name="name\nwith spaces", stat=Mock(return_value=entry_stat))
    entries = Mock()
    entries.__enter__ = Mock(return_value=iter([entry]))
    entries.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(os, "scandir", Mock(return_value=entries))
    monkeypatch.setattr(ops.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="owner"))
    monkeypatch.setattr(ops.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="group"))
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=_worker))
    result = await session.ls("parent", user="example-user")
    assert len(result) == 1
    assert result[0].path == "/workspace/parent/name\nwith spaces"
    assert result[0].owner == "owner"
    assert result[0].group == "group"
    assert result[0].size == 12
    assert result[0].kind.value == kind
    assert str(result[0].permissions) == permissions
    assert result[0].permissions.directory is (kind == "directory")
    entry.stat.assert_called_once_with(follow_symlinks=False)
    assert [call.args[0] for call in closed.call_args_list] == [10, 11, 12]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ls", "write"])
async def test_ungranted_path_never_dispatches(
    session: unix_local.UnixLocalSandboxSession, operation: str
) -> None:
    with pytest.raises(InvalidManifestPathError):
        if operation == "ls":
            await session.ls("/ungranted/file", user="example-user")
        else:
            await session.write(Path("/ungranted/file"), io.BytesIO(b"value"), user="example-user")
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_only_grant_rejects_user_write_before_dispatch(
    session: unix_local.UnixLocalSandboxSession,
) -> None:
    session.state.manifest.extra_path_grants = (SandboxPathGrant(path="/shared", read_only=True),)
    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await session.write(Path("/shared/file"), io.BytesIO(b"value"), user="example-user")
    assert error.value.context["reason"] == "read_only_extra_path_grant"
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_user_runtime_reports_error_without_fallback(
    session: unix_local.UnixLocalSandboxSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatch = Mock(
        return_value=subprocess.CompletedProcess([], 1, b"", b"Python: permission denied")
    )
    monkeypatch.setattr(subprocess, "run", dispatch)
    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await session.write(Path("file"), io.BytesIO(b"value"), user="example-user")
    assert error.value.context["stderr"] == "Python: permission denied"
    assert dispatch.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ls", "write"])
async def test_user_file_operation_does_not_use_application_runtime(
    session: unix_local.UnixLocalSandboxSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setattr(sys, "executable", "/private/application/.venv/bin/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/private/application/.venv")
    monkeypatch.setenv("PATH", "/private/application/.venv/bin:/workspace/bin")
    monkeypatch.setenv("PYTHONPATH", "/workspace/python")
    dispatch = Mock(return_value=subprocess.CompletedProcess([], 0, b"[]", b""))
    monkeypatch.setattr(subprocess, "run", dispatch)
    if operation == "ls":
        assert await session.ls("directory", user="example-user") == []
    else:
        await session.write(Path("file"), io.BytesIO(b"value"), user="example-user")
    command = dispatch.call_args.args[0]
    assert command[:8] == ["/usr/bin/sudo", "-u", "example-user", "--", "python3", "-I", "-S", "-c"]
    assert sys.executable not in command
    assert dispatch.call_args.kwargs["cwd"] == "/"
    assert dispatch.call_args.kwargs["env"] == {"PATH": os.defpath}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ls", "write"])
async def test_missing_system_python_does_not_retry_with_application_runtime(
    session: unix_local.UnixLocalSandboxSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    dispatch = Mock(
        return_value=subprocess.CompletedProcess([], 1, b"", b"sudo: python3: command not found")
    )
    monkeypatch.setattr(subprocess, "run", dispatch)
    if operation == "ls":
        with pytest.raises(ExecNonZeroError):
            await session.ls("directory", user="example-user")
    else:
        with pytest.raises(WorkspaceArchiveWriteError):
            await session.write(Path("file"), io.BytesIO(b"value"), user="example-user")
    assert dispatch.call_count == 1


@pytest.mark.asyncio
async def test_cancellation_waits_for_user_worker_completion(
    session: unix_local.UnixLocalSandboxSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A thread-controlled fake process exposes the existing worker ownership boundary.
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def dispatch(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", dispatch)
    task = asyncio.create_task(
        session.write(Path("file"), io.BytesIO(b"value"), user="example-user")
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()


def test_search_only_flag_is_preferred() -> None:
    # Check the available platform primitive without opening a directory.
    if hasattr(os, "O_SEARCH"):
        assert ops._TRAVERSE_FLAGS & os.O_SEARCH == os.O_SEARCH
    elif hasattr(os, "O_PATH"):
        assert ops._TRAVERSE_FLAGS & os.O_PATH == os.O_PATH

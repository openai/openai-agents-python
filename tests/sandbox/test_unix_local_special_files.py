from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix only")


def _fifo_probe(root: str, operation: str, replacement: str) -> None:
    """Run public file operations in a child so blocking-open regressions are bounded."""
    import asyncio
    import io
    import os
    import pwd
    import subprocess
    import sys
    from pathlib import Path
    from unittest.mock import patch

    from agents.sandbox.apply_patch import WorkspaceEditor
    from agents.sandbox.errors import WorkspaceArchiveReadError, WorkspaceArchiveWriteError
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )
    from agents.sandbox.snapshot import NoopSnapshot

    workspace = Path(root).resolve()
    target = workspace / "pipe"
    if replacement == "swap":
        target.write_bytes(b"original")
    else:
        os.mkfifo(target)
    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(workspace)), snapshot=NoopSnapshot(id="fifo-probe")
        )
    )
    real_open = os.open
    real_run = subprocess.run
    username = pwd.getpwuid(os.geteuid()).pw_name
    peer: int | None = None
    rejected_fd: int | None = None

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal peer, rejected_fd
        if path == "pipe":
            target.rename(workspace / "original")
            os.mkfifo(target)
            # A peer makes the writable FIFO open succeed, so fstat must reject it
            # without writing any bytes. Without O_NONBLOCK, the no-peer cases hang.
            peer = real_open(target, os.O_RDWR | os.O_NONBLOCK)
            rejected_fd = real_open(path, flags, *args, **kwargs)
            return rejected_fd
        return real_open(path, flags, *args, **kwargs)

    def run_as_current_user(command, **kwargs):
        # Exercise the shipped worker and public dispatcher without requiring sudo.
        # The requested identity is already this process's effective identity.
        assert command[1:4] == ["-u", username, "--"]
        assert command[4:8] == ["python3", "-I", "-S", "-c"]
        return real_run([sys.executable, *command[5:]], timeout=5, **kwargs)

    async def exercise():
        try:
            if operation == "read":
                with await session.read(Path("pipe")):
                    raise AssertionError("FIFO returned as a readable file")
            elif operation == "delete":
                await WorkspaceEditor(session).apply_patch({"type": "delete_file", "path": "pipe"})
            else:
                payload = io.BytesIO(b"payload")
                with (
                    patch("shutil.which", return_value="/usr/bin/sudo"),
                    patch("subprocess.run", side_effect=run_as_current_user),
                ):
                    await session.write(
                        Path("pipe"), payload, user=username if operation == "user-write" else None
                    )
                assert not payload.closed
        except (WorkspaceArchiveReadError, WorkspaceArchiveWriteError):
            return
        raise AssertionError("FIFO operation unexpectedly succeeded")

    try:
        with patch("os.open", side_effect=swapping_open if replacement == "swap" else real_open):
            asyncio.run(exercise())
        assert target.is_fifo()
        if replacement == "swap":
            assert (workspace / "original").read_bytes() == b"original"
            assert rejected_fd is not None
            try:
                os.fstat(rejected_fd)
            except OSError:
                pass
            else:
                raise AssertionError("Rejected FIFO descriptor leaked")
            assert peer is not None
            try:
                os.read(peer, 1)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("Rejected FIFO received payload")
    finally:
        if peer is not None:
            os.close(peer)


@pytest.mark.parametrize("operation", ["read", "write", "user-write", "delete"])
def test_public_file_operations_reject_peerless_fifo(tmp_path: Path, operation: str) -> None:
    source = inspect.getsource(_fifo_probe) + '\n_fifo_probe(*__import__("sys").argv[1:])'
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path), operation, "stable"],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("operation", ["read", "write"])
def test_fifo_replacement_is_rejected_before_io(tmp_path: Path, operation: str) -> None:
    source = inspect.getsource(_fifo_probe) + '\n_fifo_probe(*__import__("sys").argv[1:])'
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path), operation, "swap"],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.asyncio
async def test_regular_write_preserves_inode_permissions_and_umask(tmp_path: Path) -> None:
    import io

    from .test_unix_local_file_io import _session

    session = _session(tmp_path)
    target = tmp_path / "file"
    target.write_bytes(b"long original contents")
    alias = tmp_path / "hard-link"
    os.link(target, alias)
    target.chmod(0o200)
    before = target.stat()
    tmp_path.chmod(0o500)
    try:
        await session.write(Path("file"), io.BytesIO(b"new"))
        after = target.stat()
        assert (after.st_ino, after.st_uid, after.st_gid, after.st_mode) == (
            before.st_ino,
            before.st_uid,
            before.st_gid,
            before.st_mode,
        )
    finally:
        tmp_path.chmod(0o700)
        target.chmod(0o600)
    assert alias.read_bytes() == b"new"

    previous_umask = os.umask(0o077)
    try:
        await session.write(Path("new"), io.BytesIO(b"private"))
    finally:
        os.umask(previous_umask)
    assert (tmp_path / "new").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["socket", "device"])
async def test_stable_special_files_are_rejected_without_io_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import io
    import socket
    import tempfile

    from agents.sandbox.errors import WorkspaceArchiveReadError, WorkspaceArchiveWriteError
    from agents.sandbox.manifest import SandboxPathGrant

    from .test_unix_local_file_io import _session

    with tempfile.TemporaryDirectory() as short_dir, socket.socket(socket.AF_UNIX) as sock:
        # Keep the Unix socket address short enough on macOS.
        root = Path(short_dir)
        target = root / "s" if kind == "socket" else Path("/dev/null")
        if kind == "socket":
            sock.bind(str(target))
        session = _session(root, grants=(SandboxPathGrant(path="/dev/null"),))
        real_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            assert path != target.name, "special-file leaf must not be opened"
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", guarded_open)
        with pytest.raises(WorkspaceArchiveReadError):
            await session.read(target)
        with pytest.raises(WorkspaceArchiveWriteError):
            await session.write(target, io.BytesIO(b"payload"))


def _lease_client(root: str, operation: str) -> None:
    import asyncio
    import errno
    import io
    from pathlib import Path

    from agents.sandbox.errors import WorkspaceArchiveReadError, WorkspaceArchiveWriteError
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )
    from agents.sandbox.snapshot import NoopSnapshot

    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=root), snapshot=NoopSnapshot(id="lease-probe")
        )
    )

    async def exercise():
        try:
            if operation == "read":
                with await session.read(Path("file")):
                    pass
            else:
                await session.write(Path("file"), io.BytesIO(b"updated"))
        except (WorkspaceArchiveReadError, WorkspaceArchiveWriteError) as exc:
            assert isinstance(exc.__cause__, OSError)
            assert exc.__cause__.errno == errno.EWOULDBLOCK
            return
        raise AssertionError("A conflicting lease must be rejected")

    asyncio.run(exercise())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux file leases")
@pytest.mark.parametrize("operation", ["read", "write"])
def test_regular_file_lease_rejected(tmp_path: Path, operation: str) -> None:
    import fcntl
    import signal

    target = tmp_path / "file"
    target.write_bytes(b"original")
    notified = False

    def on_lease_break(_signum, _frame):
        nonlocal notified
        notified = True

    previous_handler = signal.signal(signal.SIGIO, on_lease_break)
    source = inspect.getsource(_lease_client) + '\n_lease_client(*__import__("sys").argv[1:])'
    try:
        with target.open("r+b") as lease:
            fcntl.fcntl(lease, fcntl.F_SETLEASE, fcntl.F_WRLCK)
            with subprocess.Popen(
                [sys.executable, "-c", source, str(tmp_path), operation],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as client:
                try:
                    # Keep the lease held until the public operation has failed. An
                    # external watchdog bounds regressions without blocking pytest.
                    _, stderr = client.communicate(timeout=10)
                    assert client.returncode == 0, stderr.decode()
                    assert notified, "Client did not request a lease break"
                finally:
                    if client.poll() is None:
                        client.kill()
                    client.wait()
                    fcntl.fcntl(lease, fcntl.F_SETLEASE, fcntl.F_UNLCK)
    finally:
        signal.signal(signal.SIGIO, previous_handler)
    assert target.read_bytes() == b"original"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_conflicting_lease_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import errno
    import io
    from types import SimpleNamespace

    from agents.sandbox.errors import WorkspaceArchiveReadError, WorkspaceArchiveWriteError
    from agents.sandbox.sandboxes import _unix_local_file_ops as file_ops

    from .test_unix_local_file_io import _session

    target = tmp_path / "file"
    target.write_bytes(b"original")
    real_open = os.open
    attempts = 0

    def leased_open(path, flags, *args, **kwargs):
        nonlocal attempts
        if path == "file":
            attempts += 1
            assert attempts == 1, "A conflicting lease must not be retried"
            raise BlockingIOError(errno.EWOULDBLOCK, "Conflicting lease")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(file_ops, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(os, "open", leased_open)
    payload = io.BytesIO(b"updated")
    error = WorkspaceArchiveReadError if operation == "read" else WorkspaceArchiveWriteError
    with pytest.raises(error) as caught:
        if operation == "read":
            await _session(tmp_path).read(Path("file"))
        else:
            await _session(tmp_path).write(Path("file"), payload)
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.EWOULDBLOCK
    assert attempts == 1
    assert not payload.closed
    assert payload.tell() == 0
    assert target.read_bytes() == b"original"

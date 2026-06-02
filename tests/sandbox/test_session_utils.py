from __future__ import annotations

import io
import shlex
import uuid
from pathlib import Path

import pytest

from agents.sandbox.entries import GCSMount, InContainerMountStrategy, MountpointMountPattern
from agents.sandbox.errors import MountConfigError
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import SandboxSessionStartEvent
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.events import SandboxSessionFinishEvent
from agents.sandbox.session.utils import (
    _best_effort_stream_len,
    _safe_decode,
    event_to_json_line,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, Permissions, User
from tests.utils.factories import TestSessionState


class _CaptureExecSession(BaseSandboxSession):
    def __init__(self) -> None:
        self.state = TestSessionState(
            manifest=Manifest(),
            snapshot=NoopSnapshot(id="noop"),
        )
        self.last_command: tuple[str, ...] | None = None

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        self.last_command = tuple(str(part) for part in command)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def read(self, path: Path, *, user: object = None) -> io.IOBase:
        _ = (path, user)
        raise AssertionError("read() should not be called in this test")

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)
        raise AssertionError("write() should not be called in this test")

    async def running(self) -> bool:
        return True

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def shutdown(self) -> None:
        return


class _ManifestSession(_CaptureExecSession):
    def __init__(self, manifest: Manifest) -> None:
        super().__init__()
        self.state = TestSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id="noop"),
        )


def test_safe_decode_truncates_and_appends_ellipsis() -> None:
    assert _safe_decode(b"abcdef", max_chars=3) == "abc…"


def test_best_effort_stream_len_tracks_remaining_bytes_for_seekable_streams() -> None:
    buffer = io.BytesIO(b"hello")
    assert _best_effort_stream_len(buffer) == 5
    assert buffer.read(1) == b"h"
    assert _best_effort_stream_len(buffer) == 4


class _NoSeekableMethodStream(io.IOBase):
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def tell(self) -> int:
        return self._buffer.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._buffer.seek(offset, whence)


def test_best_effort_stream_len_handles_streams_without_seekable_method() -> None:
    stream = _NoSeekableMethodStream(b"hello")

    assert _best_effort_stream_len(stream) == 5
    stream.seek(2)
    assert _best_effort_stream_len(stream) == 3


def test_event_to_json_line_is_single_line() -> None:
    event = SandboxSessionStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="write",
        span_id="span_write",
        data={"x": 1},
    )

    line = event_to_json_line(event)
    assert line.endswith("\n")
    assert "\n" not in line[:-1]


def test_sandbox_session_finish_event_excludes_raw_bytes_from_json_dump() -> None:
    event = SandboxSessionFinishEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="exec",
        span_id="span_exec",
        ok=True,
        duration_ms=0.0,
    )
    event.stdout_bytes = b"secret"
    event.stderr_bytes = b"secret2"

    dumped = event.model_dump(mode="json")
    assert "stdout_bytes" not in dumped
    assert "stderr_bytes" not in dumped


def test_file_entry_is_dir_uses_kind() -> None:
    directory_entry = FileEntry(
        path="/workspace/dir",
        permissions=Permissions.from_str("drwxr-xr-x"),
        owner="root",
        group="root",
        size=0,
        kind=EntryKind.DIRECTORY,
    )
    file_entry = FileEntry(
        path="/workspace/file.txt",
        permissions=Permissions.from_str("-rw-r--r--"),
        owner="root",
        group="root",
        size=3,
        kind=EntryKind.FILE,
    )

    assert directory_entry.is_dir() is True
    assert file_entry.is_dir() is False


@pytest.mark.asyncio
async def test_exec_shell_true_quotes_multi_arg_commands() -> None:
    session = _CaptureExecSession()

    await session.exec("printf", "%s\n", "hello world", "$(whoami)", "semi;colon", shell=True)

    assert session.last_command == (
        "sh",
        "-lc",
        shlex.join(["printf", "%s\n", "hello world", "$(whoami)", "semi;colon"]),
    )


@pytest.mark.asyncio
async def test_exec_shell_true_preserves_single_shell_snippet() -> None:
    session = _CaptureExecSession()

    await session.exec("echo hello && echo goodbye", shell=True)

    assert session.last_command == ("sh", "-lc", "echo hello && echo goodbye")


@pytest.mark.asyncio
async def test_check_mkdir_with_exec_runs_non_destructive_probe_as_user() -> None:
    session = _CaptureExecSession()

    checked_path = await session._check_mkdir_with_exec(
        Path("nested/dir"),
        parents=True,
        user=User(name="sandbox-user"),
    )

    assert checked_path == Path("/workspace/nested/dir")
    assert session.last_command is not None
    assert session.last_command[:4] == ("sudo", "-u", "sandbox-user", "--")
    assert session.last_command[4:6] == ("sh", "-lc")
    assert session.last_command[-2:] == ("/workspace/nested/dir", "1")


@pytest.mark.asyncio
async def test_check_rm_with_exec_runs_parent_write_probe_as_user() -> None:
    session = _CaptureExecSession()

    checked_path = await session._check_rm_with_exec(
        Path("stale.txt"),
        recursive=False,
        user=User(name="sandbox-user"),
    )

    assert checked_path == Path("/workspace/stale.txt")
    assert session.last_command is not None
    assert session.last_command[:4] == ("sudo", "-u", "sandbox-user", "--")
    assert session.last_command[4:6] == ("sh", "-lc")
    assert session.last_command[-2:] == ("/workspace/stale.txt", "0")


@pytest.mark.parametrize(
    ("skip_path", "mount_path"),
    [
        ("data", "data"),
        ("logs", "logs/remote"),
        ("data/tmp", "data"),
    ],
)
def test_register_persist_workspace_skip_path_rejects_mount_overlaps(
    skip_path: str,
    mount_path: str,
) -> None:
    session = _ManifestSession(
        Manifest(
            root="/workspace",
            entries={
                "remote": GCSMount(
                    bucket="bucket",
                    mount_path=Path(mount_path),
                    mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                )
            },
        )
    )

    with pytest.raises(MountConfigError) as exc_info:
        session.register_persist_workspace_skip_path(skip_path)

    assert str(exc_info.value) == "persist workspace skip path must not overlap mount path"


def test_register_persist_workspace_skip_path_allows_non_overlapping_path() -> None:
    session = _ManifestSession(
        Manifest(
            root="/workspace",
            entries={
                "remote": GCSMount(
                    bucket="bucket",
                    mount_path=Path("data"),
                    mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                )
            },
        )
    )

    registered = session.register_persist_workspace_skip_path("logs/events.jsonl")

    assert registered == Path("logs/events.jsonl")

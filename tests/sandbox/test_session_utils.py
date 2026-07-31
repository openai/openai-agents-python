from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agents.sandbox.entries import GCSMount, InContainerMountStrategy, MountpointMountPattern
from agents.sandbox.errors import (
    MountConfigError,
    WorkspaceArchiveReadError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import SandboxSessionStartEvent
from agents.sandbox.session.base_sandbox_session import (
    _READ_PATH_PROBE_SCRIPT,
    _READ_PATH_PROBE_TIMEOUT_S,
    BaseSandboxSession,
)
from agents.sandbox.session.events import SandboxSessionFinishEvent, validate_sandbox_session_event
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


class _QueuedExecSession(_CaptureExecSession):
    def __init__(self, results: list[ExecResult]) -> None:
        super().__init__()
        self._results = list(results)
        self.commands: list[tuple[str, ...]] = []
        self.timeouts: list[float | None] = []

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        self.commands.append(tuple(str(part) for part in command))
        self.timeouts.append(timeout)
        return self._results.pop(0)


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


def test_validate_sandbox_session_event_uses_phase_discriminator() -> None:
    event = SandboxSessionStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="read",
        span_id="span_read",
    )

    restored = validate_sandbox_session_event(event.model_dump(mode="json"))

    assert isinstance(restored, SandboxSessionStartEvent)
    assert restored.phase == "start"
    assert restored.op == "read"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_exit_code", "expected_error"),
    [
        (0, WorkspaceArchiveReadError),
        (1, WorkspaceReadNotFoundError),
        (2, WorkspaceArchiveReadError),
    ],
)
async def test_check_read_with_exec_classifies_failure_as_requested_user(
    probe_exit_code: int,
    expected_error: type[Exception],
) -> None:
    session = _QueuedExecSession(
        [
            ExecResult(stdout=b"", stderr=b"not readable", exit_code=1),
            ExecResult(stdout=b"", stderr=b"", exit_code=probe_exit_code),
        ]
    )

    with pytest.raises(expected_error):
        await session._check_read_with_exec(
            Path("target.txt"),
            user=User(name="sandbox-user"),
        )

    assert len(session.commands) == 2
    assert all(command[:4] == ("sudo", "-u", "sandbox-user", "--") for command in session.commands)
    assert "READ_PATH_PROBE_V3" in session.commands[1][6]
    assert session.timeouts == [None, _READ_PATH_PROBE_TIMEOUT_S]


@pytest.mark.asyncio
async def test_check_read_with_exec_treats_nonstandard_check_exit_as_archive_error() -> None:
    session = _QueuedExecSession([ExecResult(stdout=b"", stderr=b"check failed", exit_code=127)])

    with pytest.raises(WorkspaceArchiveReadError):
        await session._check_read_with_exec(Path("target.txt"))

    assert len(session.commands) == 1


@pytest.mark.asyncio
async def test_read_error_context_does_not_retain_partial_stdout() -> None:
    partial_stdout = b"sensitive partial contents" * 1024
    session = _QueuedExecSession(
        [
            ExecResult(stdout=partial_stdout, stderr=b"read failed", exit_code=1),
            ExecResult(stdout=b"", stderr=b"", exit_code=2),
        ]
    )

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session._check_read_with_exec(Path("target.txt"))

    assert "stdout" not in exc_info.value.context
    assert exc_info.value.context["stdout_bytes"] == len(partial_stdout)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell behavior is Unix-specific")
def test_read_path_probe_resolves_symlinks_before_classifying_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dangling = workspace / "dangling"
    dangling.symlink_to("missing")
    non_directory = workspace / "not-a-directory"
    non_directory.write_text("content", encoding="utf-8")
    invalid_target = workspace / "invalid-target"
    invalid_target.symlink_to("not-a-directory/child")
    dangling_parent = workspace / "dangling-parent"
    dangling_parent.symlink_to("missing-directory")
    invalid_parent = workspace / "invalid-parent"
    invalid_parent.symlink_to("not-a-directory")
    loop = workspace / "loop"
    loop.symlink_to("loop")
    newline_target = workspace / "newline-target\n"
    newline_target.write_text("content", encoding="utf-8")
    newline_link = workspace / "newline-link"
    newline_link.symlink_to(newline_target.name)
    (workspace / "a").write_text("sibling", encoding="utf-8")
    current = workspace
    symlink_parts: list[str] = []
    for index in range(41):
        real = current / f"real-{index}"
        real.mkdir()
        link_name = f"link-{index}"
        (current / link_name).symlink_to(real.name, target_is_directory=True)
        symlink_parts.append(link_name)
        current = real

    def probe(path: Path, *, env: dict[str, str] | None = None) -> int:
        result = subprocess.run(
            ["sh", "-c", _READ_PATH_PROBE_SCRIPT, "sh", str(path)],
            check=False,
            capture_output=True,
            env=env,
            timeout=5,
        )
        return result.returncode

    assert probe(dangling) == 1
    assert probe(invalid_target) == 2
    assert probe(dangling_parent / "child") == 1
    assert probe(invalid_parent / "child") == 2
    assert probe(loop) == 2
    assert probe(newline_link) == 0
    assert probe(workspace / "[a]") == 1
    assert probe(workspace / "?") == 1
    assert probe(workspace / "*") == 1
    assert probe(workspace.joinpath(*symlink_parts, "missing")) == 2
    assert probe(workspace / ("x" * 256)) == 2

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_find = fake_bin / "find"
    find_args_log = tmp_path / "find-args.log"
    fake_find.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$FIND_ARGS_LOG"\n'
        'printf "find: %s: Input/output error\\n" "$1" >&2\nexit 1\n',
        encoding="utf-8",
    )
    fake_find.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FIND_ARGS_LOG"] = str(find_args_log)
    missing_path = workspace / "missing"
    assert probe(missing_path, env=env) == 2
    assert find_args_log.read_text(encoding="utf-8").splitlines() == [
        str(missing_path),
        "-prune",
        "-print",
    ]
    fake_find.write_text("#!/bin/sh\nprintf match\n", encoding="utf-8")
    assert probe(missing_path, env=env) == 2


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

from __future__ import annotations

import io
import shlex
import uuid
from pathlib import Path

import pytest

from agents.sandbox.entries.codex import resolve_codex_target_triple_for_target
from agents.sandbox.errors import UnsupportedCodexTargetError
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import UCStartEvent
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.events import UCFinishEvent
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.session.utils import (
    _best_effort_stream_len,
    _safe_decode,
    event_to_json_line,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, Permissions


class _CaptureExecSession(BaseSandboxSession):
    def __init__(self) -> None:
        self.state = SandboxSessionState(
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

    async def read(self, path: Path) -> io.IOBase:
        _ = path
        raise AssertionError("read() should not be called in this test")

    async def write(self, path: Path, data: io.IOBase) -> None:
        _ = (path, data)
        raise AssertionError("write() should not be called in this test")

    async def running(self) -> bool:
        return True

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def shutdown(self) -> None:
        return


class _ScriptedExecSession(BaseSandboxSession):
    def __init__(self, responses: dict[tuple[str, ...], list[ExecResult] | ExecResult]) -> None:
        self.state = SandboxSessionState(
            manifest=Manifest(),
            snapshot=NoopSnapshot(id="noop"),
        )
        self.responses: dict[tuple[str, ...], list[ExecResult]] = {}
        for command, response in responses.items():
            if isinstance(response, ExecResult):
                self.responses[command] = [response]
            else:
                self.responses[command] = list(response)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        key = tuple(str(part) for part in command)
        if key not in self.responses or not self.responses[key]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=1)
        return self.responses[key].pop(0)

    async def read(self, path: Path) -> io.IOBase:
        _ = path
        raise AssertionError("read() should not be called in this test")

    async def write(self, path: Path, data: io.IOBase) -> None:
        _ = (path, data)
        raise AssertionError("write() should not be called in this test")

    async def running(self) -> bool:
        return True

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def shutdown(self) -> None:
        return


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
    event = UCStartEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="write",
        span_id=uuid.uuid4(),
        data={"x": 1},
    )

    line = event_to_json_line(event)
    assert line.endswith("\n")
    assert "\n" not in line[:-1]


def test_uc_finish_event_excludes_raw_bytes_from_json_dump() -> None:
    event = UCFinishEvent(
        session_id=uuid.uuid4(),
        seq=1,
        op="exec",
        span_id=uuid.uuid4(),
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
async def test_resolve_codex_github_asset_name_linux_gnu() -> None:
    session = _ScriptedExecSession(
        {
            ("uname", "-s"): ExecResult(stdout=b"Linux\n", stderr=b"", exit_code=0),
            ("uname", "-m"): ExecResult(stdout=b"x86_64\n", stderr=b"", exit_code=0),
            ("getconf", "GNU_LIBC_VERSION"): ExecResult(
                stdout=b"glibc 2.39\n",
                stderr=b"",
                exit_code=0,
            ),
        }
    )

    assert (
        await session.resolve_codex_github_asset_name() == "codex-x86_64-unknown-linux-gnu.tar.gz"
    )


@pytest.mark.asyncio
async def test_resolve_codex_github_asset_name_linux_musl() -> None:
    session = _ScriptedExecSession(
        {
            ("uname", "-s"): ExecResult(stdout=b"Linux\n", stderr=b"", exit_code=0),
            ("uname", "-m"): ExecResult(stdout=b"amd64\n", stderr=b"", exit_code=0),
            ("getconf", "GNU_LIBC_VERSION"): ExecResult(stdout=b"", stderr=b"", exit_code=1),
            ("ldd", "--version"): ExecResult(
                stdout=b"",
                stderr=b"musl libc (x86_64)\n",
                exit_code=1,
            ),
        }
    )

    assert (
        await session.resolve_codex_github_asset_name() == "codex-x86_64-unknown-linux-musl.tar.gz"
    )


@pytest.mark.asyncio
async def test_resolve_codex_github_asset_name_linux_aarch64_gnu() -> None:
    session = _ScriptedExecSession(
        {
            ("uname", "-s"): ExecResult(stdout=b"Linux\n", stderr=b"", exit_code=0),
            ("uname", "-m"): ExecResult(stdout=b"aarch64\n", stderr=b"", exit_code=0),
            ("getconf", "GNU_LIBC_VERSION"): ExecResult(
                stdout=b"glibc 2.39\n",
                stderr=b"",
                exit_code=0,
            ),
        }
    )

    assert (
        await session.resolve_codex_github_asset_name() == "codex-aarch64-unknown-linux-gnu.tar.gz"
    )


@pytest.mark.asyncio
async def test_resolve_codex_github_asset_name_darwin() -> None:
    session = _ScriptedExecSession(
        {
            ("uname", "-s"): ExecResult(stdout=b"Darwin\n", stderr=b"", exit_code=0),
            ("uname", "-m"): ExecResult(stdout=b"x86_64\n", stderr=b"", exit_code=0),
        }
    )

    assert await session.resolve_codex_github_asset_name() == "codex-x86_64-apple-darwin.tar.gz"


@pytest.mark.asyncio
async def test_resolve_codex_github_asset_name_darwin_arm64() -> None:
    session = _ScriptedExecSession(
        {
            ("uname", "-s"): ExecResult(stdout=b"Darwin\n", stderr=b"", exit_code=0),
            ("uname", "-m"): ExecResult(stdout=b"arm64\n", stderr=b"", exit_code=0),
        }
    )

    assert await session.resolve_codex_github_asset_name() == "codex-aarch64-apple-darwin.tar.gz"


@pytest.mark.asyncio
async def test_resolve_codex_github_asset_name_windows() -> None:
    session = _ScriptedExecSession(
        {
            ("uname", "-s"): ExecResult(stdout=b"", stderr=b"", exit_code=1),
            ("cmd", "/c", "echo", "%OS%"): ExecResult(
                stdout=b"Windows_NT\r\n",
                stderr=b"",
                exit_code=0,
            ),
            ("cmd", "/c", "echo", "%PROCESSOR_ARCHITECTURE%"): ExecResult(
                stdout=b"AMD64\r\n",
                stderr=b"",
                exit_code=0,
            ),
        }
    )

    assert (
        await session.resolve_codex_github_asset_name() == "codex-x86_64-pc-windows-msvc.exe.tar.gz"
    )


@pytest.mark.asyncio
async def test_resolve_codex_github_asset_name_windows_arm64() -> None:
    session = _ScriptedExecSession(
        {
            ("uname", "-s"): ExecResult(stdout=b"", stderr=b"", exit_code=1),
            ("cmd", "/c", "echo", "%OS%"): ExecResult(
                stdout=b"Windows_NT\r\n",
                stderr=b"",
                exit_code=0,
            ),
            ("cmd", "/c", "echo", "%PROCESSOR_ARCHITECTURE%"): ExecResult(
                stdout=b"ARM64\r\n",
                stderr=b"",
                exit_code=0,
            ),
        }
    )

    assert (
        await session.resolve_codex_github_asset_name()
        == "codex-aarch64-pc-windows-msvc.exe.tar.gz"
    )


def test_resolve_codex_target_triple_reports_unsupported_os() -> None:
    with pytest.raises(
        UnsupportedCodexTargetError,
        match=(
            "Unsupported Codex target operating system: freebsd. "
            "Available operating systems: linux, darwin, windows."
        ),
    ) as exc_info:
        resolve_codex_target_triple_for_target(
            target_os="freebsd",
            target_arch="x86_64",
        )

    assert exc_info.value.reason == "operating_system"
    assert exc_info.value.target_os == "freebsd"
    assert exc_info.value.supported_operating_systems == ("linux", "darwin", "windows")


def test_resolve_codex_target_triple_reports_unsupported_architecture() -> None:
    with pytest.raises(
        UnsupportedCodexTargetError,
        match=(
            "Unsupported Codex target architecture for darwin: ppc64le. "
            "Available architectures: x86_64, aarch64."
        ),
    ) as exc_info:
        resolve_codex_target_triple_for_target(
            target_os="darwin",
            target_arch="ppc64le",
        )

    assert exc_info.value.reason == "architecture"
    assert exc_info.value.target_arch == "ppc64le"
    assert exc_info.value.supported_architectures == ("x86_64", "aarch64")


def test_resolve_codex_target_triple_normalizes_arm_aliases() -> None:
    assert (
        resolve_codex_target_triple_for_target(
            target_os="darwin",
            target_arch="arm64",
        )
        == "aarch64-apple-darwin"
    )


def test_resolve_codex_target_triple_reports_unsupported_linux_libc() -> None:
    with pytest.raises(
        UnsupportedCodexTargetError,
        match=(
            "Unsupported Linux libc variant for Codex target resolution: uclibc. "
            "Available libc variants: gnu, musl."
        ),
    ) as exc_info:
        resolve_codex_target_triple_for_target(
            target_os="linux",
            target_arch="x86_64",
            linux_libc="uclibc",
        )

    assert exc_info.value.reason == "linux_libc"
    assert exc_info.value.linux_libc == "uclibc"
    assert exc_info.value.supported_linux_libc_variants == ("gnu", "musl")

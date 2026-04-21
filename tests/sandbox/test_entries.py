from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import pytest

import agents.sandbox.entries.artifacts as artifacts_module
from agents.sandbox import SandboxConcurrencyLimits
from agents.sandbox.entries import Dir, File, GitRepo, LocalDir, LocalFile
from agents.sandbox.errors import (
    ExecNonZeroError,
    LocalDirReadError,
    LocalFileReadError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.workspace_payloads import coerce_write_payload
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, User
from tests.utils.factories import TestSessionState


class _RecordingSession(BaseSandboxSession):
    def __init__(self, manifest: Manifest | None = None) -> None:
        self.state = TestSessionState(
            manifest=manifest or Manifest(),
            snapshot=NoopSnapshot(id="noop"),
        )
        self.exec_calls: list[tuple[str, ...]] = []
        self.writes: dict[Path, bytes] = {}

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = tuple(str(part) for part in command)
        self.exec_calls.append(cmd)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def read(self, path: Path, *, user: object = None) -> io.IOBase:
        _ = user
        return io.BytesIO(self.writes[path])

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = user
        self.writes[path] = data.read()

    async def running(self) -> bool:
        return True

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def shutdown(self) -> None:
        return


class _GitRefSession(_RecordingSession):
    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = tuple(str(part) for part in command)
        self.exec_calls.append(cmd)
        if cmd == ("command -v git >/dev/null 2>&1",):
            return ExecResult(stdout=b"/usr/bin/git\n", stderr=b"", exit_code=0)
        if cmd[:2] == ("git", "clone"):
            return ExecResult(stdout=b"", stderr=b"unexpected clone path", exit_code=1)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)


class _MetadataFailureSession(_RecordingSession):
    def __init__(
        self,
        manifest: Manifest | None = None,
        *,
        fail_commands: set[str],
    ) -> None:
        super().__init__(manifest)
        self.fail_commands = fail_commands

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = tuple(str(part) for part in command)
        self.exec_calls.append(cmd)
        if cmd and cmd[0] in self.fail_commands:
            return ExecResult(stdout=b"", stderr=b"metadata failed", exit_code=1)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)


class _MutatingWriteSession(_RecordingSession):
    def __init__(self, mutate_before_read: Callable[[], None]) -> None:
        super().__init__()
        self._mutate_before_read = mutate_before_read
        self._mutated = False

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        if not self._mutated:
            self._mutate_before_read()
            self._mutated = True
        await super().write(path, data, user=user)


class _ChunkedMutatingWriteSession(_RecordingSession):
    def __init__(self, mutate_after_first_chunk: Callable[[], None]) -> None:
        super().__init__()
        self._mutate_after_first_chunk = mutate_after_first_chunk
        self._mutated = False

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = user
        chunks: list[bytes] = []
        first = data.read(4)
        if isinstance(first, bytes):
            chunks.append(first)
        if not self._mutated:
            self._mutate_after_first_chunk()
            self._mutated = True
        rest = data.read()
        if isinstance(rest, bytes):
            chunks.append(rest)
        self.writes[path] = b"".join(chunks)


class _PayloadWrappingWriteSession(_RecordingSession):
    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = user
        payload = coerce_write_payload(path=path, data=data)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = payload.stream.read(4)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=path, cause=e) from e
        self.writes[path] = b"".join(chunks)


class _StagedFailureAfterReadSession(_RecordingSession):
    def __init__(self) -> None:
        super().__init__()
        self.removed: list[Path] = []
        self.staged_writes: dict[Path, bytes] = {}

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = user
        chunks: list[bytes] = []
        while True:
            chunk = data.read(4)
            if not chunk:
                break
            chunks.append(chunk)
        staged_path = path.with_name(f".{path.name}.staged")
        self.staged_writes[staged_path] = b"".join(chunks)
        raise WorkspaceArchiveWriteError(
            path=path,
            context={"reason": "final_install_failed"},
        )

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: object = None,
    ) -> None:
        _ = recursive, user
        normalized = Path(path)
        self.removed.append(normalized)
        self.writes.pop(normalized, None)


class _FailAfterChunkStream(io.BytesIO):
    def __init__(self, data: bytes, *, owned_fd: int | None = None) -> None:
        super().__init__(data)
        self._owned_fd = owned_fd
        self._read_count = 0

    def read(self, size: int | None = -1) -> bytes:
        if self._read_count > 0:
            raise OSError("source read failed")
        self._read_count += 1
        return super().read(-1 if size is None else size)

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._owned_fd is not None:
                os.close(self._owned_fd)
                self._owned_fd = None


def _symlink_or_skip(path: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as e:
        if os.name == "nt" and getattr(e, "winerror", None) == 1314:
            pytest.skip("symlink creation requires elevated privileges on Windows")
        raise


@pytest.mark.asyncio
async def test_base_sandbox_session_uses_current_working_directory_for_local_file_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = _RecordingSession(
        Manifest(
            entries={"copied.txt": LocalFile(src=Path("source.txt"))},
        ),
    )

    result = await session.apply_manifest()

    assert result.files[0].path == Path("/workspace/copied.txt")
    assert result.files[0].sha256 == hashlib.sha256(b"hello").hexdigest()
    assert session.writes[Path("/workspace/copied.txt")] == b"hello"


@pytest.mark.asyncio
async def test_local_file_checksum_matches_written_bytes_when_source_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"original")

    def mutate_source() -> None:
        source.write_bytes(b"mutated")

    session = _ChunkedMutatingWriteSession(mutate_source)

    result = await LocalFile(src=Path("source.txt")).apply(
        session,
        Path("/workspace/copied.txt"),
        tmp_path,
    )

    written = session.writes[Path("/workspace/copied.txt")]
    assert result[0].sha256 == hashlib.sha256(written).hexdigest()


@pytest.mark.asyncio
async def test_local_file_does_not_remove_existing_destination_when_staged_write_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"new content")
    dest = Path("/workspace/copied.txt")
    session = _StagedFailureAfterReadSession()
    session.writes[dest] = b"old content"

    with pytest.raises(WorkspaceArchiveWriteError):
        await LocalFile(src=Path("source.txt")).apply(session, dest, tmp_path)

    assert session.writes[dest] == b"old content"
    assert session.removed == []
    assert session.staged_writes[Path("/workspace/.copied.txt.staged")] == b"new content"


@pytest.mark.asyncio
async def test_local_file_rejects_symlinked_source_ancestors(tmp_path: Path) -> None:
    target_dir = tmp_path / "secret-dir"
    target_dir.mkdir()
    nested_dir = target_dir / "sub"
    nested_dir.mkdir()
    (nested_dir / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink_or_skip(tmp_path / "link", target_dir, target_is_directory=True)
    session = _RecordingSession()

    with pytest.raises(LocalFileReadError) as excinfo:
        await LocalFile(src=Path("link/sub/secret.txt")).apply(
            session,
            Path("/workspace/copied.txt"),
            tmp_path,
        )

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "link"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_file_rejects_symlinked_source_leaf(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    _symlink_or_skip(tmp_path / "link.txt", secret)
    session = _RecordingSession()

    with pytest.raises(LocalFileReadError) as excinfo:
        await LocalFile(src=Path("link.txt")).apply(
            session,
            Path("/workspace/copied.txt"),
            tmp_path,
        )

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "link.txt"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_file_rejects_symlinked_source_before_checksum(tmp_path: Path) -> None:
    target_dir = tmp_path / "secret-dir"
    target_dir.mkdir()
    _symlink_or_skip(tmp_path / "link.txt", target_dir, target_is_directory=True)
    session = _RecordingSession()

    with pytest.raises(LocalFileReadError) as excinfo:
        await LocalFile(src=Path("link.txt")).apply(
            session,
            Path("/workspace/copied.txt"),
            tmp_path,
        )

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "link.txt"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_dir_copy_falls_back_when_safe_dir_fd_open_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    src_file = src_root / "safe.txt"
    src_file.write_text("safe", encoding="utf-8")
    session = _RecordingSession()
    local_dir = LocalDir(src=Path("src"))

    monkeypatch.setattr("agents.sandbox.entries.artifacts._OPEN_SUPPORTS_DIR_FD", False)
    monkeypatch.setattr("agents.sandbox.entries.artifacts._HAS_O_DIRECTORY", False)

    result = await local_dir._copy_local_dir_file(
        base_dir=tmp_path,
        session=session,
        src_root=src_root,
        src=src_file,
        dest_root=Path("/workspace/copied"),
    )

    assert result.path == Path("/workspace/copied/safe.txt")
    assert session.writes[Path("/workspace/copied/safe.txt")] == b"safe"


@pytest.mark.asyncio
async def test_local_dir_checksum_matches_written_bytes_when_source_changes(
    tmp_path: Path,
) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    src_file = src_root / "safe.txt"
    src_file.write_bytes(b"original")

    def mutate_source() -> None:
        src_file.write_bytes(b"mutated")

    session = _ChunkedMutatingWriteSession(mutate_source)
    local_dir = LocalDir(src=Path("src"))

    result = await local_dir._copy_local_dir_file(
        base_dir=tmp_path,
        session=session,
        src_root=src_root,
        src=src_file,
        dest_root=Path("/workspace/copied"),
    )

    written = session.writes[Path("/workspace/copied/safe.txt")]
    assert result.sha256 == hashlib.sha256(written).hexdigest()


@pytest.mark.asyncio
async def test_local_dir_does_not_remove_existing_destination_when_staged_write_fails(
    tmp_path: Path,
) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    src_file = src_root / "safe.txt"
    src_file.write_bytes(b"new content")
    dest = Path("/workspace/copied")
    child_dest = dest / "safe.txt"
    session = _StagedFailureAfterReadSession()
    session.writes[child_dest] = b"old content"

    with pytest.raises(WorkspaceArchiveWriteError):
        await LocalDir(src=Path("src")).apply(session, dest, tmp_path)

    assert session.writes[child_dest] == b"old content"
    assert session.removed == []
    assert session.staged_writes[Path("/workspace/copied/.safe.txt.staged")] == b"new content"


@pytest.mark.asyncio
async def test_local_file_preserves_local_read_error_when_write_wraps_stream_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = (tmp_path / "source.txt").resolve()
    source.write_bytes(b"original")
    session = _PayloadWrappingWriteSession()

    def failing_fdopen(
        fd: int,
        *args: object,
        **kwargs: object,
    ) -> io.IOBase:
        _ = args, kwargs
        return _FailAfterChunkStream(b"original", owned_fd=fd)

    monkeypatch.setattr("agents.sandbox.entries.artifacts.os.fdopen", failing_fdopen)

    with pytest.raises(LocalFileReadError) as excinfo:
        await LocalFile(src=Path("source.txt")).apply(
            session,
            Path("/workspace/copied.txt"),
            tmp_path,
        )

    assert excinfo.value.context["src"] == str(source)
    assert isinstance(excinfo.value.cause, OSError)


@pytest.mark.asyncio
async def test_local_dir_copy_preserves_local_read_error_when_write_wraps_stream_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    src_file = (src_root / "safe.txt").resolve()
    src_file.write_bytes(b"original")
    session = _PayloadWrappingWriteSession()
    local_dir = LocalDir(src=Path("src"))

    def failing_fdopen(fd: int, *args: object, **kwargs: object) -> io.IOBase:
        return _FailAfterChunkStream(b"original", owned_fd=fd)

    monkeypatch.setattr("agents.sandbox.entries.artifacts.os.fdopen", failing_fdopen)

    with pytest.raises(LocalFileReadError) as excinfo:
        await local_dir._copy_local_dir_file(
            base_dir=tmp_path,
            session=session,
            src_root=src_root,
            src=src_file,
            dest_root=Path("/workspace/copied"),
        )

    assert excinfo.value.context["src"] == str(src_file)
    assert isinstance(excinfo.value.cause, OSError)


@pytest.mark.asyncio
async def test_local_dir_copy_revalidates_swapped_paths_during_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not artifacts_module._OPEN_SUPPORTS_DIR_FD or not artifacts_module._HAS_O_DIRECTORY:
        pytest.skip("safe dir_fd open pinning is unavailable on this platform")

    src_root = tmp_path / "src"
    src_root.mkdir()
    src_file = src_root / "safe.txt"
    src_file.write_text("safe", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    session = _RecordingSession()
    local_dir = LocalDir(src=Path("src"))
    original_open = os.open
    swapped = False

    def swap_then_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "safe.txt" and not swapped:
            src_file.unlink()
            _symlink_or_skip(src_file, secret)
            swapped = True
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agents.sandbox.entries.artifacts.os.open", swap_then_open)

    with pytest.raises(LocalDirReadError) as excinfo:
        await local_dir._copy_local_dir_file(
            base_dir=tmp_path,
            session=session,
            src_root=src_root,
            src=src_file,
            dest_root=Path("/workspace/copied"),
        )

    assert excinfo.value.context["reason"] in {
        "symlink_not_supported",
        "path_changed_during_copy",
    }
    assert excinfo.value.context["child"] == "safe.txt"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_dir_copy_pins_parent_directories_during_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not artifacts_module._OPEN_SUPPORTS_DIR_FD or not artifacts_module._HAS_O_DIRECTORY:
        pytest.skip("safe dir_fd open pinning is unavailable on this platform")

    src_root = tmp_path / "src"
    src_root.mkdir()
    nested_dir = src_root / "nested"
    nested_dir.mkdir()
    src_file = nested_dir / "safe.txt"
    src_file.write_text("safe", encoding="utf-8")
    secret_dir = tmp_path / "secret-dir"
    secret_dir.mkdir()
    (secret_dir / "safe.txt").write_text("secret", encoding="utf-8")
    session = _RecordingSession()
    local_dir = LocalDir(src=Path("src"))
    original_open = os.open
    swapped = False

    def swap_parent_then_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "safe.txt" and not swapped:
            (src_root / "nested").rename(src_root / "nested-original")
            _symlink_or_skip(src_root / "nested", secret_dir, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agents.sandbox.entries.artifacts.os.open", swap_parent_then_open)

    result = await local_dir._copy_local_dir_file(
        base_dir=tmp_path,
        session=session,
        src_root=src_root,
        src=src_file,
        dest_root=Path("/workspace/copied"),
    )

    assert result.path == Path("/workspace/copied/nested/safe.txt")
    assert session.writes[Path("/workspace/copied/nested/safe.txt")] == b"safe"


@pytest.mark.asyncio
async def test_local_dir_apply_rejects_source_root_swapped_to_symlink_after_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not artifacts_module._OPEN_SUPPORTS_DIR_FD or not artifacts_module._HAS_O_DIRECTORY:
        pytest.skip("safe dir_fd open pinning is unavailable on this platform")

    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "safe.txt").write_text("safe", encoding="utf-8")
    secret_dir = tmp_path / "secret-dir"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("secret", encoding="utf-8")
    session = _RecordingSession()
    local_dir = LocalDir(src=Path("src"))
    original_open = os.open
    swapped = False

    def swap_root_then_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "src" and dir_fd is not None and not swapped:
            src_root.rename(tmp_path / "src-original")
            _symlink_or_skip(tmp_path / "src", secret_dir, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agents.sandbox.entries.artifacts.os.open", swap_root_then_open)

    with pytest.raises(LocalDirReadError) as excinfo:
        await local_dir.apply(session, Path("/workspace/copied"), tmp_path)

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "src"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_dir_apply_uses_configured_file_copy_fanout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "a.txt").write_text("a", encoding="utf-8")
    (src_root / "b.txt").write_text("b", encoding="utf-8")
    session = _RecordingSession()
    session._set_concurrency_limits(
        SandboxConcurrencyLimits(
            manifest_entries=4,
            local_dir_files=2,
        )
    )
    observed_limits: list[int | None] = []

    async def gather_with_limit_recording(
        task_factories: Sequence[Callable[[], Awaitable[MaterializedFile]]],
        *,
        max_concurrency: int | None = None,
    ) -> list[MaterializedFile]:
        observed_limits.append(max_concurrency)
        return [await factory() for factory in task_factories]

    monkeypatch.setattr(
        artifacts_module,
        "gather_in_order",
        gather_with_limit_recording,
    )

    result = await LocalDir(src=Path("src")).apply(
        session,
        Path("/workspace/copied"),
        tmp_path,
    )

    assert observed_limits == [2]
    assert sorted(file.path.as_posix() for file in result) == [
        "/workspace/copied/a.txt",
        "/workspace/copied/b.txt",
    ]
    assert session.writes == {
        Path("/workspace/copied/a.txt"): b"a",
        Path("/workspace/copied/b.txt"): b"b",
    }


@pytest.mark.asyncio
async def test_local_dir_rejects_symlinked_source_ancestors(tmp_path: Path) -> None:
    target_dir = tmp_path / "secret-dir"
    target_dir.mkdir()
    nested_dir = target_dir / "sub"
    nested_dir.mkdir()
    (nested_dir / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink_or_skip(tmp_path / "link", target_dir, target_is_directory=True)
    session = _RecordingSession()

    with pytest.raises(LocalDirReadError) as excinfo:
        await LocalDir(src=Path("link/sub")).apply(session, Path("/workspace/copied"), tmp_path)

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "link"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_dir_rejects_symlinked_source_root(tmp_path: Path) -> None:
    target_dir = tmp_path / "secret-dir"
    target_dir.mkdir()
    (target_dir / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink_or_skip(tmp_path / "src", target_dir, target_is_directory=True)
    session = _RecordingSession()

    with pytest.raises(LocalDirReadError) as excinfo:
        await LocalDir(src=Path("src")).apply(session, Path("/workspace/copied"), tmp_path)

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "src"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_dir_rejects_symlinked_files(tmp_path: Path) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "safe.txt").write_text("safe", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    _symlink_or_skip(src_root / "link.txt", secret)
    session = _RecordingSession()

    with pytest.raises(LocalDirReadError) as excinfo:
        await LocalDir(src=Path("src")).apply(session, Path("/workspace/copied"), tmp_path)

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "link.txt"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_local_dir_rejects_symlinked_directories(tmp_path: Path) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "safe.txt").write_text("safe", encoding="utf-8")
    target_dir = tmp_path / "secret-dir"
    target_dir.mkdir()
    (target_dir / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink_or_skip(src_root / "linked-dir", target_dir, target_is_directory=True)
    session = _RecordingSession()

    with pytest.raises(LocalDirReadError) as excinfo:
        await LocalDir(src=Path("src")).apply(session, Path("/workspace/copied"), tmp_path)

    assert excinfo.value.context["reason"] == "symlink_not_supported"
    assert excinfo.value.context["child"] == "linked-dir"
    assert session.writes == {}


@pytest.mark.asyncio
async def test_git_repo_uses_fetch_checkout_path_for_commit_refs() -> None:
    session = _GitRefSession()
    repo = GitRepo(repo="openai/example", ref="deadbeef")

    await repo.apply(session, Path("/workspace/repo"), Path("/ignored"))

    assert not any(call[:2] == ("git", "clone") for call in session.exec_calls)
    assert any(call[:2] == ("git", "init") for call in session.exec_calls)
    assert any(
        len(call) >= 7
        and call[:2] == ("git", "-C")
        and call[3:6] == ("remote", "add", "origin")
        and call[6] == "https://github.com/openai/example.git"
        for call in session.exec_calls
    )
    assert any(
        len(call) >= 9
        and call[:2] == ("git", "-C")
        and call[3:7] == ("fetch", "--depth", "1", "--no-tags")
        and call[-2:] == ("origin", "deadbeef")
        for call in session.exec_calls
    )
    assert any(
        len(call) >= 6
        and call[:2] == ("git", "-C")
        and call[3:5] == ("checkout", "--detach")
        and call[-1] == "FETCH_HEAD"
        for call in session.exec_calls
    )


@pytest.mark.asyncio
async def test_dir_metadata_strips_file_type_bits_before_chmod() -> None:
    session = _RecordingSession()
    dest = Path("/workspace/dir")

    await Dir()._apply_metadata(session, dest)

    assert ("chmod", "0755", str(dest)) in session.exec_calls


@pytest.mark.asyncio
async def test_apply_manifest_raises_on_chmod_failure() -> None:
    session = _MetadataFailureSession(
        Manifest(entries={"copied.txt": File(content=b"hello")}),
        fail_commands={"chmod"},
    )

    with pytest.raises(ExecNonZeroError):
        await session.apply_manifest()


@pytest.mark.asyncio
async def test_apply_manifest_raises_on_chgrp_failure() -> None:
    session = _MetadataFailureSession(
        Manifest(
            entries={
                "copied.txt": File(
                    content=b"hello",
                    group=User(name="sandbox-user"),
                )
            }
        ),
        fail_commands={"chgrp"},
    )

    with pytest.raises(ExecNonZeroError):
        await session.apply_manifest()

    assert ("chgrp", "sandbox-user", str(Path("/workspace/copied.txt"))) in session.exec_calls
    assert not any(call[0] == "chmod" for call in session.exec_calls)

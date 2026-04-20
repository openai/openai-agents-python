from __future__ import annotations

import io
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path, PureWindowsPath

import pytest

import agents.sandbox.entries.artifacts as artifacts_module
from agents.sandbox import SandboxConcurrencyLimits
from agents.sandbox.entries import Dir, File, GitRepo, LocalDir, LocalFile, resolve_workspace_path
from agents.sandbox.errors import ExecNonZeroError, InvalidManifestPathError, LocalDirReadError
from agents.sandbox.manifest import Manifest
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
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


def test_resolve_workspace_path_rejects_windows_drive_absolute_path() -> None:
    with pytest.raises(InvalidManifestPathError) as exc_info:
        resolve_workspace_path(
            Path("/workspace"),
            PureWindowsPath("C:/tmp/secret.txt"),
            allow_absolute_within_root=True,
        )

    assert str(exc_info.value) == "manifest path must be relative: C:/tmp/secret.txt"
    assert exc_info.value.context == {"rel": "C:/tmp/secret.txt", "reason": "absolute"}


def test_resolve_workspace_path_rejects_absolute_escape_after_normalization() -> None:
    with pytest.raises(InvalidManifestPathError) as exc_info:
        resolve_workspace_path(
            Path("/workspace"),
            "/workspace/../etc/passwd",
            allow_absolute_within_root=True,
        )

    assert str(exc_info.value) == "manifest path must be relative: /etc/passwd"
    assert exc_info.value.context == {"rel": "/etc/passwd", "reason": "absolute"}


def test_resolve_workspace_path_rejects_absolute_symlink_escape_for_host_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    escaped = link / "secret.txt"

    with pytest.raises(InvalidManifestPathError) as exc_info:
        resolve_workspace_path(
            root,
            escaped,
            allow_absolute_within_root=True,
        )

    assert str(exc_info.value) == f"manifest path must be relative: {escaped.as_posix()}"
    assert exc_info.value.context == {"rel": escaped.as_posix(), "reason": "absolute"}


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
    assert session.writes[Path("/workspace/copied.txt")] == b"hello"


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
async def test_local_dir_copy_revalidates_swapped_paths_during_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
        if (path == "safe.txt" or Path(path) == src_file) and not swapped:
            src_file.unlink()
            src_file.symlink_to(secret)
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
            (src_root / "nested").symlink_to(secret_dir, target_is_directory=True)
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
        if (path == "src" or Path(path) in {src_root, src_root / "safe.txt"}) and not swapped:
            src_root.rename(tmp_path / "src-original")
            (tmp_path / "src").symlink_to(secret_dir, target_is_directory=True)
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
async def test_local_dir_apply_fallback_rejects_source_root_swapped_to_symlink_after_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "safe.txt").write_text("safe", encoding="utf-8")
    secret_dir = tmp_path / "secret-dir"
    secret_dir.mkdir()
    session = _RecordingSession()
    local_dir = LocalDir(src=Path("src"))
    original_open = os.open
    swapped = False

    monkeypatch.setattr("agents.sandbox.entries.artifacts._OPEN_SUPPORTS_DIR_FD", False)
    monkeypatch.setattr("agents.sandbox.entries.artifacts._HAS_O_DIRECTORY", False)

    def swap_root_then_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if Path(path) == src_root / "safe.txt" and not swapped:
            src_root.rename(tmp_path / "src-original")
            (tmp_path / "src").symlink_to(secret_dir, target_is_directory=True)
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
    (tmp_path / "link").symlink_to(target_dir, target_is_directory=True)
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
    (tmp_path / "src").symlink_to(target_dir, target_is_directory=True)
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
    (src_root / "link.txt").symlink_to(secret)
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
    (src_root / "linked-dir").symlink_to(target_dir, target_is_directory=True)
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

    await Dir()._apply_metadata(session, Path("/workspace/dir"))

    assert ("chmod", "0755", "/workspace/dir") in session.exec_calls


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

    assert ("chgrp", "sandbox-user", "/workspace/copied.txt") in session.exec_calls
    assert not any(call[0] == "chmod" for call in session.exec_calls)

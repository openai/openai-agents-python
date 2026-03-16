from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Literal

import pytest

import agents.sandbox.entries.codex as codex_module
from agents.sandbox.entries import Codex, Dir, File, GitRepo, LocalFile
from agents.sandbox.errors import ExecNonZeroError
from agents.sandbox.manifest import Manifest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExecResult, User


class _RecordingSession(BaseSandboxSession):
    def __init__(self, manifest: Manifest | None = None) -> None:
        self.state = SandboxSessionState(
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

    async def read(self, path: Path) -> io.IOBase:
        return io.BytesIO(self.writes[path])

    async def write(self, path: Path, data: io.IOBase) -> None:
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


class _CodexSession(_RecordingSession):
    def __init__(self, asset_name: str, *, resolved_binary_path: str) -> None:
        super().__init__()
        self.asset_name = asset_name
        self.resolved_binary_path = resolved_binary_path

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = tuple(str(part) for part in command)
        self.exec_calls.append(cmd)
        if cmd[:2] == ("sh", "-lc") and "find " in cmd[2] and "head -n 1" in cmd[2]:
            return ExecResult(
                stdout=f"{self.resolved_binary_path}\n".encode(),
                stderr=b"",
                exit_code=0,
            )
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def resolve_codex_github_asset_name(self) -> str:
        return self.asset_name


class _RestorableSnapshot(SnapshotBase):
    __test__ = False
    type: Literal["entry-restorable"] = "entry-restorable"

    async def persist(self, data: io.IOBase) -> None:
        _ = data

    async def restore(self) -> io.IOBase:
        return io.BytesIO(b"snapshot")

    async def restorable(self) -> bool:
        return True


class _ResumeCodexSession(_CodexSession):
    def __init__(self, asset_name: str, *, resolved_binary_path: str, codex_path: Path) -> None:
        super().__init__(asset_name, resolved_binary_path=resolved_binary_path)
        self.state.snapshot = _RestorableSnapshot(id="resume")
        self.codex_path = codex_path
        self.hydrated = False
        self.existing_paths: set[str] = set()

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = tuple(str(part) for part in command)
        self.exec_calls.append(cmd)
        if cmd[:2] == ("test", "-e"):
            return ExecResult(
                stdout=b"",
                stderr=b"",
                exit_code=0 if cmd[2] in self.existing_paths else 1,
            )
        if cmd[:2] == ("sh", "-lc") and "find " in cmd[2] and "head -n 1" in cmd[2]:
            return ExecResult(
                stdout=f"{self.resolved_binary_path}\n".encode(),
                stderr=b"",
                exit_code=0,
            )
        if cmd[:1] == ("cp",):
            self.existing_paths.add(cmd[2])
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        self.hydrated = True


def _tar_gz_bytes(*, members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


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


@pytest.mark.asyncio
async def test_codex_artifact_downloads_resolved_release_asset_inside_unix_box() -> None:
    session = _CodexSession(
        "codex-x86_64-unknown-linux-gnu.tar.gz",
        resolved_binary_path="/workspace/.codex_bin/.codex-install-123/codex",
    )
    entry = Codex(version="v1.2.3")
    archive_bytes = _tar_gz_bytes(members={"codex": b"#!/bin/sh\n"})

    class _FakeResponse:
        headers = {"Content-Length": str(len(archive_bytes))}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield archive_bytes[:5]
            yield archive_bytes[5:]

    class _FakeStreamContext:
        def __enter__(self) -> _FakeResponse:
            return _FakeResponse()

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def _fake_stream(url: str) -> _FakeStreamContext:
        _ = url
        return _FakeStreamContext()

    original_stream = codex_module._stream_release_asset
    codex_module._stream_release_asset = _fake_stream
    try:
        result = await entry.apply(session, Path("/workspace/.codex_bin/codex"), Path("/ignored"))
    finally:
        codex_module._stream_release_asset = original_stream

    assert result == []
    assert any(path.name == "codex-x86_64-unknown-linux-gnu.tar.gz" for path in session.writes)
    assert Path("/workspace/.codex_bin/codex") not in session.writes
    assert any(
        call[:2] == ("tar", "-xzf")
        and call[2].endswith("/codex-x86_64-unknown-linux-gnu.tar.gz")
        and call[3:5] == ("-C", call[4])
        and "/.codex-install-" in call[2]
        for call in session.exec_calls
    )
    assert (
        "cp",
        "/workspace/.codex_bin/.codex-install-123/codex",
        "/workspace/.codex_bin/codex",
    ) in session.exec_calls
    assert ("chmod", "0755", "/workspace/.codex_bin/codex") in session.exec_calls


@pytest.mark.asyncio
async def test_codex_artifact_rejects_windows_release_assets() -> None:
    session = _CodexSession(
        "codex-x86_64-pc-windows-msvc.exe.tar.gz",
        resolved_binary_path="/workspace/.codex_bin/.codex-install-456/codex.exe",
    )
    entry = Codex()

    with pytest.raises(RuntimeError, match="Windows Codex artifacts are not supported"):
        await entry.apply(session, Path("/workspace/.codex_bin/codex.exe"), Path("/ignored"))


@pytest.mark.asyncio
async def test_base_session_reapplies_missing_codex_entry_after_snapshot_restore() -> None:
    codex_path = Path("/workspace/.codex_bin/codex")
    session = _ResumeCodexSession(
        "codex-x86_64-unknown-linux-gnu.tar.gz",
        resolved_binary_path="/workspace/.codex_bin/.codex-install-789/codex",
        codex_path=codex_path,
    )
    session.state.manifest = Manifest(entries={".codex_bin/codex": Codex(version="v1.2.3")})
    archive_bytes = _tar_gz_bytes(members={"codex": b"#!/bin/sh\n"})

    class _FakeResponse:
        headers = {"Content-Length": str(len(archive_bytes))}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield archive_bytes

    class _FakeStreamContext:
        def __enter__(self) -> _FakeResponse:
            return _FakeResponse()

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def _fake_stream(url: str) -> _FakeStreamContext:
        _ = url
        return _FakeStreamContext()

    original_stream = codex_module._stream_release_asset
    codex_module._stream_release_asset = _fake_stream
    try:
        await session.start()
    finally:
        codex_module._stream_release_asset = original_stream

    assert session.hydrated is True
    assert ("test", "-e", str(codex_path)) in session.exec_calls
    assert (
        "cp",
        "/workspace/.codex_bin/.codex-install-789/codex",
        str(codex_path),
    ) in session.exec_calls
    assert str(codex_path) in session.existing_paths

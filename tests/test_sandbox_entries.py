from __future__ import annotations

import io
from pathlib import Path

import pytest

from agents.sandbox.entries import Dir, File, GitRepo, LocalFile
from agents.sandbox.errors import ExecNonZeroError
from agents.sandbox.manifest import Manifest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot
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


@pytest.mark.asyncio
async def test_base_sandbox_session_uses_current_working_directory_for_local_file_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = _RecordingSession(
        Manifest(entries={"copied.txt": LocalFile(src=Path("source.txt"))}),
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

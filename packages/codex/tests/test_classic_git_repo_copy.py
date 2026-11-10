"""Git directory selection preserves ordinary sandbox copy behavior."""

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents.classic.sandbox.entries import GitRepo
from agents.classic.sandbox.entries.artifacts import _GIT_COPY_LAUNCHER
from agents.classic.sandbox.errors import GitCopyError
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.classic.sandbox.types import ExecResult

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell and filesystem")


class FixtureGitSession:
    """Replace network cloning with a benign directory fixture."""

    def __init__(self, fixture: Path, cwd: Path) -> None:
        self.fixture = fixture
        self.cwd = cwd
        self.state = SimpleNamespace(session_id=uuid.uuid4())
        self.checkout: Path | None = None

    async def exec(self, *args: str, shell: bool = True) -> ExecResult:
        if args[:2] == ("git", "clone"):
            self.checkout = Path(args[-1])
            shutil.copytree(self.fixture, self.checkout, symlinks=True)
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if shell:
            process = await asyncio.create_subprocess_shell(
                args[0],
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, stderr = await process.communicate()
        assert process.returncode is not None
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=process.returncode)

    async def mkdir(self, path: Path, *, parents: bool) -> None:
        (self.cwd / path).mkdir(parents=parents, exist_ok=True)

    async def _exec_checked_nonzero(self, *args: str) -> ExecResult:
        result = await self.exec(*args, shell=False)
        assert result.ok(), result.stderr
        return result


@pytest.fixture
def git_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "fixture"
    nested = fixture / "directory with spaces" / "nested"
    nested.mkdir(parents=True)
    (fixture / "root.txt").write_text("root fixture\n")
    (nested / "selected.txt").write_text("selected fixture\n")
    (fixture / "selected-link").symlink_to("directory with spaces/nested", target_is_directory=True)
    return fixture


@pytest.mark.parametrize("subpath", [None, ".", "directory with spaces/nested", "selected-link"])
@pytest.mark.parametrize("relative_destination", [False, True])
@pytest.mark.parametrize("cwd_name", ["working directory", "working directory\n"])
async def test_git_repo_copies_selected_directory(
    tmp_path: Path,
    git_fixture: Path,
    subpath: str | None,
    relative_destination: bool,
    cwd_name: str,
) -> None:
    cwd = tmp_path / cwd_name
    cwd.mkdir()
    destination = Path("output directory") if relative_destination else cwd / "output directory"
    session = FixtureGitSession(git_fixture, cwd)
    entry = GitRepo(repo="fixture/repository", ref="main", subpath=subpath)

    assert await entry.apply(cast(BaseSandboxSession, session), destination, cwd) == []

    output = cwd / destination
    if subpath in (None, "."):
        assert (output / "root.txt").read_text() == "root fixture\n"
        assert (
            output / "directory with spaces/nested/selected.txt"
        ).read_text() == "selected fixture\n"
    else:
        assert (output / "selected.txt").read_text() == "selected fixture\n"
        assert not (output / "root.txt").exists()
    assert session.checkout is not None
    assert not session.checkout.exists()


async def test_git_repo_uses_system_copy_with_a_limited_path(
    tmp_path: Path, git_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_bin = tmp_path / "tools"
    tool_bin.mkdir()
    for utility in ("sh", "git", "rm", "chmod"):
        executable = shutil.which(utility)
        assert executable is not None
        (tool_bin / utility).symlink_to(executable)
    monkeypatch.setenv("PATH", f"relative-tools{os.pathsep}{tool_bin}")

    session = FixtureGitSession(git_fixture, tmp_path)
    entry = GitRepo(repo="fixture/repository", ref="main")
    destination = tmp_path / "output"

    assert await entry.apply(cast(BaseSandboxSession, session), destination, tmp_path) == []
    assert (destination / "root.txt").read_text() == "root fixture\n"


def test_git_copy_launcher_clears_the_inherited_environment(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "sh",
            "-c",
            _GIT_COPY_LAUNCHER,
            "git-copy-test-launcher",
            'printf "%s\\n" "${SDK_GIT_COPY_TEST_CONTEXT-unset}" "$1"',
            "git-copy-test",
            "ordinary argument",
        ],
        cwd=tmp_path,
        env={**os.environ, "SDK_GIT_COPY_TEST_CONTEXT": "fixture"},
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout == "unset\nordinary argument\n"


@pytest.mark.parametrize("subpath", ["missing-directory", "root.txt"])
async def test_git_repo_reports_unavailable_directory_as_copy_error(
    tmp_path: Path, git_fixture: Path, subpath: str
) -> None:
    session = FixtureGitSession(git_fixture, tmp_path)
    entry = GitRepo(repo="fixture/repository", ref="main", subpath=subpath)
    destination = tmp_path / "output"

    with pytest.raises(GitCopyError):
        await entry.apply(cast(BaseSandboxSession, session), destination, tmp_path)

    assert list(destination.iterdir()) == []
    assert session.checkout is not None
    assert not session.checkout.exists()

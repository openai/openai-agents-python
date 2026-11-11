"""Git directory selection preserves ordinary sandbox copy behavior."""

import asyncio
import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import cast

import pytest

from agents.classic.sandbox.entries import GitRepo
from agents.classic.sandbox.entries.artifacts import _GIT_COPY_SOURCE_BOUNDARY
from agents.classic.sandbox.errors import GitCopyError
from agents.classic.sandbox.manifest import Environment, Manifest
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.classic.sandbox.session.dependencies import Dependencies
from agents.classic.sandbox.types import ExecResult
from agents.classic.sandbox.workspace_paths import _resolve_git_copy_path

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell and filesystem")


class FixtureGitSession:
    """Replace network cloning with a benign directory fixture."""

    _dependencies: Dependencies | None = None

    def __init__(self, fixture: Path, cwd: Path, *, copy_path: str = os.defpath) -> None:
        self.fixture = fixture
        self.cwd = cwd
        self.copy_path = copy_path
        self.state = SimpleNamespace(session_id=uuid.uuid4())
        self.checkout: Path | None = None

    async def exec(self, *args: str, shell: bool | list[str] = True) -> ExecResult:
        if args[:2] == ("git", "clone"):
            self.checkout = Path(args[-1])
            shutil.copytree(self.fixture, self.checkout, symlinks=True)
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if shell:
            prefix = shell if isinstance(shell, list) else ["sh", "-c"]
            args = (*prefix, args[0])
        return await self._run(*args)

    async def _exec_git_copy(self, script: str, *arguments: str) -> ExecResult:
        return await self._run(
            "sh", "-c", script, "sandbox-git-copy", *arguments, env={"PATH": self.copy_path}
        )

    async def _run(self, *args: str, env: dict[str, str] | None = None) -> ExecResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            env=env,
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


@pytest.mark.parametrize(
    ("source", "expected_status"),
    [
        ("/example/checkout", 0),
        ("/example/checkout/nested", 0),
        ("/example/neighbor", 1),
        ("/example/checkout-copy", 1),
    ],
)
def test_git_copy_requires_a_contained_source(source: str, expected_status: int) -> None:
    result = subprocess.run(
        [
            "sh",
            "-c",
            "repo_root=$1\nsource_root=$2\n" + _GIT_COPY_SOURCE_BOUNDARY,
            "git-copy-boundary",
            "/example/checkout",
            source,
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_status


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
        link = output / "selected-link"
        assert link.is_symlink()
        assert link.readlink() == Path("directory with spaces/nested")
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


@pytest.mark.parametrize("copy_path", ["provider tools", "", "missing:"])
async def test_git_repo_anchors_relative_copy_tools(
    tmp_path: Path, git_fixture: Path, copy_path: str
) -> None:
    tools = tmp_path / "provider tools" if copy_path == "provider tools" else tmp_path
    tools.mkdir(exist_ok=True)
    shell = shutil.which("sh")
    system_copy = shutil.which("cp")
    assert shell is not None and system_copy is not None
    (tools / "sh").symlink_to(shell)
    marker = tmp_path / "selected copy"
    copy = tools / "cp"
    copy.write_text(
        f"#!/bin/sh\nprintf '%s' provider > {shlex.quote(str(marker))}\n"
        f'exec {shlex.quote(system_copy)} "$@"\n',
        encoding="utf-8",
    )
    copy.chmod(0o755)

    session = FixtureGitSession(git_fixture, tmp_path, copy_path=copy_path)
    entry = GitRepo(repo="fixture/repository", ref="main")
    destination = tmp_path / "output"

    assert await entry.apply(cast(BaseSandboxSession, session), destination, tmp_path) == []
    assert (destination / "root.txt").read_text() == "root fixture\n"
    assert marker.read_text(encoding="utf-8") == "provider"


async def test_unix_local_git_copy_uses_application_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    system_copy = shutil.which("cp")
    assert system_copy is not None
    tool_bin = tmp_path / "custom tools"
    tool_bin.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source"
    source.write_text("fixture", encoding="utf-8")
    copy_marker = workspace / "copy marker"
    copy_helper = tool_bin / "sdk-test-record-copy"
    copy_helper.write_text(
        f"#!/bin/sh\nprintf '%s' 'custom copy' > {shlex.quote(str(copy_marker))}\n",
        encoding="utf-8",
    )
    copy_helper.chmod(0o755)
    custom_copy = tool_bin / "cp"
    custom_copy.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'test "${SDK_GIT_COPY_TEST_CONTEXT-unset}" = unset\n'
        "sdk-test-record-copy\n"
        f'exec {shlex.quote(system_copy)} "$@"\n',
        encoding="utf-8",
    )
    custom_copy.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", tool_bin.name, prepend=os.pathsep)
    session = await UnixLocalSandboxClient().create(
        manifest=Manifest(
            root=str(workspace),
            environment=Environment(
                value={"PATH": "/usr/bin:/bin", "SDK_GIT_COPY_TEST_CONTEXT": "fixture"}
            ),
        ),
    )

    destination = workspace / "output"
    try:
        copy = await session._exec_git_copy('exec cp -- "$1" "$2"', str(source), str(destination))
        assert copy.ok(), copy.stderr
    finally:
        await session.aclose()

    assert destination.read_text(encoding="utf-8") == "fixture"
    assert copy_marker.read_text(encoding="utf-8") == "custom copy"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/image/tools/sh", "/image/tools/multicall"),
        ("/image/relative-tools/cp", "/toolbox/cp"),
        ("/image/workspace-tools", None),
        ("/image/workspace-alias", None),
        ("/image/tools/cp", None),
        ("/image/loop", None),
    ],
)
def test_git_copy_tool_paths_stay_outside_workspace(path: str, expected: str | None) -> None:
    links = {
        "/image/tools/sh": "multicall",
        "/image/relative-tools": "../toolbox",
        "/image/workspace-tools": "/workspace/tools",
        "/image/workspace-alias": "/workspace/tool-alias",
        "/workspace/tool-alias": "/toolbox",
        "/image/tools/cp": "/workspace/cp",
        "/image/loop": "loop",
    }
    result = _resolve_git_copy_path(
        PurePosixPath(path),
        lambda part: links.get(str(part), ""),
        workspace=PurePosixPath("/workspace"),
    )
    assert result == (PurePosixPath(expected) if expected is not None else None)


async def test_default_git_copy_uses_a_clean_child_environment(
    tmp_path: Path, git_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDK_COPY_TEST_VALUE", "workspace-only")
    session = cast(BaseSandboxSession, FixtureGitSession(git_fixture, tmp_path))
    result = await BaseSandboxSession._exec_git_copy(
        session,
        'test -z "${SDK_COPY_TEST_VALUE+x}" && printf "%s" "$1"',
        "selected directory",
    )
    assert result.ok()
    assert result.stdout == b"selected directory"


def test_docker_git_copy_clears_image_and_manifest_environment() -> None:
    pytest.importorskip("docker")
    from agents.classic.sandbox.sandboxes.docker import DockerSandboxSession

    image = SimpleNamespace(attrs={"Config": {"Env": ["PATH=/tools", "SDK_IMAGE_TEST=value"]}})
    container = SimpleNamespace(
        image=image,
        attrs={
            "Config": {
                "Env": ["PATH=/workspace/tools", "SDK_IMAGE_TEST=value", "SDK_MANIFEST_TEST=value"]
            }
        },
    )
    session = cast(
        DockerSandboxSession,
        SimpleNamespace(
            _container=container,
            _git_copy_link_target=lambda path: "",
            state=SimpleNamespace(manifest=Manifest()),
        ),
    )
    assert DockerSandboxSession._git_copy_environment(session) == [
        "PATH=/tools:/usr/bin:/bin",
        "SDK_IMAGE_TEST",
        "SDK_MANIFEST_TEST",
    ]


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

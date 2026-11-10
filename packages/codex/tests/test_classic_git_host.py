"""Git repository hosts retain authority-only HTTPS configuration."""

import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from agents.classic.sandbox.entries import Dir, GitRepo
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.classic.sandbox.session.manifest_application import ManifestApplier
from agents.classic.sandbox.types import ExecResult, User


def recording_session() -> SimpleNamespace:
    # Keep this boundary local: observe commands without invoking Git or a provider.
    return SimpleNamespace(
        state=SimpleNamespace(session_id=UUID(int=0)),
        exec=AsyncMock(return_value=ExecResult(stdout=b"", stderr=b"", exit_code=0)),
        mkdir=AsyncMock(),
        _exec_checked_nonzero=AsyncMock(),
    )


@pytest.mark.parametrize(
    "host",
    [
        "github.com",
        "git.example.test:8443",
        "xn--bcher-kva.example.test",
        "bücher.example.test",
        "[::1]",
        "[::1]:8443",
    ],
)
@pytest.mark.parametrize("ref", ["release/stable", "0123456789abcdef"])
async def test_git_host_preserves_https_authority_and_refs(host: str, ref: str) -> None:
    session = recording_session()
    entry = GitRepo(host=host, repo="group/project", ref=ref)

    assert await entry.apply(cast(BaseSandboxSession, session), Path("/dest"), Path("/base")) == []

    commands = [call.args for call in session.exec.await_args_list]
    expected_url = f"https://{host}/group/project.git"
    if ref == "release/stable":
        clone = next(command for command in commands if command[:2] == ("git", "clone"))
        assert clone[6:8] == (ref, expected_url)
    else:
        remote = next(
            command for command in commands if command[3:6] == ("remote", "add", "origin")
        )
        assert remote[6] == expected_url
        fetch = next(command for command in commands if command[3:4] == ("fetch",))
        assert fetch[-1] == ref
    assert entry.host == host
    assert all(call.kwargs == {"shell": False} for call in session.exec.await_args_list[1:])
    session.mkdir.assert_awaited_once_with(Path("/dest"), parents=True)


async def test_git_clone_uses_validated_host_across_await() -> None:
    session = recording_session()
    entry = GitRepo(host="original.example.test", repo="group/project", ref="stable")

    async def change_host(*_args: str, **_kwargs: object) -> ExecResult:
        entry.host = "updated.example.test"
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    session.exec.side_effect = change_host
    await entry.apply(cast(BaseSandboxSession, session), Path("/dest"), Path("/base"))

    clone = next(
        call.args for call in session.exec.await_args_list if call.args[:2] == ("git", "clone")
    )
    assert clone[7] == "https://original.example.test/group/project.git"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "account@example.test",
        "account:synthetic-password@example.test",
        "example.test/path",
        "https://example.test",
        "example.test?query",
        "example.test#fragment",
        " example.test",
        "example.test\n",
        "example.test\\path",
        "example%2etest",
        "example.test:invalid",
        "example.test:",
        "example.test:65536",
        "[::1]:",
        "[not-an-address]",
        "[1234]",
        "[::1]suffix",
        "example{test}",
        "example|test",
        'example"test',
        "example<test>",
        "example`test",
    ],
)
async def test_invalid_git_host_rejects_before_session_work(host: str) -> None:
    session = recording_session()
    entry = GitRepo(host=host, repo="group/project", ref="release/stable")

    with pytest.raises(ValueError) as caught:
        await entry.apply(cast(BaseSandboxSession, session), Path("/dest"), Path("/base"))

    assert caught.value.args == ("GitRepo host must be an HTTPS authority without userinfo",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    if host:
        diagnostic = traceback.TracebackException.from_exception(caught.value, capture_locals=True)
        frame_locals = diagnostic.stack[-1].locals
        assert frame_locals is not None
        assert all(host not in value for value in frame_locals.values())
        assert host not in repr(entry)
    session.exec.assert_not_awaited()
    session.mkdir.assert_not_awaited()
    session._exec_checked_nonzero.assert_not_awaited()


async def test_create_rejects_invalid_nested_git_host(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = Manifest(
        root=str(workspace),
        entries={
            "nested": Dir(
                children={
                    "repo": GitRepo(host="https://example.test", repo="group/project", ref="stable")
                }
            )
        },
    )

    with pytest.raises(ValueError, match="GitRepo host must be an HTTPS authority"):
        await UnixLocalSandboxClient().create(manifest=manifest)

    assert not workspace.exists()


async def test_start_and_resume_reject_invalid_git_host_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = UnixLocalSandboxClient()
    manifest = Manifest(
        root=str(tmp_path / "workspace"),
        entries={"repo": GitRepo(host="example.test", repo="group/project", ref="stable")},
    )
    session = await client.create(manifest=manifest)
    repo = session.state.manifest.entries["repo"]
    assert isinstance(repo, GitRepo)
    repo.host = "https://example.test"
    start_backend = AsyncMock()
    rollback = AsyncMock()
    monkeypatch.setattr(session._inner, "_ensure_backend_started", start_backend)
    monkeypatch.setattr(session._inner, "_after_start_failed", rollback)

    with pytest.raises(ValueError, match="GitRepo host must be an HTTPS authority"):
        await session.start()
    with pytest.raises(ValueError, match="GitRepo host must be an HTTPS authority"):
        await client.resume(session.state)

    start_backend.assert_not_awaited()
    rollback.assert_not_awaited()


async def test_manifest_rejects_invalid_nested_git_host_before_materialization() -> None:
    mkdir = AsyncMock()
    exec_checked = AsyncMock()
    apply_entry = AsyncMock()
    applier = ManifestApplier(
        mkdir=mkdir, exec_checked_nonzero=exec_checked, apply_entry=apply_entry
    )
    manifest = Manifest(
        entries={
            "first": Dir(),
            "nested": Dir(
                children={
                    "repo": GitRepo(host="https://example.test", repo="group/project", ref="stable")
                }
            ),
        },
        users=[User(name="example")],
    )

    with pytest.raises(ValueError, match="GitRepo host must be an HTTPS authority"):
        await applier.apply_manifest(manifest)
    with pytest.raises(ValueError, match="GitRepo host must be an HTTPS authority"):
        await applier._apply_entry_batch(
            [(Path("/workspace") / name, entry) for name, entry in manifest.entries.items()],
            base_dir=Path("/"),
        )

    mkdir.assert_not_awaited()
    exec_checked.assert_not_awaited()
    apply_entry.assert_not_awaited()


async def test_direct_dir_rejects_invalid_git_host_before_materialization() -> None:
    session = recording_session()
    directory = Dir(
        children={"repo": GitRepo(host="https://example.test", repo="group/project", ref="stable")}
    )

    with pytest.raises(ValueError, match="GitRepo host must be an HTTPS authority"):
        await directory.apply(cast(BaseSandboxSession, session), Path("/dest"), Path("/"))

    session.mkdir.assert_not_awaited()

"""Git repository hosts retain authority-only HTTPS configuration."""

import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from agents.classic.sandbox.entries import GitRepo
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.classic.sandbox.types import ExecResult


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

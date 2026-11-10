"""Public local read grants with real commands and independent host file oracles."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import pytest

from agents import Agent, CodexLogin, Environment, LocalStorage, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)

native_sandbox = (
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)


def _respond_command(responses: ResponsesFixture, command: str, call_id: str) -> None:
    responses.respond(function_call("exec_command", {"cmd": command, "login": False}, call_id))
    responses.respond(assistant_output("done"))


def _agent(responses: ResponsesFixture) -> Agent:
    return Agent(
        instructions="Use the selected command.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )


@native_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="local read grants require macOS")
@pytest.mark.parametrize("read_only", [False, True])
async def test_local_read_grant_reads_named_skill_without_expanding_authority(
    responses: ResponsesFixture, tmp_path: Path, read_only: bool
) -> None:
    tmp_path = tmp_path.resolve()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    skill = shared / "SKILL.md"
    skill_contents = (
        "---\nname: named\ndescription: A synthetic skill.\n---\nSCOPED_SKILL_CONTENT\n"
    )
    skill.write_text(skill_contents)
    outside = tmp_path / "ungranted"
    outside.write_text("UNGRANTED_PRIVATE_CONTENT\n")
    skills = workspace / ".agents" / "skills"
    skills.mkdir(parents=True)
    (skills / "named").symlink_to(shared, target_is_directory=True)
    denied_skill = tmp_path / "denied-skill"
    denied_skill.mkdir()
    (denied_skill / "SKILL.md").write_text(
        "---\nname: denied\ndescription: A synthetic skill.\n---\nDENIED_SKILL_CONTENT\n"
    )
    (skills / "denied").symlink_to(denied_skill, target_is_directory=True)
    (workspace / "escape").symlink_to(outside)
    (shared / "escape").symlink_to(outside)
    # macOS Seatbelt defaults otherwise expose /private/tmp independently of
    # pytest's per-user temporary directory. Exercise both locations.
    with TemporaryDirectory(dir="/tmp", prefix="agents-ungranted-") as directory:
        private = Path(directory).resolve() / "private"
        private.write_text("PRIVATE_TMP_SENTINEL\n")
        workspace_write = (
            "if (printf unexpected > result) 2>/dev/null; then exit 17; fi"
            if read_only
            else "printf WORKSPACE_WRITE > result"
        )
        command = (
            "set -e; "
            f"cat {shlex.quote(str(skill))}; "
            "cat .agents/skills/named/SKILL.md; "
            f"if cat {shlex.quote(str(outside))} >/dev/null 2>&1; then exit 11; fi; "
            "if cat escape >/dev/null 2>&1; then exit 12; fi; "
            "if cat .agents/skills/named/escape >/dev/null 2>&1; then exit 13; fi; "
            f"if cat {shlex.quote(str(private))} >/dev/null 2>&1; then exit 14; fi; "
            f"if (printf changed >> {shlex.quote(str(skill))}) 2>/dev/null; then exit 15; fi; "
            "if (printf changed > .agents/skills/named/new) 2>/dev/null; then exit 16; fi; "
            f"{workspace_write}; printf ALL_BOUNDARIES_PASSED"
        )
        _respond_command(responses, command, "scope")
        async with (
            timeout(30),
            _agent(responses).session(
                environment=Environment.local(
                    cwd=workspace,
                    tools=["exec_command"],
                    read_only_roots=[shared],
                    read_only=read_only,
                    capability_directories=[str(skills.parent)],
                )
            ) as session,
        ):
            assert (
                await (await session.submit("Use $named and $denied if available.")).get_result()
            ).final_output == "done"
        first_request = json.dumps(responses.requests[0])
        assert "SCOPED_SKILL_CONTENT" in first_request
        assert "DENIED_SKILL_CONTENT" not in first_request
        actual = tool_output(responses.requests[-1], "scope")
        assert "Process exited with code 0" in actual
        assert actual.count("SCOPED_SKILL_CONTENT") == 2
        assert "ALL_BOUNDARIES_PASSED" in actual
        assert "UNGRANTED_PRIVATE_CONTENT" not in actual
        assert "PRIVATE_TMP_SENTINEL" not in actual
        assert private.read_text() == "PRIVATE_TMP_SENTINEL\n"
    assert skill.read_text() == skill_contents
    assert outside.read_text() == "UNGRANTED_PRIVATE_CONTENT\n"
    assert not (shared / "new").exists()
    if read_only:
        assert not (workspace / "result").exists()
    else:
        assert (workspace / "result").read_text() == "WORKSPACE_WRITE"


@native_sandbox
@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize("explicit_empty", [False, True])
async def test_local_default_read_boundary_has_no_extra_roots(
    responses: ResponsesFixture, tmp_path: Path, explicit_empty: bool
) -> None:
    tmp_path = tmp_path.resolve()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("DEFAULT_PRIVATE_CONTENT\n")
    (workspace / "escape").symlink_to(outside)
    command = (
        "set -e; "
        f"if cat {shlex.quote(str(outside))} >/dev/null 2>&1; then exit 11; fi; "
        "if cat escape >/dev/null 2>&1; then exit 12; fi; "
        "printf WORKSPACE_WRITE > result; printf DEFAULT_BOUNDARY_PASSED"
    )
    _respond_command(responses, command, "default")
    environment = (
        Environment.local(cwd=workspace, tools=["exec_command"], read_only_roots=[])
        if explicit_empty
        else Environment.local(cwd=workspace, tools=["exec_command"])
    )
    async with timeout(30), _agent(responses).session(environment=environment) as session:
        assert (
            await (await session.submit("Check the default boundary.")).get_result()
        ).final_output == "done"
    actual = tool_output(responses.requests[-1], "default")
    assert "Process exited with code 0" in actual
    assert "DEFAULT_BOUNDARY_PASSED" in actual
    assert "DEFAULT_PRIVATE_CONTENT" not in actual
    assert outside.read_text() == "DEFAULT_PRIVATE_CONTENT\n"
    assert (workspace / "result").read_text() == "WORKSPACE_WRITE"


@native_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="local read grants require macOS")
async def test_local_read_grant_restores_with_the_current_environment(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    tmp_path = tmp_path.resolve()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    source = shared / "source"
    source.write_text("RESTORED_READ_CONTENT\n")
    outside = tmp_path / "outside"
    outside.write_text("RESTORED_PRIVATE_CONTENT\n")
    command = (
        "set -e; "
        f"cat {shlex.quote(str(source))}; "
        f"cat {shlex.quote(str(tmp_path / 'second' / 'source'))}; "
        f"if cat {shlex.quote(str(outside))} >/dev/null 2>&1; then exit 11; fi; "
        f"if (printf changed >> {shlex.quote(str(source))}) 2>/dev/null; then exit 12; fi; "
        "printf RESTORED_WORKSPACE_WRITE > result; printf RESTORED_BOUNDARY_PASSED"
    )
    _respond_command(responses, command, "initial")
    _respond_command(responses, command, "restored")
    second = tmp_path / "second"
    second.mkdir()
    (second / "source").write_text("SECOND_READ_CONTENT\n")
    changed = tmp_path / "changed"
    changed.mkdir()
    environment = Environment.local(
        cwd=workspace, tools=["exec_command"], read_only_roots=[shared, second]
    )
    agent = _agent(responses)
    async with timeout(30):
        async with agent.session(environment=environment) as session:
            assert (await (await session.submit("Read.")).get_result()).final_output == "done"
            state = await session.save_state()
        for roots in [[], [shared, changed]]:
            with pytest.raises(ValueError, match="applied executor policy"):
                async with agent.session(
                    state=state,
                    environment=Environment.local(
                        cwd=workspace, tools=["exec_command"], read_only_roots=roots
                    ),
                ):
                    pytest.fail("A changed read grant restored a checkpoint")
            assert len(responses.requests) == 2
        reordered = Environment.local(
            cwd=workspace, tools=["exec_command"], read_only_roots=[second, shared]
        )
        async with agent.session(state=state, environment=reordered) as restored:
            assert (
                await (await restored.submit("Read after restore.")).get_result()
            ).final_output == "done"
    for index, call_id in [(1, "initial"), (3, "restored")]:
        actual = tool_output(responses.requests[index], call_id)
        assert "Process exited with code 0" in actual
        assert "RESTORED_READ_CONTENT" in actual
        assert "SECOND_READ_CONTENT" in actual
        assert "RESTORED_BOUNDARY_PASSED" in actual
        assert "RESTORED_PRIVATE_CONTENT" not in actual
    assert source.read_text() == "RESTORED_READ_CONTENT\n"
    assert outside.read_text() == "RESTORED_PRIVATE_CONTENT\n"
    assert (workspace / "result").read_text() == "RESTORED_WORKSPACE_WRITE"


@pytest.mark.parametrize("transport", ["local", "strict", "legacy"])
@pytest.mark.parametrize("explicit_empty", [False, True])
def test_empty_read_roots_preserve_existing_native_settings(
    transport: str, explicit_empty: bool
) -> None:
    options = {"read_only_roots": []} if explicit_empty else {}
    if transport == "local":
        declaration = Environment.local(cwd="/workspace", **options)
        expected = {
            "local": True,
            "cwd": "file:///workspace",
            "writable_roots": [],
            "headers": {},
            "connect_timeout": None,
        }
    else:
        declaration = Environment(
            url="wss://executor.invalid", cwd="/workspace", write_policy=transport, **options
        )
        expected = {
            "url": "wss://executor.invalid",
            "cwd": "file:///workspace",
            "headers": {},
            "connect_timeout": None,
        }
        if transport == "strict":
            expected["writable_roots"] = []
    assert declaration._to_native() == expected


def test_read_roots_follow_executor_path_conventions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    declaration = Environment.local(
        cwd="/workspace",
        read_only_roots=["file://localhost/z", PurePosixPath("/a"), "/z"],
        writable_roots=["/write"],
    )
    assert declaration._to_native()["read_only_roots"] == ["file:///a", "file:///z"]
    assert declaration._to_native()["writable_roots"] == ["file:///write"]
    with pytest.raises(ValueError, match="absolute executor directories"):
        Environment.local(cwd="/workspace", read_only_roots=["/shared/../private"])


def test_nonempty_remote_read_roots_are_rejected() -> None:
    with pytest.raises(ValueError, match="read_only_roots requires Environment.local on macOS"):
        Environment(url="wss://executor.invalid", cwd="/workspace", read_only_roots=["/shared"])


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_nonempty_local_read_roots_require_macos(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(ValueError, match="read_only_roots requires Environment.local on macOS"):
        Environment.local(cwd="/workspace", read_only_roots=["/shared"])
    assert "read_only_roots" not in Environment.local(cwd="/workspace")._to_native()
    assert (
        "read_only_roots"
        not in Environment.local(cwd="/workspace", read_only_roots=[])._to_native()
    )


@native_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="local read grants require macOS")
@pytest.mark.parametrize(
    "kind", ["root_alias", "cwd_alias", "writable", "hardlink", "fifo", "storage"]
)
async def test_unsafe_local_read_roots_refuse_native_startup(
    responses: ResponsesFixture, tmp_path: Path, kind: str
) -> None:
    root = tmp_path.resolve()
    workspace = root / "workspace"
    shared = root / "shared"
    workspace.mkdir()
    shared.mkdir()
    granted = shared
    writable = []
    storage = None
    reason = "read_only_roots"
    if kind == "root_alias":
        granted = root / "alias"
        granted.symlink_to(shared, target_is_directory=True)
        reason = "symlinks"
    elif kind == "cwd_alias":
        alias = root / "alias"
        alias.symlink_to(shared, target_is_directory=True)
        workspace = alias
        reason = "must not overlap cwd or writable roots"
    elif kind == "writable":
        writable = [shared]
        reason = "must not overlap cwd or writable roots"
    elif kind == "hardlink":
        source = workspace / "source"
        source.write_text("SYNTHETIC_SHARED_CONTENT\n")
        os.link(source, shared / "alias")
        reason = r"\bhard(?: link|-linked file)\b"
    elif kind == "fifo":
        os.mkfifo(shared / "pipe")
        reason = "must contain only regular files, directories, or symlinks"
    elif kind == "storage":
        storage = LocalStorage(shared)
        reason = "native SDK storage"
    with pytest.raises(RuntimeError, match=reason):
        async with _agent(responses).session(
            storage=storage,
            environment=Environment.local(
                cwd=workspace,
                tools=["exec_command"],
                read_only_roots=[granted],
                writable_roots=writable,
            ),
        ):
            pytest.fail("An unsafe local read grant opened a Session")
    assert responses.requests == []


@native_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="local read grants require macOS")
@pytest.mark.parametrize(
    "kind", ["login", "refresh_parent", "private_hardlink", "home_alias", "configured_home_alias"]
)
async def test_local_read_roots_reject_saved_login_and_refresh_parent(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from tests.test_native_codex_login import saved_login

    root = tmp_path.resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    home = saved_login(root / "login")
    login = CodexLogin(codex_home=home)
    granted = home
    reason = "saved Codex login"
    if kind == "refresh_parent":
        target = root / "refresh"
        target.mkdir()
        auth = home / "auth.json"
        auth.rename(target / "auth.json")
        auth.symlink_to(target / "auth.json")
        # This sibling contains no credentials, but refresh publishes beside
        # the resolved auth.json, so its parent must remain private as well.
        granted = target / "sibling"
        granted.mkdir()
        reason = "saved Codex login and refresh directories"
    elif kind == "private_hardlink":
        granted = root / "shared"
        granted.mkdir()
        os.link(home / "auth.json", granted / "alias")
        reason = "hard link to a private native SDK file"
    elif kind in ("home_alias", "configured_home_alias"):
        granted = root / "shared"
        granted.mkdir()
        alias = granted / "login"
        alias.symlink_to(home, target_is_directory=True)
        if kind == "configured_home_alias":
            monkeypatch.setenv("CODEX_HOME", str(alias))
            login = CodexLogin()
        else:
            login = CodexLogin(codex_home=alias)
        reason = "saved Codex login and refresh directories"
    agent = Agent(
        instructions="Use the selected command.",
        provider=NativeProvider(auth=login),
    )
    with pytest.raises(RuntimeError, match=reason):
        async with agent.session(
            environment=Environment.local(
                cwd=workspace, tools=["exec_command"], read_only_roots=[granted]
            )
        ):
            pytest.fail("A private saved-login or refresh directory opened as a read grant")
    assert responses.requests == []

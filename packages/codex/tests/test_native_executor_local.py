"""Direct local execution through the public Session facade."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Literal
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import agents
from agents import (
    Agent,
    Checklist,
    Environment,
    ExecutorContext,
    LocalStorage,
    NativeError,
    NativeProvider,
    NetworkPolicy,
    StaleTurnError,
    StdioMCP,
    TurnNeedsAttention,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import test_certificate as create_certificate
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.native_planning_fixtures import legacy_goal


def test_local_declaration_has_no_transport_and_normalizes_additional_roots() -> None:
    local = Environment.local(
        cwd=PurePosixPath("/job/worktree"),
        writable_roots=[PurePosixPath("/job/cache files"), "file://localhost/job/cache%20files/"],
    )
    assert local.url == "file:///job/worktree"
    assert replace(local, cwd="/job/new-worktree").url == "file:///job/new-worktree"
    with pytest.raises(ValueError, match="must use wss://"):
        Environment(url=local.url, cwd=local.cwd)
    assert local._to_native() == {
        "local": True,
        "cwd": "file:///job/worktree",
        "writable_roots": ["file:///job/cache%20files"],
        "headers": {},
        "connect_timeout": None,
    }
    env_vars = [
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "openai_api_key",
        "codex_api_key",
        "DECLARED_VAR",
    ]
    server = StdioMCP(
        name="local-tools",
        command="/bin/echo",
        tools=["echo"],
        location="environment",
        env_vars=env_vars,
    )
    declaration = server._config(local)
    assert declaration["environment_id"] == "local"
    assert declaration["cwd"] == "/job/worktree"
    assert declaration["env_vars"] == ["openai_api_key", "codex_api_key", "DECLARED_VAR"]
    host = replace(server, location="host")
    assert host._config(local)["env_vars"] == env_vars


def test_trusted_workspace_write_requires_a_local_opt_in() -> None:
    invalid_policy: Any = "unknown"
    with pytest.raises(ValueError, match="write_policy must be strict or trusted_workspace_write"):
        Environment.local(cwd="/job/worktree", write_policy=invalid_policy)
    with pytest.raises(ValueError, match="requires Environment.local"):
        Environment(
            url="wss://executor.example.invalid",
            cwd="/job/worktree",
            write_policy="trusted_workspace_write",
        )
    with pytest.raises(ValueError, match="requires trusted workspace-write"):
        Environment.local(cwd="/job/worktree", temp_dir="/job/temp")
    with pytest.raises(ValueError, match="Read-only execution requires strict"):
        Environment.local(
            cwd="/job/worktree", write_policy="trusted_workspace_write", read_only=True
        )


@pytest.mark.parametrize(
    "path", ["relative", "/", "/work/../cache", "file:///work/%2e%2e/cache", "file:///work/%00"]
)
def test_local_declaration_rejects_ambiguous_or_root_grants(path: str) -> None:
    with pytest.raises(ValueError, match="absolute executor directories"):
        Environment.local(cwd="/job", writable_roots=[path])


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
async def test_missing_local_root_refuses_session_before_model(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    with pytest.raises(RuntimeError, match="could not resolve an executor write root"):
        async with agent.session(
            environment=Environment.local(
                cwd=tmp_path, tools=["exec_command"], writable_roots=[tmp_path / "missing"]
            )
        ):
            pytest.fail("An unresolved write root opened a Session")
    assert responses.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize("trusted", [False, True], ids=["strict", "trusted"])
async def test_local_write_roots_cannot_include_the_native_helper_installation(
    responses: ResponsesFixture, tmp_path: Path, trusted: bool
) -> None:
    installation = Path(agents.__file__).resolve().parent
    alias = tmp_path / "helper-installation"
    alias.symlink_to(installation, target_is_directory=True)
    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    with pytest.raises(RuntimeError, match="writable roots must stay outside the native helper"):
        async with agent.session(
            environment=Environment.local(
                cwd=tmp_path,
                writable_roots=[alias],
                write_policy="trusted_workspace_write" if trusted else "strict",
            )
        ):
            pytest.fail("A writable native helper installation opened a Session")
    assert responses.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize("overlap", ["cwd", "additional_alias", "storage_alias", "descendant"])
async def test_local_write_roots_cannot_overlap_native_storage(
    responses: ResponsesFixture, tmp_path: Path, overlap: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    profile = tmp_path / "profile"
    extra = []
    if overlap == "cwd":
        profile = work / "new" / "profile"
    elif overlap == "additional_alias":
        shared = tmp_path / "shared"
        shared.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(shared, target_is_directory=True)
        extra = [alias]
        profile = shared / "profile"
    elif overlap == "storage_alias":
        alias = work / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        profile = alias / "profile"
    else:
        extra = [profile / "workspace"]
        extra[0].mkdir(parents=True)

    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    with pytest.raises(RuntimeError, match="writable roots must stay outside native SDK storage"):
        async with agent.session(
            storage=LocalStorage(profile),
            environment=Environment.local(cwd=work, writable_roots=extra, tools=["exec_command"]),
        ):
            pytest.fail("A writable storage profile opened a Session")
    if overlap == "descendant":
        assert list(profile.iterdir()) == extra
    else:
        assert not profile.exists()
    assert responses.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize(
    "location", ["sessions", "session-year", "session-date-grant", "archived_sessions"]
)
async def test_local_write_roots_cannot_overlap_relocated_rollout_directories(
    responses: ResponsesFixture, tmp_path: Path, location: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace.with_name("WORKSPACE")
    if target.exists():
        assert target.samefile(workspace)
    else:
        target = workspace

    storage = LocalStorage(tmp_path / "profile")
    cwd = workspace
    date = datetime.now(timezone.utc).astimezone()
    alias = storage.path / location
    if location == "session-year":
        alias = storage.path / "sessions" / str(date.year)
    elif location == "session-date-grant":
        alias = storage.path / "sessions"
        # A fresh recorder will create the missing day directory inside this grant.
        cwd = workspace / str(date.year) / f"{date.month:02d}"
        cwd.mkdir(parents=True)
    alias.parent.mkdir(parents=True)
    alias.symlink_to(target, target_is_directory=True)
    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )

    with pytest.raises(RuntimeError, match="writable roots must stay outside native SDK storage"):
        async with agent.session(storage=storage, environment=Environment.local(cwd=cwd)):
            pytest.fail("A write grant included the relocated rollout directory")

    assert responses.requests == []
    assert list(cwd.iterdir()) == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.parametrize("grant_history", [False, True], ids=["disjoint", "writable"])
async def test_local_resume_keeps_relocated_rollout_files_outside_write_roots(
    responses: ResponsesFixture, tmp_path: Path, grant_history: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = tmp_path / "relocated-sessions"
    sessions.mkdir()
    storage = LocalStorage(tmp_path / "profile")
    storage.path.mkdir()
    (storage.path / "sessions").symlink_to(sessions, target_is_directory=True)
    # The local runtime does not select unrelated links as rollout storage.
    (sessions / "unrelated").symlink_to(workspace, target_is_directory=True)
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Answer.", provider=provider)
    environment = Environment.local(cwd=workspace)

    async with provider:
        responses.respond(assistant_output("original"))
        async with agent.session(storage=storage, environment=environment) as session:
            thread_id = session.id
            assert (await (await session.submit("First.")).get_result()).final_output == "original"

        (rollout,) = sessions.rglob(f"*{thread_id}.jsonl")
        target = workspace if grant_history else tmp_path / "relocated-history"
        target.mkdir(exist_ok=True)
        relocated = rollout.rename(target / rollout.name)
        rollout.symlink_to(relocated)
        original_history = relocated.read_bytes()

        if grant_history:
            with pytest.raises(
                RuntimeError, match="writable roots must stay outside native SDK storage"
            ):
                async with agent.session(
                    storage=storage, resume=thread_id, environment=environment
                ):
                    pytest.fail("A write grant included the relocated recorded rollout")
            assert relocated.read_bytes() == original_history
            assert len(responses.requests) == 1
        else:
            responses.respond(assistant_output("resumed"))
            async with agent.session(
                storage=storage, resume=thread_id, environment=environment
            ) as session:
                assert session.id == thread_id
                result = await (await session.submit("Second.")).get_result()
                assert result.final_output == "resumed"
                # The native writer now records the physical path in SQLite.
                # Removing its old alias must not allow a later write grant.
                rollout.unlink()
                refused = await session.submit("Refuse this grant.", writable_roots=[target])
                with pytest.raises(
                    NativeError, match="writable roots must stay outside native SDK storage"
                ):
                    await refused.get_result()
            assert b"resumed" in relocated.read_bytes()[len(original_history) :]
            with pytest.raises(
                RuntimeError, match="writable roots must stay outside native SDK storage"
            ):
                async with agent.session(
                    storage=storage,
                    resume=thread_id,
                    environment=Environment.local(cwd=workspace, writable_roots=[target]),
                ):
                    pytest.fail("A write grant included the recorded physical rollout")
            assert len(responses.requests) == 2


def _relocate_database(database: Path, directory: Path) -> Path:
    directory.mkdir()
    target = database.rename(directory / database.name)
    database.symlink_to(target)
    return target


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize("location", ["sdk-checklists", "sdk-checklists/budgets"])
async def test_local_open_rejects_a_writable_checklist_directory(
    responses: ResponsesFixture, tmp_path: Path, location: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = LocalStorage(tmp_path / "profile")
    alias = storage.path / location
    alias.parent.mkdir(parents=True)
    alias.symlink_to(workspace, target_is_directory=True)
    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )

    with pytest.raises(RuntimeError, match="writable roots must stay outside native SDK storage"):
        async with agent.session(storage=storage, environment=Environment.local(cwd=workspace)):
            pytest.fail("A write grant included relocated checklist storage")
    assert responses.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_replacement_rejects_a_relocated_checklist_hard_link(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    workspace, sidecars, grant = (tmp_path / name for name in ("workspace", "sidecars", "grant"))
    for directory in (workspace, sidecars, grant):
        directory.mkdir()
    storage = LocalStorage(tmp_path / "profile")
    storage.path.mkdir()
    (storage.path / "sdk-checklists").symlink_to(sidecars, target_is_directory=True)
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Answer.", provider=provider, tools=[Checklist()])
    environment = Environment.local(cwd=workspace, tools=["exec_command"])
    plan = [{"step": "An item", "status": "in_progress"}]
    responses.respond(
        function_call("update_plan", {"plan": plan, "explanation": "initial"}, "plan")
    )
    responses.respond(assistant_output("saved"))

    async with (
        timeout(30),
        provider,
        agent.session(storage=storage, environment=environment) as session,
    ):
        assert (await (await session.submit("Create a plan.")).get_result()).final_output == "saved"
        checklist = await session.checklist()
        assert checklist is not None and checklist.explanation == "initial"
        canonical = sidecars / f"{session.id}.json"
        record = canonical
        alias = grant / "ordinary-name"
        request_count = len(responses.requests)
        responses.respond(assistant_output("done"))
        for spelling in (
            canonical,
            canonical.with_suffix(".jſon"),
            sidecars / canonical.name.upper(),
        ):
            if not spelling.exists():
                continue
            if spelling != record:
                record.rename(sidecars / "rename-checklist").rename(spelling)
                assert spelling.name in {path.name for path in sidecars.iterdir()}
                assert await session.checklist() == checklist
                record = spelling
            alias.hardlink_to(record)
            refused = await session.submit("Do not grant the checklist.", writable_roots=[grant])
            with pytest.raises(NativeError, match="writable roots contain a hard link") as error:
                await refused.get_result()
            assert error.value.effects == "not_started"
            assert len(responses.requests) == request_count
            assert await session.checklist() == checklist
            alias.unlink()

        ordinary = tmp_path / "ordinary-file"
        ordinary.write_text("ordinary")
        alias.hardlink_to(ordinary)
        # An app-owned link outside the reserved sidecar directories is unrelated.
        (storage.path / "unrelated").symlink_to(grant, target_is_directory=True)
        allowed = await session.submit("Grant an ordinary link.", writable_roots=[grant])
        assert (await allowed.get_result()).final_output == "done"
        assert await session.checklist() == checklist


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize("relocate", [False, True], ids=["profile", "relocated"])
async def test_local_open_rejects_a_storage_database_hard_link(
    responses: ResponsesFixture, tmp_path: Path, relocate: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = LocalStorage(tmp_path / "profile")
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Answer.", provider=provider)

    async with provider:
        async with agent.session(storage=storage):
            pass
        (database,) = storage.path.glob("state_*.sqlite")
        if relocate:
            database = _relocate_database(database, tmp_path / "relocated")
            selected = Environment.local(cwd=workspace, writable_roots=[database.parent])
            with pytest.raises(
                RuntimeError, match="writable roots must stay outside native SDK storage"
            ):
                async with agent.session(storage=storage, environment=selected):
                    pytest.fail("A write grant directly included the relocated database")
        (workspace / "ordinary-name").hardlink_to(database)
        with pytest.raises(RuntimeError, match="writable roots contain a hard link"):
            async with agent.session(storage=storage, environment=Environment.local(cwd=workspace)):
                pytest.fail("A write grant could modify the private database through its alias")

    assert responses.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.parametrize("private_file", ["relocated-wal", "runtime-goal"])
async def test_local_replacement_rejects_a_private_file_hard_link(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_file: str,
) -> None:
    workspace, grant, disjoint, runtime_parent = (
        tmp_path / name for name in ("workspace", "grant", "disjoint", "runtime")
    )
    for directory in (workspace, grant, disjoint, runtime_parent):
        directory.mkdir()
    monkeypatch.setenv("TMPDIR", str(runtime_parent))
    storage = LocalStorage(tmp_path / "profile") if private_file == "relocated-wal" else None
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Answer.", provider=provider)
    selected = Environment.local(
        cwd=workspace, tools=["exec_command", "write_stdin", "apply_patch"]
    )

    async with timeout(30), provider:
        state = None
        if storage is not None:
            async with agent.session(storage=storage):
                pass
            (database,) = storage.path.glob("state_*.sqlite")
            database = _relocate_database(database, tmp_path / "relocated")
            source = database.with_name(database.name + "-wal")
        else:
            async with agent.session(environment=selected) as original:
                checkpoint = json.loads((await original.save_state()).to_json())
                checkpoint["root_goal"] = legacy_goal(original.id)
            state = SessionState.from_json(json.dumps(checkpoint))
            source = None
        async with agent.session(storage=storage, state=state, environment=selected) as session:
            if source is None:
                (source,) = runtime_parent.glob("native-agent-*/goals_*.sqlite")
                assert (await session.save_state()).root_goal is not None
            (disjoint / "backup").hardlink_to(source)
            alias = grant / "nested" / "ordinary-name"
            alias.parent.mkdir()
            alias.hardlink_to(source)

            responses.respond(assistant_output("allowed"))
            refused = await session.submit(
                "Do not grant a private alias.", writable_roots=[grant.resolve()]
            )
            with pytest.raises(NativeError, match="writable roots contain a hard link") as error:
                await refused.get_result()
            assert error.value.effects == "not_started"
            assert responses.requests == []

            alias.unlink()
            ordinary_file = disjoint / "ordinary-file"
            ordinary_file.write_text("ordinary workspace content")
            alias.hardlink_to(ordinary_file)
            allowed = await session.submit(
                "Grant an ordinary alias.", writable_roots=[grant.resolve()]
            )
            assert (await allowed.get_result()).final_output == "allowed"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_write_roots_cannot_overlap_native_runtime_home(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_parent, cwd, cache = (tmp_path / name for name in ("runtime", "cwd", "cache"))
    for directory in (runtime_parent, cwd, cache):
        directory.mkdir()
    monkeypatch.setenv("TMPDIR", str(runtime_parent))
    overlap = "writable roots must stay outside the native SDK runtime home"
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    selected = Environment.local(cwd=cwd, writable_roots=[cache], tools=["exec_command"])
    responses.respond(assistant_output("done"))

    async with provider:
        with pytest.raises(RuntimeError, match=overlap):
            async with agent.session(environment=Environment.local(cwd=runtime_parent)):
                pytest.fail("A writable runtime home opened a Session")
        async with agent.session(environment=selected) as session:
            refused = await session.submit(
                "Do not grant the runtime home.", writable_roots=[runtime_parent]
            )
            with pytest.raises(NativeError, match=overlap) as error:
                await refused.get_result()
            assert error.value.effects == "not_started"
            assert responses.requests == []

            (home,) = runtime_parent.glob("native-agent-*")
            external = tmp_path / "outside-runtime"
            external.mkdir()
            alias = home / "unverified-directory"
            alias.symlink_to(external, target_is_directory=True)
            refused = await session.submit(
                "Do not follow the runtime link.", writable_roots=[cache]
            )
            with pytest.raises(
                NativeError, match="runtime home contains an unsupported symbolic link"
            ):
                await refused.get_result()
            assert responses.requests == []
            alias.unlink()

            allowed = await session.submit("Use the sibling cache.", writable_roots=[cache])
            assert (await allowed.get_result()).final_output == "done"


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux system helper paths")
@pytest.mark.parametrize("system_root", ["/bin", "/usr"])
async def test_local_write_roots_cannot_include_system_sandbox_helper_paths(
    responses: ResponsesFixture, tmp_path: Path, system_root: str
) -> None:
    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    with pytest.raises(RuntimeError, match="writable roots must stay outside the native helper"):
        async with agent.session(
            environment=Environment.local(cwd=tmp_path, writable_roots=[system_root])
        ):
            pytest.fail("A writable system helper path opened a Session")
    assert responses.requests == []


@pytest.fixture
def undeclared_scratch() -> Iterator[Path]:
    # The process Seatbelt defaults otherwise expose /private/tmp, independent
    # of pytest's (usually per-user) temporary directory on macOS.
    with TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def isolated_github_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_shell_patch_and_linked_worktree_respect_explicit_roots(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    undeclared_scratch: Path,
) -> None:
    git_executable = shutil.which("git")
    assert git_executable is not None
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-local-model-key")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-local-codex-key")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "synthetic-local-aws-token")
    monkeypatch.setenv("DATABASE_URL", "synthetic-local-database-url")

    def git(cwd: Path, *args: str) -> str:
        return subprocess.check_output([git_executable, "-C", str(cwd), *args], text=True).strip()

    origin = tmp_path / "origin"
    origin.mkdir()
    linked = tmp_path / "linked"
    cache = tmp_path / "cache"
    cache.mkdir()
    git(origin, "init", "-q")
    git(
        origin,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "initial",
    )
    git(origin, "worktree", "add", "-q", "-b", "linked", str(linked))
    head = git(linked, "rev-parse", "HEAD")
    gitdir = Path(git(linked, "rev-parse", "--absolute-git-dir"))
    common = origin / ".git"
    outside = tmp_path / "outside"
    outside.write_text("protected")
    scratch_file = undeclared_scratch / "outside"
    scratch_file.write_text("protected")
    untrusted_bin = tmp_path / "bin"
    untrusted_bin.mkdir()
    bwrap = untrusted_bin / "bwrap"
    bwrap_ran = tmp_path / "untrusted-bwrap-ran"
    bwrap.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(bwrap_ran))}\nexit 1\n")
    bwrap.chmod(0o755)
    monkeypatch.setenv("PATH", str(untrusted_bin), prepend=os.pathsep)
    (linked / "escape").symlink_to(outside)
    (linked / ".agents").mkdir()
    (linked / ".agents" / "protected").write_text("metadata")

    selected = Environment.local(
        cwd=linked,
        writable_roots=[gitdir, common, cache],
        tools=["exec_command", "apply_patch"],
    )
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "patch",
                "input": "*** Begin Patch\n*** Add File: result.txt\n+changed\n*** End Patch",
            },
        }
    )
    sandbox_git = git_executable
    if sys.platform == "darwin":
        # Stage the Apple Git binary itself so Seatbelt does not have to load
        # the Xcode shim or reach into Homebrew.
        apple_git = subprocess.check_output(["/usr/bin/xcrun", "--find", "git"], text=True).strip()
        sandbox_git = shutil.copy(apple_git, str(cache / "git"))
    git_path = shlex.quote(sandbox_git)
    scratch_path = shlex.quote(str(scratch_file))
    command = (
        'set -e; test -z "${OPENAI_API_KEY+x}${CODEX_API_KEY+x}'
        '${AWS_SESSION_TOKEN+x}${DATABASE_URL+x}"; '
        f"if cat {shlex.quote(str(outside))} >/dev/null 2>&1; then exit 14; fi; "
        "if cat escape >/dev/null 2>&1; then exit 15; fi; "
        f"if cat {scratch_path} >/dev/null 2>&1; then exit 16; fi; "
        f"if (printf denied > {scratch_path}) 2>/dev/null; then exit 17; fi; "
        f"printf cached > {shlex.quote(str(cache / 'marker'))}; "
        # A Linux sandbox can create a private shadow of the missing target.
        # Check the host file below after attempting to follow the symlink.
        "(printf denied > escape) 2>/dev/null || true; "
        "if (printf denied > .agents/protected) 2>/dev/null; then exit 12; fi; "
        "if (printf denied >> .git) 2>/dev/null; then exit 13; fi; "
        "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1; "
        f"{git_path} add result.txt; "
        f"{git_path} -c user.name=Fixture -c user.email=fixture@example.invalid -c gc.auto=0 "
        "commit -qm local-change"
    )
    responses.respond(function_call("exec_command", {"cmd": command, "login": False}, "command"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(30),
        Agent(
            instructions="Use the selected tools.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
        ).session(environment=selected) as session,
    ):
        turn = await session.submit("Patch and commit the artifact.")
        assert (await turn.get_result()).final_output == "done"
    assert "Process exited with code 0" in tool_output(responses.requests[-1], "command")
    assert git(linked, "rev-parse", "HEAD") != head
    assert git(linked, "show", "HEAD:result.txt") == "changed"
    assert (cache / "marker").read_text() == "cached"
    assert outside.read_text() == "protected"
    assert scratch_file.read_text() == "protected"
    assert not bwrap_ran.exists()
    assert (linked / ".agents" / "protected").read_text() == "metadata"


def _macos_identity_probe(directory: Path) -> str:
    source = directory / "identity-probe.c"
    probe = directory / "identity-probe"
    source.write_text(
        r"""
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <stdio.h>
#include <unistd.h>

// Match the no-report global-name query in the pinned Seatbelt tests.
extern const int SANDBOX_CHECK_NO_REPORT;
extern int sandbox_check(pid_t, const char *, int, ...);

int main(void) {
    const int global_name_filter = 2;
    const char *services[] = {
        "com.apple.system.opendirectoryd.libinfo",
        "com.apple.system.DirectoryService.libinfo_v1",
        "com.apple.system.opendirectoryd.membership",
    };
    mach_port_t port = MACH_PORT_NULL;
    kern_return_t status = task_get_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, &port);
    int present = status == KERN_SUCCESS && MACH_PORT_VALID(port);
    printf("bootstrap port status=%d present=%d\n", status, present);
    if (present) {
        mach_port_deallocate(mach_task_self(), port);
    }
    for (size_t i = 0; i < sizeof(services) / sizeof(services[0]); ++i) {
        int result = sandbox_check(
            getpid(), "mach-lookup", global_name_filter | SANDBOX_CHECK_NO_REPORT, services[i]
        );
        printf("mach-lookup %s=%d\n", services[i], result);
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            ["/usr/bin/xcrun", "--sdk", "macosx", "clang", "-o", str(probe), str(source)],
            capture_output=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"printf 'identity probe build unavailable (%s)\\n' {type(error).__name__}"
    return f"{shlex.quote(str(probe))} || printf 'identity probe exit=%s\\n' \"$?\""


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.usefixtures("isolated_github_cli")
async def test_trusted_local_shell_uses_host_context_and_fences_linked_worktree_writes(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    undeclared_scratch: Path,
) -> None:
    home, bin_dir, origin, linked, sibling, job_temp, capabilities = (
        tmp_path / name
        for name in ("home", "bin", "origin", "linked", "sibling", "job-temp", "capabilities")
    )
    for path in (home, bin_dir, origin, job_temp, capabilities / "skills"):
        path.mkdir(parents=True)
    (home / ".gitconfig").write_text("[credential]\n    helper = store\n")
    (home / ".git-credentials").write_text(
        "https://fixture:synthetic-git-password@credential.example.invalid\n"
    )
    tool = bin_dir / "fixture-tool"
    tool.write_text("#!/bin/sh\nprintf 'fixture tool\\n'\n")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    monkeypatch.setenv("GH_TOKEN", "synthetic-gh-token")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-codex-key")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    def git(cwd: Path, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git(origin, "init", "-q")
    git(
        origin,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "initial",
    )
    git(origin, "worktree", "add", "-q", "-b", "linked", str(linked))
    git(origin, "worktree", "add", "-q", "-b", "sibling", str(sibling))
    gitdir = Path(git(linked, "rev-parse", "--absolute-git-dir"))
    for path in (sibling / "marker", undeclared_scratch / "marker"):
        path.write_text("readable")
    (linked / ".agents").mkdir()
    (linked / ".agents" / "marker").write_text("protected")

    shared_skill = tmp_path / "shared" / "selected-skill"
    other_skill = tmp_path / "unselected" / "skills" / "hidden-skill"
    for path in (shared_skill, other_skill):
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {path.name}\ndescription: Fixture for {path.name}.\n---\nRead me.\n"
        )
    (capabilities / "skills" / shared_skill.name).symlink_to(shared_skill, target_is_directory=True)
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path / "library-path"))

    selected = Environment.local(
        cwd=linked,
        writable_roots=[gitdir, origin / ".git", job_temp],
        tools=["exec_command", "write_stdin"],
        capability_directories=[str(capabilities)],
        write_policy="trusted_workspace_write",
        temp_dir=job_temp,
    )
    # Darwin's id -F fails when getpwuid cannot resolve the real UID.
    macos_uid_lookup = ("/usr/bin/id", "-F")
    ssh = "/usr/bin/ssh" if sys.platform == "darwin" else "ssh"
    ssh_command = f"{ssh} -G -T -F /dev/null credential.example.invalid"
    if sys.platform == "darwin":
        identity_probe = _macos_identity_probe(linked)
        ssh_probe = (
            f"ssh_config=$({ssh_command} & ssh_pid=$!; "
            'if ! wait "$ssh_pid"; then '
            'printf "trusted SSH pid=%s, UID lookup exit=%s\\n" "$ssh_pid" "$uid_status" >&2; '
            "exit 1; fi) && printf '%s\\n' \"$ssh_config\" | grep -q '^user '; "
        )
        # Keep checking Git and write grants after recording the real SSH exit.
        ssh_probe = (
            f"{identity_probe}; "
            f"uid_status=0; {shlex.join(macos_uid_lookup)} >/dev/null 2>&1 || uid_status=$?; "
            'printf "trusted UID lookup exit=%s\\n" "$uid_status"; '
            f"ssh_status=0; ({ssh_probe}) || ssh_status=$?; "
            'printf "trusted SSH config exit=%s\\n" "$ssh_status"; '
        )
    else:
        ssh_probe = f"{ssh_command} | grep -q '^user '; "
    command = (
        'set -eu; test "$(fixture-tool)" = "fixture tool"; '
        f'test "$HOME" = {shlex.quote(str(home))}; '
        'test "$GH_TOKEN" = synthetic-gh-token; '
        'test -z "${OPENAI_API_KEY+x}${CODEX_API_KEY+x}${LD_LIBRARY_PATH+x}"; '
        f"{ssh_probe}"
        "printf 'protocol=https\\nhost=credential.example.invalid\\n\\n' "
        "| git credential fill | grep -qx password=synthetic-git-password; "
        'scratch=$(mktemp -d "$TMPDIR/sdk-job.XXXXXX"); printf job > "$scratch/result"; '
        "if (printf denied > .agents/marker) 2>/dev/null; then exit 12; fi; "
        "if (printf denied >> .git) 2>/dev/null; then exit 13; fi; "
    )
    if shutil.which("gh") is not None:
        command += 'test "$(gh auth token --hostname github.com)" = synthetic-gh-token; '
    for path in (sibling / "marker", undeclared_scratch / "marker"):
        quoted = shlex.quote(str(path))
        command += (
            f'test "$(cat {quoted})" = readable; '
            f"if (printf denied > {quoted}) 2>/dev/null; then exit 14; fi; "
        )
    command += (
        "printf artifact > result.txt; git add result.txt; "
        "git -c user.name=Fixture -c user.email=fixture@example.invalid -c gc.auto=0 "
        "commit -qm local-change"
    )
    responses.respond(
        function_call(
            "exec_command", {"cmd": command, "login": False, "yield_time_ms": 1000}, "trusted"
        )
    )
    command_outputs: list[str] = []
    completion_id = "trusted"

    class UntilCommandDone(deque[list[dict[str, Any]]]):
        def __bool__(self) -> bool:
            return True

        def popleft(self) -> list[dict[str, Any]]:
            nonlocal completion_id
            if len(self):
                return super().popleft()
            output = tool_output(responses.requests[-1], completion_id)
            command_outputs.append(output)
            running = re.search(r"(?m)^Process running with session ID (-?\d+)\r?$", output)
            if running:
                completion_id = f"trusted-poll-{len(command_outputs)}"
                return [
                    function_call(
                        "write_stdin",
                        {"session_id": int(running[1]), "chars": "", "yield_time_ms": 1000},
                        completion_id,
                    )
                ]
            return [assistant_output("done")]

    responses.replies = UntilCommandDone(responses.replies)
    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    async with timeout(40), agent.session(environment=selected) as session:
        turn = await session.submit("Create and commit the artifact.")
        assert (await turn.get_result()).final_output == "done"
    output = "\n".join(command_outputs)
    failure = output
    if sys.platform == "darwin" and (
        probe := re.search(r"^trusted SSH pid=([0-9]+), UID lookup exit=[0-9]+\r?$", output, re.M)
    ):
        pid = probe[1]
        denial = "no matching Mach lookup denial visible"
        try:
            log = subprocess.run(
                [
                    "/usr/bin/log",
                    "show",
                    "--last",
                    "1m",
                    "--predicate",
                    f'eventMessage CONTAINS "ssh({pid}) deny(" '
                    'AND eventMessage CONTAINS " mach-lookup "',
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if match := re.search(rf"ssh\({pid}\) deny\([0-9]+\) mach-lookup [\w.-]+", log.stdout):
                denial = match[0]
        except (OSError, subprocess.TimeoutExpired):
            pass
        control = (
            f"status=0; {shlex.join(macos_uid_lookup)} >/dev/null 2>&1 || status=$?; "
            'printf "UID lookup exit=%s\\n" "$status"; '
            f"status=0; {ssh_command} >/dev/null 2>&1 || status=$?; "
            'printf "SSH config exit=%s\\n" "$status"; '
            f"{identity_probe}"
        )
        host_lookup = "identity probe unavailable"
        try:
            host_lookup = subprocess.run(
                ["/bin/sh", "-c", control],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": control, "login": False, "yield_time_ms": 5000},
                "strict-identity",
            )
        )
        responses.respond(assistant_output("done"))
        strict_environment = Environment.local(cwd=linked, tools=["exec_command"])
        try:
            async with timeout(20), agent.session(environment=strict_environment) as session:
                turn = await session.submit("Check account lookup.")
                await turn.get_result()
            strict_lookup = tool_output(responses.requests[-1], "strict-identity")
        except (AssertionError, TimeoutError, NativeError) as error:
            strict_lookup = f"identity probe unavailable ({type(error).__name__})"
        failure = f"{output}\nHost {host_lookup}\nStrict {strict_lookup}\n{denial}"
    assert "Process exited with code 0" in output, failure
    advertised = json.dumps(responses.requests[0])
    assert "Fixture for selected-skill" in advertised
    assert "Fixture for hidden-skill" not in advertised
    assert git(linked, "show", "HEAD:result.txt") == "artifact"
    assert not (sibling / "result.txt").exists()
    assert (sibling / "marker").read_text() == "readable"
    assert (undeclared_scratch / "marker").read_text() == "readable"
    assert (linked / ".agents" / "marker").read_text() == "protected"
    (job_result,) = job_temp.glob("sdk-job.*/result")
    assert job_result.read_text() == "job"
    if sys.platform == "darwin":
        assert "trusted SSH config exit=0" in output.splitlines(), (
            f"{failure}\nTrusted Git, job temp, and write-fence assertions passed."
        )
        assert "trusted UID lookup exit=0" in output.splitlines(), failure


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Xcode and Homebrew")
@pytest.mark.requires_native_macos_sandbox
@pytest.mark.usefixtures("isolated_github_cli")
async def test_trusted_local_runs_xcode_shims_and_homebrew(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("brew") is None:
        pytest.skip("Homebrew is required")
    monkeypatch.setenv("HOMEBREW_NO_AUTO_UPDATE", "1")
    monkeypatch.setenv("HOMEBREW_NO_ANALYTICS", "1")
    prefix = subprocess.check_output(["brew", "--prefix"], text=True).strip()
    command = (
        "set -e; /usr/bin/xcrun --no-cache --find git; /usr/bin/git --version; "
        "/usr/bin/make --version; brew --version; brew --prefix"
    )
    bash = Path(prefix) / "bin" / "bash"
    if bash.is_file():
        command += f"; {shlex.quote(str(bash))} -c 'printf homebrew-bash'"
    responses.respond(function_call("exec_command", {"cmd": command, "login": False}, "dev-tools"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    selected = Environment.local(
        cwd=tmp_path,
        tools=["exec_command"],
        write_policy="trusted_workspace_write",
        temp_dir=tmp_path,
    )
    async with timeout(40), agent.session(environment=selected) as session:
        turn = await session.submit("Run the developer tools.")
        assert (await turn.get_result()).final_output == "done"
    output = tool_output(responses.requests[-1], "dev-tools")
    assert "Process exited with code 0" in output
    assert "git version" in output
    assert "GNU Make" in output
    assert "Homebrew " in output
    assert prefix in output.splitlines()
    if bash.is_file():
        assert "homebrew-bash" in output


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.usefixtures("isolated_github_cli")
@pytest.mark.parametrize("explicit_network", [False, True], ids=["default-deny", "explicit-allow"])
async def test_trusted_local_shell_verifies_tls_using_an_inherited_ca(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_network: bool,
) -> None:
    ca_dir, workspace = tmp_path / "certificates", tmp_path / "workspace"
    ca_dir.mkdir()
    workspace.mkdir()
    ca, context = await create_certificate(ca_dir, "IP:127.0.0.1")
    monkeypatch.setenv("CURL_CA_BUNDLE", str(ca))
    app = web.Application()
    request_count = 0

    async def peer(request: web.Request) -> web.Response:
        nonlocal request_count
        assert request.secure
        request_count += 1
        return web.Response(text="verified local TLS")

    app.router.add_get("/", peer)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(ssl=context)
    selected = Environment.local(
        cwd=workspace,
        tools=["exec_command"],
        write_policy="trusted_workspace_write",
        network=NetworkPolicy(
            allowed_domains=["127.0.0.1"],
            allow_all_unix_sockets=True,
            allow_local_binding=True,
        )
        if explicit_network
        else None,
    )
    proxy_args = (
        "--proxy \"$HTTPS_PROXY\" --noproxy ''" if explicit_network else "--proxy '' --noproxy '*'"
    )
    command = (
        f"curl -q --fail --silent --show-error --max-time 10 {proxy_args} "
        f"{shlex.quote(str(server.make_url('/')))}"
    )
    responses.respond(
        function_call(
            "exec_command", {"cmd": command, "login": False, "yield_time_ms": 20_000}, "tls"
        )
    )
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    async with server, timeout(40), agent.session(environment=selected) as session:
        turn = await session.submit("Read the local TLS peer.")
        assert (await turn.get_result()).final_output == "done"
    output = tool_output(responses.requests[-1], "tls")
    if explicit_network:
        assert "Process exited with code 0" in output, output
        assert "verified local TLS" in output
        assert request_count == 1
    else:
        assert re.search(r"Process exited with code (?:7|28)\b", output), output
        assert request_count == 0


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.parametrize("trusted", [False, True], ids=["strict", "trusted"])
@pytest.mark.usefixtures("isolated_github_cli")
async def test_local_write_root_replacement_survives_portable_resume(
    responses: ResponsesFixture, tmp_path: Path, trusted: bool
) -> None:
    cwd, cache, sibling = (tmp_path / name for name in ("cwd", "cache", "sibling"))
    for directory in (cwd, cache, sibling):
        directory.mkdir()
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    selected = Environment.local(
        cwd=cwd,
        writable_roots=[cache],
        tools=["exec_command"],
        write_policy="trusted_workspace_write" if trusted else "strict",
        temp_dir=cache if trusted else None,
    )
    (cache / "marker").write_text("initial")
    cache_file = shlex.quote(str(cache / "marker"))
    sibling_file = shlex.quote(str(sibling / "marker"))
    revised = (
        f"if (printf denied > {cache_file}) 2>/dev/null; then exit 11; fi; "
        f"printf expanded >> {sibling_file}"
    )
    if trusted:
        revised = (
            f'test "$TMPDIR" = {shlex.quote(str(cache))} || exit 12; '
            f'test "$(cat {cache_file})" = initial || exit 13; '
            'if mktemp -d "$TMPDIR/sdk-job.XXXXXX" 2>/dev/null; then exit 14; fi; ' + revised
        )
    responses.respond(assistant_output("replace"))
    responses.respond(function_call("exec_command", {"cmd": revised, "login": False}, "resume"))
    responses.respond(assistant_output("resume"))

    async with timeout(40), provider:
        async with agent.session(environment=selected) as session:
            updated = await session.submit(
                "Change the write policy.", writable_roots=[sibling.resolve()]
            )
            assert (await updated.get_result()).final_output == "replace"
            saved = await session.save_state()
        with pytest.raises(ValueError, match="agree on the applied executor policy"):
            async with agent.session(environment=selected, state=saved):
                pytest.fail("Portable resume accepted superseded grants")
        if trusted:
            with pytest.raises(ValueError, match="agree on the applied executor policy"):
                async with agent.session(
                    environment=replace(
                        selected, write_policy="strict", temp_dir=None, writable_roots=[sibling]
                    ),
                    state=saved,
                ):
                    pytest.fail("Portable resume accepted a different local write policy")
        assert len(responses.requests) == 1
        async with agent.session(
            environment=replace(selected, writable_roots=[sibling]),
            state=saved,
        ) as resumed:
            turn = await resumed.submit("Append to the new artifact.")
            assert (await turn.get_result()).final_output == "resume"
            assert "Process exited with code 0" in tool_output(responses.requests[-1], "resume")
    assert (cache / "marker").read_text() == "initial"
    assert (sibling / "marker").read_text() == "expanded"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_turn_write_policy_and_reopen_checks_the_applied_policy(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    work, first, second, third = (tmp_path / name for name in ("work", "first", "second", "third"))
    for directory in (work, first, second, third):
        directory.mkdir()
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    storage = LocalStorage(tmp_path / "profile")
    network = NetworkPolicy(
        allowed_domains=["127.0.0.1"],
        denied_domains=["blocked.example.invalid"],
        allow_all_unix_sockets=True,
        allow_local_binding=True,
    )
    selected = Environment.local(
        cwd=work, writable_roots=[first, second], tools=["exec_command"], network=network
    )
    applied_roots = (third,)
    excluded_roots = (first, second)
    denials = {
        "blocked.example.invalid": (
            "domain is explicitly denied by policy and cannot be approved from this prompt"
        ),
        "8.8.8.8": "domain is not on the allowlist for the current sandbox mode",
    }
    hits: list[str] = []

    async def handle_peer(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.Response(text=f"boundary peer {request.path}")

    app = web.Application()
    app.router.add_get("/{stage}", handle_peer)
    curl = (
        'test -n "$HTTP_PROXY" && curl --silent --show-error --max-time 3 '
        '--proxy "$HTTP_PROXY" '
        "--noproxy ''"
    )

    def command(*, allow: tuple[Path, ...], deny: tuple[Path, ...]) -> str:
        parts = ["set -e", 'test -n "$HTTP_PROXY"']
        for directory in allow:
            parts.append(f"printf allowed >> {shlex.quote(str(directory / 'marker'))}")
        for directory in deny:
            path = shlex.quote(str(directory / "forbidden"))
            parts.append(f"if (printf forbidden > {path}) 2>/dev/null; then exit 12; fi")
        return "; ".join(parts)

    async def run_turn(session, call_id, cmd, **settings):
        start = len(responses.requests)
        responses.respond(function_call("exec_command", {"cmd": cmd, "login": False}, call_id))
        allowed_call = f"{call_id}-allow"
        target = shlex.quote(str(peer.make_url(f"/{call_id}")))
        responses.respond(
            function_call(
                "exec_command", {"cmd": f"{curl} --fail {target}", "login": False}, allowed_call
            )
        )
        # Core reports a blocked request as a tool failure, so probe each host separately.
        for host in denials:
            probe = f"{curl} --output /dev/null http://{host}/"
            responses.respond(
                function_call("exec_command", {"cmd": probe, "login": False}, f"{call_id}-{host}")
            )
        responses.respond(assistant_output(call_id))
        turn = await session.submit(call_id, **settings)
        assert (await turn.get_result()).final_output == call_id
        assert "Process exited with code 0" in tool_output(responses.requests[start + 1], call_id)
        allowed = tool_output(responses.requests[start + 2], allowed_call)
        assert "Process exited with code 0" in allowed
        assert f"boundary peer /{call_id}" in allowed
        for request, (host, reason) in zip(
            responses.requests[start + 3 :], denials.items(), strict=True
        ):
            output = tool_output(request, f"{call_id}-{host}")
            assert output.startswith("exec_command failed: ProcessFailed")
            assert host in output and f"was blocked: {reason}" in output
        return turn.turn_id

    async with timeout(120), provider, TestServer(app, host="127.0.0.1") as peer:
        async with agent.session(storage=storage, environment=selected) as session:
            session_id = session.id
            before = await run_turn(
                session, "original", command(allow=(work, first, second), deny=(third,))
            )
            request_count = len(responses.requests)
            overlap = await session.submit(
                "Do not grant the profile.", writable_roots=[storage.path.resolve()]
            )
            with pytest.raises(
                NativeError, match="writable roots must stay outside native SDK storage"
            ) as refused:
                await overlap.get_result()
            assert refused.value.effects == "not_started"
            assert len(responses.requests) == request_count
            bad = await session.submit("Invalid policy", writable_roots=[tmp_path / "missing"])
            with pytest.raises(NativeError, match="resolve an executor write root") as failure:
                await bad.get_result()
            assert failure.value.effects == "not_started"
            after = await run_turn(
                session,
                "replacement",
                command(allow=(work, *applied_roots), deny=excluded_roots),
                writable_roots=[root.resolve() for root in applied_roots],
            )
            inherited = await run_turn(
                session, "inherited", command(allow=(work, *applied_roots), deny=excluded_roots)
            )
            receipts = {
                turn["id"]: turn["receipt"]["executors"][0]
                async for turn in session.turns()
                if "receipt" in turn
            }
            assert set(receipts[before]["writable_roots"]) == {
                path.resolve().as_uri() for path in (first, second, work)
            }
            assert set(receipts[after]["writable_roots"]) == {
                path.resolve().as_uri() for path in (work, *applied_roots)
            }
            assert receipts[inherited] == receipts[after]
            assert "permission_profile" not in receipts[after]
            assert receipts[before]["network_sandbox"] == receipts[after]["network_sandbox"]
            assert receipts[before]["network_policy"] == receipts[after]["network_policy"]
            assert receipts[after]["network_policy"]["domains"] == {
                "127.0.0.1": "allow",
                "blocked.example.invalid": "deny",
            }

        with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
            async with agent.session(
                storage=storage,
                resume=session_id,
                environment=replace(selected, writable_roots=excluded_roots),
            ):
                pytest.fail("Reopen accepted different grants")
        replacement = replace(selected, writable_roots=[third])
        changed_network = replace(replacement, network=NetworkPolicy(allow_all_unix_sockets=True))
        request_count = len(responses.requests)
        with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
            async with agent.session(
                storage=storage, resume=session_id, environment=changed_network
            ):
                pytest.fail("Reopen accepted a different network policy with the same grants")
        assert len(responses.requests) == request_count
        async with agent.session(
            storage=storage, resume=session_id, environment=replacement
        ) as session:
            [restored_policy] = [
                turn["receipt"]["executors"][0]
                async for turn in session.turns()
                if turn["id"] == inherited
            ]
            assert restored_policy == receipts[inherited]
            await run_turn(
                session,
                "cleared",
                command(allow=(work,), deny=(first, second, third)),
                writable_roots=[],
            )
    assert hits == ["/original", "/replacement", "/inherited", "/cleared"]


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_root_change_refuses_a_retargeted_working_directory(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    original, replacement, aliases = [tmp_path / name for name in ("original", "other", "aliases")]
    for directory in (original, replacement, aliases):
        directory.mkdir()
    alias = aliases / "workspace"
    alias.symlink_to(original, target_is_directory=True)
    selected = Environment.local(cwd=alias, writable_roots=[aliases], tools=["exec_command"])
    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-local-key"),
    )
    responses.respond(assistant_output("ready"))
    async with timeout(30), agent.session(environment=selected) as session:
        assert (await (await session.submit("Start.")).get_result()).final_output == "ready"
        alias.unlink()
        alias.symlink_to(replacement, target_is_directory=True)
        responses.respond(assistant_output("unexpectedly admitted"))
        turn = await session.submit("Clear extra roots.", writable_roots=[])
        with pytest.raises(NativeError, match="executor working directory changed") as refused:
            await turn.get_result()
        assert refused.value.effects == "not_started"
        assert len(responses.requests) == 1


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_steer_twice_and_interrupt_running_local_command(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    work, sibling = tmp_path / "work", tmp_path / "sibling"
    work.mkdir()
    sibling.mkdir()
    heartbeat = work / "heartbeat"
    background = work / "background"
    selected = Environment.local(cwd=work, tools=["exec_command", "write_stdin"])
    responses.respond(
        function_call(
            "exec_command",
            {
                "cmd": "while :; do printf '.' >> background; sleep 0.05; done",
                "yield_time_ms": 250,
                "login": False,
            },
            "background",
        )
    )
    responses.respond(
        function_call(
            "exec_command",
            {
                "cmd": "while :; do printf '.' >> heartbeat; sleep 0.05; done",
                "yield_time_ms": 10000,
                "login": False,
            },
            "heartbeat",
        )
    )
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    async with (
        timeout(60),
        provider,
        Agent(instructions="Use the selected tools.", provider=provider).session(
            environment=selected
        ) as session,
    ):
        running = await session.submit("Start the command.")
        while not heartbeat.exists():
            await asyncio.sleep(0.01)
        assert "session ID" in tool_output(responses.requests[1], "background")
        turn_id = running.turn_id
        assert turn_id is not None
        first = await running.steer_exact("First correction.", expected_turn_id=turn_id)
        second = await running.steer_exact("Second correction.", expected_turn_id=turn_id)
        assert first.turn_id == second.turn_id == turn_id
        assert first.input_id != second.input_id
        await running.cancel()
        outcome = await running.wait_for_attention()
        assert isinstance(outcome, TurnNeedsAttention) and outcome.reason == "interrupted"
        ticks = heartbeat.read_bytes()
        background_ticks = background.read_bytes()
        assert ticks
        await asyncio.sleep(0.25)
        assert heartbeat.read_bytes() == ticks
        assert len(background.read_bytes()) > len(background_ticks)
        assert len(responses.requests) == 2
        refused = await session.submit("Change write roots.", writable_roots=[sibling.resolve()])
        with pytest.raises(NativeError, match="old executor work") as failure:
            await refused.get_result()
        assert failure.value.effects == "not_started"
        assert len(responses.requests) == 2
        responses.respond(assistant_output("New work."))
        successor = await session.submit("Start fresh.")
        with pytest.raises(StaleTurnError):
            await running.steer_exact("Stale correction.", expected_turn_id=turn_id)
        assert (await successor.get_result()).final_output == "New work."
        assert len(responses.requests) == 3


def _command_is_running(cmdline: Path, name: str) -> bool:
    try:
        return cmdline.read_bytes().split(b"\0", 1)[0] == name.encode()
    except (FileNotFoundError, ProcessLookupError):
        return False


async def _owned_command(name: str) -> Path:
    # Inspect only descendants of this test process. A sandbox wrapper's
    # initial yield can precede the final exec into the tagged command.
    async with timeout(8):
        while True:
            pending = [os.getpid()]
            while pending:
                process = Path("/proc") / str(pending.pop())
                cmdline = process / "cmdline"
                if _command_is_running(cmdline, name):
                    return cmdline
                for children in process.glob("task/*/children"):
                    try:
                        pending.extend(map(int, children.read_text().split()))
                    except (FileNotFoundError, ProcessLookupError):
                        continue
            await asyncio.sleep(0.02)


@pytest.mark.requires_native_linux_sandbox
async def test_cancel_joins_local_process_before_its_result_is_accepted(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    handles: dict[str, int] = {}

    async def transform(ctx: ExecutorContext[None], result: dict[str, object]) -> dict[str, object]:
        assert ctx.call_id is not None
        handle = result["session_id"]
        assert isinstance(handle, int)
        handles[ctx.call_id] = handle
        if ctx.call_id == "held":
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        return result

    commands = {call_id: "agents-idle-" + uuid4().hex for call_id in ("returned", "held")}
    for call_id, command in commands.items():
        script = shlex.quote(f"exec -a {command} /usr/bin/sleep 60")
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": f"/bin/bash -c {script}", "yield_time_ms": 250, "login": False},
                call_id,
            )
        )
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    environment = Environment.local(
        cwd=tmp_path, tools=["exec_command"], transform_result=transform
    )
    async with (
        timeout(40),
        provider,
        Agent(instructions="Use the selected tools.", provider=provider).session(
            environment=environment
        ) as session,
    ):
        turn = await session.submit("Start two idle commands.")
        stopping = None
        try:
            await entered.wait()
            returned = await _owned_command(commands["returned"])
            held = await _owned_command(commands["held"])
            assert (
                json.loads(tool_output(responses.requests[1], "returned"))["session_id"]
                == (handles["returned"])
            )
            assert handles["held"] != handles["returned"]
            stopping = asyncio.create_task(turn.cancel())
            await cancelled.wait()
            assert not stopping.done()
        finally:
            release.set()
            if stopping is not None:
                await stopping
        assert len(responses.requests) == 2
        assert not _command_is_running(held, commands["held"])
        assert _command_is_running(returned, commands["returned"])


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
@pytest.mark.requires_native_macos_sandbox
@pytest.mark.parametrize("write_policy", ["strict", "trusted_workspace_write"])
async def test_macos_session_close_stops_child_after_command_exit(
    responses: ResponsesFixture,
    tmp_path: Path,
    write_policy: Literal["strict", "trusted_workspace_write"],
) -> None:
    cwd, extra = tmp_path / "cwd", tmp_path / "extra"
    cwd.mkdir()
    extra.mkdir()
    ready, release, witness = cwd / "ready", cwd / "release", extra / "witness"
    detach = cwd / f"detach-{uuid4().hex}"
    subprocess.run(
        [
            "/usr/bin/cc",
            str(Path(__file__).with_name("fixtures") / "detached_child.c"),
            "-o",
            str(detach),
        ],
        check=True,
    )

    def child_running(pid: int) -> bool:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return False
        result.check_returncode()
        state, command = result.stdout.split(maxsplit=1)
        return not state.startswith("Z") and detach.name in command

    cmd = "exec " + shlex.join([str(detach), str(release), str(witness), str(ready)])
    responses.respond(function_call("exec_command", {"cmd": cmd, "login": False}, "detach"))
    responses.respond(assistant_output("parent exited"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    environment = Environment.local(
        cwd=cwd, writable_roots=[extra], tools=["exec_command"], write_policy=write_policy
    )
    # The fixture's 30-second self-exit must never satisfy this test.
    async with timeout(25), provider:
        try:
            async with agent.session(environment=environment) as session:
                turn = await session.submit("Start the child.")
                assert (await turn.get_result()).final_output == "parent exited"
                assert "Process exited with code 0" in tool_output(responses.requests[1], "detach")
                pid, detach_error = map(int, ready.read_text().split())
                assert not witness.exists()
            alive_at_close = child_running(pid)
            release.touch()
            async with timeout(5):
                while child_running(pid):
                    await asyncio.sleep(0.05)
            assert not witness.exists(), (
                f"child wrote after Session close (setsid errno {detach_error})"
            )
            assert not alive_at_close, f"child survived Session close (setsid errno {detach_error})"
        finally:
            release.touch()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt and launchd")
@pytest.mark.requires_native_macos_sandbox
async def test_macos_local_command_cannot_delegate_execution_to_launchd(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "launchd-marker"

    def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True, timeout=5, check=False
        )

    uid = os.getuid()
    domain = f"gui/{uid}"
    if launchctl("print", domain).returncode:
        domain = f"user/{uid}"

    label = f"org.openai.agents.sdk-test.{uuid4().hex}"
    service = f"{domain}/{label}"
    plist = workspace / f"{label}.plist"
    job = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/touch", str(marker)],
        "RunAtLoad": False,
        "KeepAlive": False,
        "ExitTimeOut": 2,
        "ThrottleInterval": 1,
    }
    if domain.startswith("user/"):
        job["LimitLoadToSessionType"] = "Background"
    plist.write_bytes(plistlib.dumps(job))

    def runs() -> int:
        status = launchctl("print", service)
        assert status.returncode == 0, status.stderr
        count = re.search(r"^\s*runs = (\d+)", status.stdout, re.MULTILINE)
        assert count is not None, status.stdout
        return int(count[1])

    for operation, args in (
        ("bootstrap", (domain, str(plist))),
        ("kickstart", (service,)),
    ):
        cmd = (
            "rc=0; "
            + shlex.join(["/bin/launchctl", operation, *args])
            + f" || rc=$?; printf '{operation} exit %s\\n' \"$rc\""
        )
        responses.respond(
            function_call(
                "exec_command", {"cmd": cmd, "yield_time_ms": 10000, "login": False}, operation
            )
        )
        responses.respond(assistant_output("checked"))

    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    environment = Environment.local(cwd=workspace, tools=["exec_command"])
    try:
        assert launchctl("print", service).returncode != 0
        assert not marker.exists()
        async with timeout(35), provider, agent.session(environment=environment) as session:
            turn = await session.submit("Try to register the test job.")
            assert (await turn.get_result()).final_output == "checked"
            output = tool_output(responses.requests[1], "bootstrap")
            assert re.search(r"^bootstrap exit [1-9]\d*$", output, re.MULTILINE), output
            assert launchctl("print", service).returncode != 0
            assert not marker.exists()

            bootstrapped = launchctl("bootstrap", domain, str(plist))
            assert bootstrapped.returncode == 0, bootstrapped.stderr
            assert runs() == 0

            turn = await session.submit("Try to start the idle test job.")
            assert (await turn.get_result()).final_output == "checked"
            output = tool_output(responses.requests[3], "kickstart")
            assert re.search(r"^kickstart exit [1-9]\d*$", output, re.MULTILINE), output
            assert runs() == 0 and not marker.exists(), (
                "sandboxed kickstart ran the registered launchd job"
            )

        kicked = launchctl("kickstart", service)
        assert kicked.returncode == 0, kicked.stderr
        async with timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.05)
        assert runs() == 1
    finally:
        stopped = launchctl("bootout", service)
        if stopped.returncode:
            assert launchctl("print", service).returncode != 0, stopped.stderr


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.parametrize("login", [None, False], ids=["omitted", "false"])
async def test_local_bash_uses_non_login_shell(
    responses: ResponsesFixture, tmp_path: Path, login: bool | None
) -> None:
    marker = tmp_path / "login-mode.txt"
    arguments: dict[str, str | bool] = {
        "cmd": (
            'set -eu; test -n "$BASH_VERSION"; '
            "if shopt -q login_shell; then exit 17; fi; "
            "printf 'once\\n' >> login-mode.txt; printf 'NONLOGIN\\n'"
        ),
        "shell": "/bin/bash",
    }
    if login is not None:
        arguments["login"] = login
    responses.respond(function_call("exec_command", arguments, "login-mode"))
    responses.respond(assistant_output("done"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    async with (
        timeout(30),
        provider,
        agent.session(
            environment=Environment.local(cwd=tmp_path, tools=["exec_command"])
        ) as session,
    ):
        assert (
            await (await session.submit("Inspect Bash login mode.")).get_result()
        ).final_output == "done"
    assert len(responses.requests) == 2
    output = tool_output(responses.requests[1], "login-mode")
    assert "Process exited with code 0" in output
    assert re.findall(r"(?m)^NONLOGIN$", output) == ["NONLOGIN"]
    assert marker.read_text() == "once\n"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_login_shell_rejection_has_no_command_effect(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    forbidden = tmp_path / "forbidden-login"
    healthy = tmp_path / "healthy-command"
    responses.respond(
        function_call(
            "exec_command",
            {"cmd": "printf forbidden > forbidden-login", "shell": "/bin/bash", "login": True},
            "forbidden-login",
        )
    )
    responses.respond(assistant_output("rejected"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    async with (
        timeout(30),
        provider,
        agent.session(
            environment=Environment.local(cwd=tmp_path, tools=["exec_command"])
        ) as session,
    ):
        rejected = await session.submit("Request a login shell.")
        assert (await rejected.get_result()).final_output == "rejected"
        assert len(responses.requests) == 2
        assert "login shell is disabled by config; omit `login` or set it to false." in tool_output(
            responses.requests[1], "forbidden-login"
        )
        assert not forbidden.exists()
        responses.respond(
            function_call(
                "exec_command",
                {"cmd": "printf healthy > healthy-command", "shell": "/bin/bash", "login": False},
                "healthy-command",
            )
        )
        responses.respond(assistant_output("healthy"))
        resumed = await session.submit("Run an ordinary command in the same Session.")
        assert (await resumed.get_result()).final_output == "healthy"
    assert len(responses.requests) == 4
    assert "Process exited with code 0" in tool_output(responses.requests[3], "healthy-command")
    assert healthy.read_text() == "healthy"
    assert not forbidden.exists()

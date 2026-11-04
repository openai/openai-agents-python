"""Direct local execution through the public Session facade."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from agents import (
    Agent,
    Environment,
    LocalStorage,
    NativeError,
    NativeProvider,
    NetworkPolicy,
    StaleTurnError,
    StdioMCP,
    TurnNeedsAttention,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


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
    server = StdioMCP(
        name="local-tools",
        command="/bin/echo",
        tools=["echo"],
        location="environment",
        env_vars=["OPENAI_API_KEY", "CODEX_API_KEY", "DECLARED_VAR"],
    )
    declaration = server._config(local)
    assert declaration["environment_id"] == "local"
    assert declaration["cwd"] == "/job/worktree"
    assert declaration["env_vars"] == ["DECLARED_VAR"]


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
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_shell_patch_and_linked_worktree_respect_explicit_roots(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_executable = shutil.which("git")
    assert git_executable is not None
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-local-model-key")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-local-codex-key")

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
    git_path = shlex.quote(git_executable)
    command = (
        'set -e; test -z "${OPENAI_API_KEY+x}${CODEX_API_KEY+x}"; '
        f"printf cached > {shlex.quote(str(cache / 'marker'))}; "
        "if (printf denied > escape) 2>/dev/null; then exit 11; fi; "
        "if (printf denied > .agents/protected) 2>/dev/null; then exit 12; fi; "
        "if (printf denied >> .git) 2>/dev/null; then exit 13; fi; "
        f"{git_path} add result.txt; "
        f"{git_path} -c user.name=Fixture -c user.email=fixture@example.invalid "
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
    assert (linked / ".agents" / "protected").read_text() == "metadata"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_write_root_replacement_survives_portable_resume(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    cwd, cache, sibling = (tmp_path / name for name in ("cwd", "cache", "sibling"))
    for directory in (cwd, cache, sibling):
        directory.mkdir()
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    selected = Environment.local(cwd=cwd, writable_roots=[cache], tools=["exec_command"])
    (cache / "marker").write_text("initial")
    cache_file = shlex.quote(str(cache / "marker"))
    sibling_file = shlex.quote(str(sibling / "marker"))
    revised = (
        f"if (printf denied > {cache_file}) 2>/dev/null; then exit 11; fi; "
        f"printf expanded >> {sibling_file}"
    )
    responses.respond(assistant_output("replace"))
    responses.respond(function_call("exec_command", {"cmd": revised, "login": False}, "resume"))
    responses.respond(assistant_output("resume"))

    async with timeout(40), provider:
        async with agent.session(environment=selected) as session:
            updated = await session.submit("Change the write policy.", writable_roots=[sibling])
            assert (await updated.get_result()).final_output == "replace"
            saved = await session.save_state()
        with pytest.raises(ValueError, match="agree on the applied executor policy"):
            async with agent.session(environment=selected, state=saved):
                pytest.fail("Portable resume accepted superseded grants")
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
async def test_turn_replaces_additional_roots_and_reopen_checks_the_applied_policy(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    work, first, second, third = (tmp_path / name for name in ("work", "first", "second", "third"))
    for directory in (work, first, second, third):
        directory.mkdir()
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    storage = LocalStorage(tmp_path / "profile")
    network = NetworkPolicy(
        allowed_domains=["allowed.example.invalid"],
        denied_domains=["blocked.example.invalid"],
        allow_all_unix_sockets=True,
    )
    selected = Environment.local(
        cwd=work, writable_roots=[first, second], tools=["exec_command"], network=network
    )
    denials = {
        "blocked.example.invalid": (
            "domain is explicitly denied by policy and cannot be approved from this prompt"
        ),
        "8.8.8.8": "domain is not on the allowlist for the current sandbox mode",
    }

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
        # Core reports a blocked request as a tool failure, so probe each host separately.
        for host in denials:
            probe = (
                'test -n "$HTTP_PROXY" && curl --silent --show-error --max-time 3 '
                '--proxy "$HTTP_PROXY" '
                f"--noproxy '' --output /dev/null http://{host}/"
            )
            responses.respond(
                function_call("exec_command", {"cmd": probe, "login": False}, f"{call_id}-{host}")
            )
        responses.respond(assistant_output(call_id))
        turn = await session.submit(call_id, **settings)
        assert (await turn.get_result()).final_output == call_id
        assert "Process exited with code 0" in tool_output(responses.requests[start + 1], call_id)
        for request, (host, reason) in zip(
            responses.requests[start + 2 :], denials.items(), strict=True
        ):
            output = tool_output(request, f"{call_id}-{host}")
            assert output.startswith("exec_command failed: ProcessFailed")
            assert host in output and f"was blocked: {reason}" in output
        return turn.turn_id

    async with timeout(60), provider:
        async with agent.session(storage=storage, environment=selected) as session:
            session_id = session.id
            before = await run_turn(
                session, "original", command(allow=(work, first, second), deny=(third,))
            )
            bad = await session.submit("Invalid policy", writable_roots=[tmp_path / "missing"])
            with pytest.raises(NativeError, match="resolve an executor write root") as failure:
                await bad.get_result()
            assert failure.value.effects == "not_started"
            after = await run_turn(
                session,
                "replacement",
                command(allow=(work, third), deny=(first, second)),
                writable_roots=[third],
            )
            inherited = await run_turn(
                session, "inherited", command(allow=(work, third), deny=(first, second))
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
                path.resolve().as_uri() for path in (third, work)
            }
            assert receipts[inherited] == receipts[after]
            assert "permission_profile" not in receipts[after]
            assert receipts[before]["network_sandbox"] == receipts[after]["network_sandbox"]
            assert receipts[before]["network_policy"] == receipts[after]["network_policy"]
            assert receipts[after]["network_policy"]["domains"] == {
                "allowed.example.invalid": "allow",
                "blocked.example.invalid": "deny",
            }

        with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
            async with agent.session(storage=storage, resume=session_id, environment=selected):
                pytest.fail("Reopen accepted superseded grants")
        replacement = Environment.local(
            cwd=work, writable_roots=[third], tools=["exec_command"], network=network
        )
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


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_steer_twice_and_interrupt_running_local_command(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    heartbeat = work / "heartbeat"
    selected = Environment.local(cwd=work, tools=["exec_command"])
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
        timeout(30),
        provider,
        Agent(instructions="Use the selected tools.", provider=provider).session(
            environment=selected
        ) as session,
    ):
        running = await session.submit("Start the command.")
        while not heartbeat.exists():
            await asyncio.sleep(0.01)
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
        assert ticks
        await asyncio.sleep(0.25)
        assert heartbeat.read_bytes() == ticks
        assert len(responses.requests) == 1
        responses.respond(assistant_output("New work."))
        successor = await session.submit("Start fresh.")
        with pytest.raises(StaleTurnError):
            await running.steer_exact("Stale correction.", expected_turn_id=turn_id)
        assert (await successor.get_result()).final_output == "New work."
        assert len(responses.requests) == 2

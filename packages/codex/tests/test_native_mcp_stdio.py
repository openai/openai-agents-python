"""The public declaration reaches the actual native stdio process owner."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents import (
    Agent,
    Environment,
    FunctionTool,
    HttpMCP,
    MCPResultContext,
    NativeError,
    StdioMCP,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import Peer, declarations, mcp_call

SERVER = Path(__file__).parent / "fixtures/native_stdio_mcp.py"


def stdio(log: Path, *args: str, **kwargs: Any) -> StdioMCP[Any]:
    return StdioMCP(
        name="local",
        command=sys.executable,
        args=("-I", "-B", str(SERVER), "--log", str(log), *args),
        tools=("search",),
        **kwargs,
    )


def records(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    # The final write may still be in flight while this process polls the peer.
    process_logs = [log] if log.is_file() else sorted(log.glob("*.jsonl"))
    return [
        json.loads(line)
        for process_log in process_logs
        for line in process_log.read_text().splitlines(keepends=True)
        if line.endswith("\n") and line.strip()
    ]


async def wait_for_call(log: Path) -> None:
    while not any(item.get("message", {}).get("method") == "tools/call" for item in records(log)):
        await asyncio.sleep(0.01)


async def assert_stopped(log: Path) -> None:
    starts = [item for item in records(log) if item["event"] == "start"]
    assert starts, "native client did not start the stdio peer"
    for item in starts:
        if os.name == "nt":
            # os.kill(pid, 0) terminates on Windows. Query the process handle
            # without granting termination access and wait for actual exit.
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x00100000, False, item["pid"])  # SYNCHRONIZE
            if not handle:
                error = ctypes.get_last_error()
                if error == 87:  # ERROR_INVALID_PARAMETER: the process no longer exists.
                    continue
                raise ctypes.WinError(error)
            try:
                while True:
                    status = kernel.WaitForSingleObject(handle, 0)
                    if status == 0:  # WAIT_OBJECT_0
                        break
                    if status != 258:  # WAIT_TIMEOUT
                        raise ctypes.WinError()
                    await asyncio.sleep(0.01)
            finally:
                if not kernel.CloseHandle(handle):
                    raise ctypes.WinError()
            continue
        while True:
            try:
                os.kill(item["pid"], 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)


async def test_native_stdio_selection_policy_and_http_python_composition(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "stdio.jsonl"
    monkeypatch.setenv("N19_STDIO_CURRENT", "current-host-value")
    decisions: list[tuple[str, str, str]] = []

    def policy(context: MCPResultContext[None], result: dict[str, Any]) -> dict[str, Any]:
        decisions.append((context.server_name, context.tool_name, context.call_id))
        assert result["structuredContent"] == {"receipt": "private-stdio-result"}
        return {"content": [{"type": "text", "text": "public-stdio-result"}], "isError": False}

    @function_tool
    def local_python(key: str) -> str:
        return f"python-{key}"

    peer = Peer()
    async with timeout(30), peer.serve() as url:
        descriptor = stdio(
            log,
            env_vars=["N19_STDIO_CURRENT"],
            cwd=str(tmp_path),
            transform_result=policy,
        )
        responses.respond(mcp_call("search", "mcp__local", "stdio-call"))
        responses.respond(mcp_call("search", "mcp__remote", "http-call"))
        responses.respond(function_call("local_python", {"key": "43"}, "python-call"))
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use the selected tools.",
            tools=[descriptor, HttpMCP(name="remote", url=url, tools=["search"]), local_python],
            provider=provider(responses),
        ).session() as session:
            result = await (await session.submit("read")).get_result()
            assert result.final_output == "done"
            offered = declarations(responses.requests[0])
            assert ("mcp__local", "search") in offered
            assert ("mcp__local", "excluded") not in offered
            assert ("mcp__remote", "search") in offered
            assert ("functions", "local_python") in offered
            assert set(offered) == {
                ("mcp__local", "search"),
                ("mcp__remote", "search"),
                ("functions", "local_python"),
            }
            history = [item async for item in session.items()]
            state = await session.save_state()
            assert state.agent_options == {}
            assert "private-stdio-result" not in json.dumps(history)
            assert "public-stdio-result" in json.dumps(history)
        await assert_stopped(log)
    assert decisions == [("local", "search", "stdio-call")]
    assert peer.calls[0]["_meta"]["callId"] == "http-call"
    launch = next(item for item in records(log) if item["event"] == "start")
    assert launch["cwd"] == str(tmp_path)
    assert launch["selected_env"] == "current-host-value"
    calls = [
        item["message"]["params"]
        for item in records(log)
        if item.get("message", {}).get("method") == "tools/call"
    ]
    assert len(calls) == 1
    assert calls[0]["_meta"]["callId"] == "stdio-call"
    assert "private-stdio-result" not in json.dumps(responses.requests)


async def test_stdio_launch_resources_are_pinned_until_current_authority_restores(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-api-key")
    first_log, current_log = tmp_path / "first.jsonl", tmp_path / "current.jsonl"
    first = stdio(
        first_log,
        "--observe-env",
        "B04_LITERAL",
        env_vars=["N19_STDIO_CURRENT"],
        env={"B04_LITERAL": "old-captured"},
    )
    current_values = {"B04_LITERAL": "current-captured"}
    current = stdio(
        current_log,
        "--field",
        "revision",
        "--observe-env",
        "B04_LITERAL",
        env_vars=["N19_STDIO_CURRENT"],
        env=current_values,
    )
    current_values["B04_LITERAL"] = "mutated-after-capture"
    monkeypatch.setenv("N19_STDIO_CURRENT", "before-open")
    async with timeout(30):
        async with Agent(
            instructions="Read.", tools=[first], provider=provider(responses)
        ).session() as session:
            responses.respond(assistant_output("first"))
            await (await session.submit("first")).get_result()
            assert (
                "key"
                in declarations(responses.requests[-1])[("mcp__local", "search")]["parameters"][
                    "properties"
                ]
            )
            monkeypatch.setenv("N19_STDIO_CURRENT", "at-replacement")
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[current])
            assert not current_log.exists()
            responses.respond(mcp_call("search", "mcp__local", "original-after-rejection"))
            responses.respond(assistant_output("original"))
            await (await session.submit("original")).get_result()
            state = await session.save_state()
            assert state.agent_options == {}
            assert "old-captured" not in json.dumps(state.model_dump(mode="json"))
        await assert_stopped(first_log)
        assert not current_log.exists()
        monkeypatch.setenv("N19_STDIO_CURRENT", "at-restore")
        poisoned = state.model_copy(
            update={
                "agent_options": {
                    "config": {
                        "mcp_servers": {
                            "old": {
                                **first._config(),
                                "env": {"B04_LITERAL": "poisoned-saved-config"},
                            }
                        }
                    }
                }
            }
        )
        async with Agent(
            instructions="Read.", tools=[current], provider=provider(responses)
        ).session(state=poisoned) as restored:
            event = function_call("search", {"revision": "2"}, "current-call")
            event["item"]["namespace"] = "mcp__local"
            responses.respond(event)
            responses.respond(assistant_output("restored"))
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "restored"
            schema = declarations(responses.requests[-2])[("mcp__local", "search")]["parameters"]
            assert "revision" in schema["properties"] and "key" not in schema["properties"]
            assert (await restored.save_state()).agent_options == {}
            assert "current-captured" not in (await restored.save_state()).to_json()
        await assert_stopped(current_log)
    assert [item["selected_env"] for item in records(first_log) if item["event"] == "start"] == [
        "before-open"
    ]
    assert sorted(
        item["selected_env"] for item in records(current_log) if item["event"] == "start"
    ) == ["at-restore"]
    assert {
        item["observed_env"]["B04_LITERAL"]
        for item in records(first_log)
        if item["event"] == "start"
    } == {"old-captured"}
    assert {
        item["observed_env"]["B04_LITERAL"]
        for item in records(current_log)
        if item["event"] == "start"
    } == {"current-captured"}
    assert all("poisoned-saved-config" not in json.dumps(item) for item in records(current_log))
    for secret in ("old-captured", "current-captured", "poisoned-saved-config"):
        assert secret not in json.dumps(responses.requests)


async def test_native_stdio_reconnect_does_not_replay_uncertain_call(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    log = tmp_path / "reconnect"
    uncertain, later = "exit-before-reply", "after-reconnect"
    receipt = "recovered-stdio-result"
    descriptor = stdio(
        log,
        "--exit-after-key",
        uncertain,
        "--label",
        receipt,
        "--writable",
        approval_mode="preapproved",
    )
    for key in (uncertain, later):
        event = function_call("search", {"key": key}, key)
        event["item"]["namespace"] = "mcp__local"
        responses.respond(event)
    responses.respond(assistant_output("done"))

    async with timeout(30):
        async with Agent(
            instructions="Use the selected tool.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            # Both calls run in one turn, with no new Session or tool selection between them.
            turn = await session.submit("Run the two marked calls in sequence.")
            assert (await turn.get_result()).final_output == "done"
            assert len(responses.requests) == 3
            error = tool_output(responses.requests[1], uncertain)
            assert "Transport closed" in error, error
            output = tool_output(responses.requests[2], later)

            peer_records = records(log)
            effects = [(row["key"], row["pid"]) for row in peer_records if row["event"] == "effect"]
            assert sorted(key for key, _ in effects) == sorted((uncertain, later)), output
            assert receipt in output, output
            effect_pids = {pid for _, pid in effects}
            assert len(effect_pids) == 2
            starts = [row["pid"] for row in peer_records if row["event"] == "start"]
            assert sorted(starts) == sorted(effect_pids)
        await assert_stopped(log)


@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_pending_stdio_call_settles_and_native_process_closes(
    responses: ResponsesFixture, tmp_path: Path, ending: str
) -> None:
    log = tmp_path / "pending.jsonl"
    responses.respond(mcp_call("search", "mcp__local", "pending-call"))
    async with timeout(30):
        async with Agent(
            instructions="Read.", tools=[stdio(log, "--hold")], provider=provider(responses)
        ).session() as session:
            handle = await session.submit("wait")
            await wait_for_call(log)
            await (handle.cancel() if ending == "cancel" else session._aclose())
            with pytest.raises(TurnStoppedError):
                await (handle).get_result()
            assert len(responses.requests) == 1
        await assert_stopped(log)


@pytest.mark.parametrize("ending", ["released", "persistent", "other-error", "other-platform"])
async def test_stdio_home_removal_retires_lease_only_after_directory_is_gone(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    sdk_session = importlib.import_module("_agents_codex.session")
    current_provider = provider(responses)
    log = tmp_path / "home-retirement.jsonl"
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0
    first = RuntimeError(
        "The process cannot access the file (os error 5)"
        if ending == "other-error"
        else "The process cannot access the file (os error 32)"
    )
    async with (
        timeout(15),
        Agent(
            instructions="Read.", tools=[stdio(log)], provider=current_provider
        ).session() as session,
    ):
        assert not records(log) and not responses.requests
        responses.respond(assistant_output("ready"))
        selected = await session.submit("Select opening defaults")
        assert (await selected.get_result()).final_output == "ready"
        assert records(log)
        native_home = session._home
        path = Path(native_home.name)

        class LockedHome:
            name = str(path)

            def cleanup(self) -> None:
                nonlocal attempts
                attempts += 1
                if not release.is_set():
                    entered.set()
                    if attempts == 1:
                        raise first
                    raise RuntimeError("still locked (os error 32)")
                native_home.cleanup()

        monkeypatch.setattr(session, "_home", LockedHome())
        try:
            # Fake only the locked phase. Real cleanup must use the host's real
            # platform (especially on Windows) and its ordinary cleanup deadline.
            with monkeypatch.context() as locked:
                locked.setattr(
                    sdk_session,
                    "sys",
                    SimpleNamespace(platform="linux" if ending == "other-platform" else "win32"),
                )
                if ending != "released":
                    locked.setattr(sdk_session, "_NATIVE_HOME_CLOSE_TIMEOUT", 0.05)
                if ending == "released":
                    closing = asyncio.create_task(session._aclose())
                    await entered.wait()
                    assert path.exists() and not closing.done()
                    with pytest.raises(RuntimeError, match="open or opening"):
                        await current_provider.aclose()
                else:
                    with pytest.raises(RuntimeError) as caught:
                        await session._aclose()
                    assert caught.value is first
                    assert path.exists() and not session._closed
                    with pytest.raises(RuntimeError, match="open or opening"):
                        await current_provider.aclose()
                    assert attempts >= 2 if ending == "persistent" else attempts == 1
            release.set()
            if ending == "released":
                await closing
                assert attempts >= 2
            else:
                await session._aclose()
            assert session._closed
            assert not path.exists()
        finally:
            release.set()
            monkeypatch.setattr(session, "_home", native_home)
    await assert_stopped(log)
    await current_provider.aclose()


@pytest.mark.parametrize("persistent", [False, True])
async def test_stdio_opening_failure_retains_orphan_until_real_cleanup(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    sdk_session = importlib.import_module("_agents_codex.session")
    native = importlib.import_module("agents._native")
    current_provider = provider(responses)
    log = tmp_path / "startup-retirement.jsonl"
    homes: list[Path] = []
    real_home = native.NativeProviderLease.home
    real_close = native.NativeProviderLease.close_orphan
    first = RuntimeError("native private home locked (os error 32)")
    attempts = 0

    def capture_home(lease):
        home = real_home(lease)
        homes.append(Path(home.name))
        return home

    async def delayed_close(lease):
        nonlocal attempts
        attempts += 1
        if persistent or attempts < 3:
            if attempts == 1:
                raise first
            raise RuntimeError("still locked (os error 32)")
        await real_close(lease)

    async with timeout(15):
        manual = FunctionTool(
            name="lookup",
            description="Wait for an app reply",
            input_type=LookupArgs,
            output_type=str,
        )
        source_log = tmp_path / "source-retirement.jsonl"
        agent = Agent(instructions="Read.", provider=current_provider)
        async with agent.session(tools=[manual, stdio(source_log)]) as source:
            assert not records(source_log)
            responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
            await source.submit("Wait with both selected")
            await next_request(source.interactions(), manual)
            saved = await source.suspend()
        await assert_stopped(source_log)
        request_count = len(responses.requests)
        with monkeypatch.context() as opening:
            opening.setattr(native.NativeProviderLease, "home", capture_home)
            opening.setattr(native.NativeProviderLease, "close_orphan", delayed_close)
            opening.setattr(sdk_session, "sys", SimpleNamespace(platform="win32"))
            if persistent:
                opening.setattr(sdk_session, "_NATIVE_HOME_CLOSE_TIMEOUT", 0.05)
            with pytest.raises(NativeError, match="selected tools unavailable") as caught:
                # A saved live grant requires current selected MCP readiness
                # before Session admission, even though fresh opening is cold.
                async with agent.session(state=saved, tools=[manual, stdio(log, "--missing-tool")]):
                    pytest.fail("Restore exposed a pending grant before current readiness")
        assert homes and attempts >= 2
        assert len(responses.requests) == request_count
        if persistent:
            assert homes[0].exists()
            current: BaseException | None = caught.value
            while current is not None and current is not first:
                current = current.__context__
            assert current is first
        else:
            assert not homes[0].exists()
        # The Provider owns any orphan whose deadline expired. Actual native
        # provider close (including the orphan's native close) must retire it.
        await current_provider.aclose()
        assert not homes[0].exists()
        await assert_stopped(log)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("--invalid-init", "Native MCP server 'local': connection not ready"),
        ("--missing-tool", "Native MCP server 'local': selected tools unavailable: 'search'"),
    ],
)
async def test_stdio_startup_failure_closes_native_process_before_executing_turn(
    responses: ResponsesFixture, tmp_path: Path, failure: str, message: str
) -> None:
    log = tmp_path / "failure.jsonl"
    async with timeout(30):
        async with Agent(
            instructions="Read.", tools=[stdio(log, failure)], provider=provider(responses)
        ).session() as session:
            assert not records(log) and not responses.requests
            selected = await session.submit("Select opening defaults")
            with pytest.raises(NativeError) as caught:
                await selected.get_result()
            await assert_stopped(log)
        assert str(caught.value) == message
        await assert_stopped(log)
    assert not responses.requests


async def test_stdio_collision_is_rejected_before_process_start(tmp_path: Path) -> None:
    log = tmp_path / "must-not-exist.jsonl"
    local = stdio(log)
    with pytest.raises(ValueError, match="colliding"):
        async with Agent(
            instructions="Read.",
            tools=[
                local,
                HttpMCP(name=local.name, url="https://example.invalid/mcp", tools=local.tools),
            ],
        ).session():
            pytest.fail("Collision reached native startup")
    assert not log.exists()


async def test_idle_transport_selection_retains_opening_resources(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    log = tmp_path / "replacement.jsonl"
    peer = Peer()
    async with timeout(30), peer.serve() as url:
        http = HttpMCP(name="local", url=url, tools=["search"])
        async with Agent(
            instructions="Read.", tools=[stdio(log)], provider=provider(responses)
        ).session() as session:
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[http])
            assert peer.requests == []
            responses.respond(mcp_call("search", "mcp__local", "stdio-after"))
            responses.respond(assistant_output("stdio"))
            assert (await (await session.submit("stdio")).get_result()).final_output == "stdio"
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[replace(stdio(log), tools=("excluded",))])
        await assert_stopped(log)
        assert peer.calls == []


async def test_http_only_session_rejects_new_stdio_capability_without_mutation(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    log = tmp_path / "must-not-start.jsonl"
    peer = Peer()
    async with timeout(30), peer.serve() as url:
        async with Agent(
            instructions="Read.",
            tools=[HttpMCP(name="local", url=url, tools=["search"])],
            provider=provider(responses),
        ).session() as session:
            with pytest.raises(ValueError, match="captured"):
                await session.submit("select", tools=[stdio(log)])
            assert not log.exists()
            responses.respond(mcp_call("search", "mcp__local", "http-after-rejection"))
            responses.respond(assistant_output("still usable"))
            assert (
                await (await session.submit("http")).get_result()
            ).final_output == "still usable"
        assert [call["_meta"]["callId"] for call in peer.calls] == ["http-after-rejection"]


async def test_http_ignores_unrelated_partial_noise_environment(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_EXEC_SERVER_NOISE_REGISTRY_URL", "https://example.invalid")
    monkeypatch.delenv("CODEX_EXEC_SERVER_NOISE_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN", raising=False)
    peer = Peer()
    async with timeout(30), peer.serve() as url:
        responses.respond(assistant_output("http remains available"))
        async with Agent(
            instructions="Read.",
            tools=[HttpMCP(name="docs", url=url, tools=["search"])],
            provider=provider(responses),
        ).session() as session:
            assert (
                await (await session.submit("read")).get_result()
            ).final_output == "http remains available"


@pytest.mark.parametrize(
    "changes",
    [
        {"command": None},
        {"url": "https://example.invalid/mcp"},
        {"command": ""},
        {"command": "bad\x00command"},
        {"command": "\ud800"},
        {"command": 3},
        {"args": "one argument"},
        {"args": [3]},
        {"args": ["bad\x00arg"]},
        {"args": ["\ud800"]},
        {"env_vars": "SECRET"},
        {"env_vars": ["BAD=NAME"]},
        {"env_vars": [3]},
        {"env": "PRIVATE_ENV"},
        {"env": []},
        {"env": {3: "private-literal"}},
        {"env": {"": "private-literal"}},
        {"env": {"BAD=NAME": "private-literal"}},
        {"env": {"bad\x00name": "private-literal"}},
        {"env": {"bad\ud800name": "private-literal"}},
        {"env": {"PRIVATE_ENV": 3}},
        {"env": {"PRIVATE_ENV": "private\x00literal"}},
        {"env": {"PRIVATE_ENV": "private\ud800literal"}},
        {"cwd": ""},
        {"cwd": 3},
        {"cwd": "bad\x00path"},
        {"command": None, "url": "https://example.invalid/mcp", "args": ["arg"]},
        {"command": None, "url": "https://example.invalid/mcp", "env_vars": ["VAR"]},
        {"command": None, "url": "https://example.invalid/mcp", "cwd": "/tmp"},
    ],
)
def test_invalid_stdio_declarations_fail_before_launch(changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        StdioMCP(**{"name": "local", "command": "server", "tools": ["search"], **changes})


def test_stdio_launch_declaration_captures_sequences_and_hides_launch_details() -> None:
    assert inspect.get_annotations(StdioMCP, eval_str=True)["env"] == Mapping[str, str] | None
    args, env_vars = ["private-argument", ""], ["PRIVATE_ENV"]
    env = {
        "PRIVATE_ENV": "private-literal",
        "EMPTY": "",
        "UNICODE": "café 🚀",
        "service.é": "literal-name",
    }
    descriptor = StdioMCP(
        name="local",
        command="private-command",
        args=args,
        env_vars=env_vars,
        env=env,
        cwd="private-cwd",
        tools=["search"],
    )
    args.append("later")
    env_vars.append("LATER")
    env["PRIVATE_ENV"] = "later-literal"
    assert descriptor.args == ("private-argument", "")
    assert descriptor.env_vars == ("PRIVATE_ENV",)
    expected = {
        "PRIVATE_ENV": "private-literal",
        "EMPTY": "",
        "UNICODE": "café 🚀",
        "service.é": "literal-name",
    }
    assert descriptor.env == expected
    captured: Any = descriptor.env
    with pytest.raises(TypeError):
        captured["PRIVATE_ENV"] = "mutated"
    config_env = descriptor._config()["env"]
    assert isinstance(config_env, dict)
    config_env["PRIVATE_ENV"] = "changed-config"
    assert replace(descriptor, tools=())._config()["env"] == expected
    for private in (
        "private-command",
        "private-argument",
        "PRIVATE_ENV",
        "private-literal",
        "private-cwd",
    ):
        assert private not in repr(descriptor)


B04_ENV_NAMES = (
    "B04_LITERAL",
    "B04_EMPTY",
    "B04_UNICODE",
    "service.é",
    "OPENAI_API_KEY",
    "NODE_REPL_AUTH_TOKEN",
    "B04_UNREQUESTED_SECRET",
)


def env_observation_args() -> tuple[str, ...]:
    return tuple(part for name in B04_ENV_NAMES for part in ("--observe-env", name))


def assert_literal_observation(log: Path, expected: str) -> None:
    starts = [item for item in records(log) if item["event"] == "start"]
    assert starts
    for item in starts:
        assert item["observed_env"] == {
            "B04_LITERAL": expected,
            "B04_EMPTY": "",
            "B04_UNICODE": "東京 café",
            "service.é": "non-identifier",
            "OPENAI_API_KEY": "synthetic-child-grant",
            "NODE_REPL_AUTH_TOKEN": None,
            "B04_UNREQUESTED_SECRET": None,
        }


def literal_values(label: str) -> dict[str, str]:
    return {
        "B04_LITERAL": label,
        "B04_EMPTY": "",
        "B04_UNICODE": "東京 café",
        "service.é": "non-identifier",
        "OPENAI_API_KEY": "synthetic-child-grant",
        "NODE_REPL_AUTH_TOKEN": "synthetic-protected-literal",
    }


async def test_stdio_literal_env_is_captured_per_concurrent_child(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-api-key")
    monkeypatch.setenv("B04_LITERAL", "parent-literal")
    monkeypatch.setenv("B04_UNREQUESTED_SECRET", "synthetic-unrequested-host-secret")
    monkeypatch.setenv("NODE_REPL_AUTH_TOKEN", "synthetic-protected-parent")
    parent = {name: os.environ.get(name) for name in B04_ENV_NAMES}

    async def run(label: str) -> None:
        log = tmp_path / label
        marker = "captured-" + label + "-private"
        supplied = literal_values(marker)
        descriptor = stdio(
            log,
            *env_observation_args(),
            env_vars=["B04_LITERAL", "NODE_REPL_AUTH_TOKEN"],
            env=supplied,
        )
        supplied["B04_LITERAL"] = "mutated"
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Read.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            assert (await (await session.submit(label)).get_result()).final_output == "done"
            assert "synthetic-child-grant" not in json.dumps(
                (await session.save_state()).model_dump(mode="json")
            )
            assert marker not in json.dumps((await session.save_state()).model_dump(mode="json"))
        await assert_stopped(log)
        assert_literal_observation(log, marker)
        for secret in (marker, "synthetic-child-grant", "synthetic-unrequested-host-secret"):
            assert secret not in json.dumps(responses.requests)

    async with timeout(40):
        await asyncio.gather(run("first-child"), run("second-child"))
    assert {name: os.environ.get(name) for name in B04_ENV_NAMES} == parent
    for secret in (
        "captured-first-child-private",
        "captured-second-child-private",
        "synthetic-child-grant",
        "synthetic-unrequested-host-secret",
    ):
        assert secret not in json.dumps(responses.requests)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_stdio_literal_env_reaches_local_environment_child(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-api-key")
    monkeypatch.setenv("NODE_REPL_AUTH_TOKEN", "synthetic-protected-parent")
    monkeypatch.setenv("B04_LITERAL", "parent-local")
    monkeypatch.setenv("B04_UNREQUESTED_SECRET", "synthetic-unrequested-host-secret")
    local_peer = tmp_path / "peer.py"
    shutil.copyfile(SERVER, local_peer)
    log = tmp_path / "local-child"
    descriptor = replace(
        stdio(
            log,
            *env_observation_args(),
            env_vars=["B04_LITERAL", "OPENAI_API_KEY", "NODE_REPL_AUTH_TOKEN"],
            env=literal_values("local-literal"),
            location="environment",
        ),
        args=("-I", "-B", str(local_peer), "--log", str(log), *env_observation_args()),
    )
    parent = {name: os.environ.get(name) for name in B04_ENV_NAMES}
    responses.respond(assistant_output("done"))
    async with timeout(40):
        async with Agent(
            instructions="Read.", tools=[descriptor], provider=provider(responses)
        ).session(environment=Environment.local(cwd=tmp_path, tools=[])) as session:
            assert (await (await session.submit("read")).get_result()).final_output == "done"
            for secret in ("local-literal", "synthetic-child-grant"):
                assert secret not in (await session.save_state()).to_json()
        await assert_stopped(log)
    assert_literal_observation(log, "local-literal")
    for secret in ("local-literal", "synthetic-child-grant", "synthetic-unrequested-host-secret"):
        assert secret not in json.dumps(responses.requests)
    assert {name: os.environ.get(name) for name in B04_ENV_NAMES} == parent


async def test_resource_only_stdio_captures_literal_env_without_model_insertion(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-api-key")
    log = tmp_path / "resource-only"
    descriptor = replace(
        stdio(log, "--resources", *env_observation_args(), env=literal_values("resource-literal")),
        tools=(),
    )
    async with timeout(30):
        async with Agent(
            instructions="Read.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            assert (await session.list_mcp_resources("local"))["resources"][0][
                "uri"
            ] == "fixture://environment"
            assert (await session.read_mcp_resource("local", "fixture://environment"))["contents"][
                0
            ]["text"] == "resource-only"
            assert (await session.save_state()).agent_options == {}
            for secret in ("resource-literal", "synthetic-child-grant"):
                assert secret not in (await session.save_state()).to_json()
        await assert_stopped(log)
    assert_literal_observation(log, "resource-literal")
    assert responses.requests == []

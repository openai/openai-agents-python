"""Project search uses the selected executor and returns bounded structured results."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from agents import (
    Agent,
    Environment,
    ExecutorContext,
    NativeError,
    StopTurn,
    ToolRef,
    TurnStoppedError,
    function_tool,
)
from agents.executor import ExecutorToolName
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_mcp_tools import declarations


def test_search_tools_do_not_require_command_execution(tmp_path: Path) -> None:
    environment = Environment.local(cwd=tmp_path, tools=["project_glob", "project_grep"])
    assert environment.tools == ("project_glob", "project_grep")
    assert Environment.local(cwd=tmp_path).tools == ()


async def run_search(
    responses: ResponsesFixture,
    environment: Environment,
    name: ExecutorToolName,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    request_index = len(responses.requests)
    responses.respond(function_call(name, arguments, "search"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(instructions="Search the configured project.", provider=provider(responses)).session(
            environment=environment
        ) as session,
    ):
        assert (await (await session.submit("Search.")).get_result()).final_output == "done"
    offered = set(declarations(responses.requests[request_index]))
    assert offered == {("functions", tool) for tool in environment.tools}
    raw = tool_output(responses.requests[request_index + 1], "search")
    assert len(raw.encode()) <= 64 * 1024
    return json.loads(raw)


local_search = (
    pytest.mark.requires_native_macos_sandbox
    if sys.platform == "darwin"
    else pytest.mark.requires_native_linux_sandbox
)


@local_search
async def test_nested_projects_and_line_context(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    nested = tmp_path / "projects" / "one"
    nested.mkdir(parents=True)
    (nested / "report.txt").write_text("before\nneedle\nafter\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("needle\n", encoding="utf-8")
    (nested / ".hidden.txt").write_text("hidden\n", encoding="utf-8")
    environment = Environment.local(cwd=tmp_path, tools=["project_glob", "project_grep"])
    glob = await run_search(
        responses, environment, "project_glob", {"pattern": "**/*.txt", "path": "projects"}
    )
    assert set(glob["matches"]) == {"projects/one/report.txt", "projects/one/.hidden.txt"}
    assert glob["truncated"] is False
    assert glob["errors"] == []
    grep = await run_search(
        responses,
        environment,
        "project_grep",
        {"pattern": "need(le)", "path": "projects", "context_lines": 1},
    )
    assert grep["matches"] == [
        {
            "path": "projects/one/report.txt",
            "line_number": 2,
            "line": "needle",
            "before": [{"line_number": 1, "line": "before"}],
            "after": [{"line_number": 3, "line": "after"}],
        }
    ]
    assert grep["truncated"] is False


@pytest.mark.parametrize(
    "name", [pytest.param(name, marks=local_search) for name in ("project_glob", "project_grep")]
)
async def test_complete_no_match_is_not_truncation(
    responses: ResponsesFixture, tmp_path: Path, name: ExecutorToolName
) -> None:
    (tmp_path / "file.txt").write_text("ordinary text\n")
    result = await run_search(
        responses, Environment.local(cwd=tmp_path, tools=[name]), name, {"pattern": "absent"}
    )
    assert result["matches"] == []
    assert result["truncated"] is False
    assert result["truncation_reasons"] == []
    assert result["errors"] == []


@local_search
async def test_result_limit_is_explicit(responses: ResponsesFixture, tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("needle\n" * 2000)
    result = await run_search(
        responses,
        Environment.local(cwd=tmp_path, tools=["project_grep"]),
        "project_grep",
        {"pattern": "needle", "max_results": 3},
    )
    assert len(result["matches"]) == 3
    assert result["truncated"] is True
    assert result["truncation_reasons"]


@local_search
async def test_symlinks_do_not_expand_search_root(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret\n")
    (root / "alias").symlink_to(outside, target_is_directory=True)
    (root / "alias.txt").symlink_to(outside / "secret.txt")
    environment = Environment.local(
        cwd=root, tools=["project_glob", "project_grep"], writable_roots=[outside]
    )
    for name in ("project_glob", "project_grep"):
        result = await run_search(
            responses,
            environment,
            name,
            {"pattern": "**/*" if name == "project_glob" else "secret"},
        )
        assert result["matches"] == []
        assert "outside secret" not in json.dumps(result)


@pytest.mark.requires_native_linux_sandbox
async def test_remote_search_reads_the_selected_executor(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        project = peer.root / "project"
        project.mkdir()
        (project / "remote.txt").write_text("remote needle\n")
        environment = Environment(
            url=peer.url,
            cwd=str(project),
            headers={"Authorization": "Bearer " + peer.token},
            tools=["project_grep"],
        )
        result = await run_search(responses, environment, "project_grep", {"pattern": "needle"})
        assert result["matches"][0]["path"] == "remote.txt"
        assert result["matches"][0]["line"] == "remote needle"
        assert peer.attempts > 0
        unreadable = project / "unreadable.txt"
        unreadable.write_text("small private file\n")
        unreadable.chmod(0)
        try:
            denied = await run_search(
                responses,
                environment,
                "project_grep",
                {"pattern": "private", "glob": "unreadable.txt"},
            )
        finally:
            unreadable.chmod(0o600)
        assert denied["matches"] == []
        assert denied["truncated"] is True
        assert denied["truncation_reasons"] == ["read_errors"]
        assert denied["errors"] == [{"path": "unreadable.txt", "code": "path_unavailable"}]


@pytest.mark.parametrize(
    "arguments,code",
    [
        pytest.param({"pattern": "["}, "invalid_pattern", marks=local_search),
        pytest.param({"pattern": "ok", "path": "../outside"}, "invalid_path", marks=local_search),
        pytest.param({"pattern": "ok", "path": "/tmp"}, "invalid_path", marks=local_search),
        pytest.param({"pattern": "ok", "path": "missing"}, "path_unavailable", marks=local_search),
        pytest.param({"pattern": "ok", "max_results": 0}, "invalid_argument", marks=local_search),
    ],
)
async def test_invalid_search_is_not_a_successful_no_match(
    responses: ResponsesFixture, tmp_path: Path, arguments: dict[str, Any], code: str
) -> None:
    result = await run_search(
        responses,
        Environment.local(cwd=tmp_path, tools=["project_grep"]),
        "project_grep",
        arguments,
    )
    assert result["matches"] == []
    assert code in [error["code"] for error in result["errors"]]


@local_search
async def test_generated_code_receives_structured_matches(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    (tmp_path / "report.txt").write_text("one\nneedle\n")
    responses.respond(
        code_call(
            'const result = await tools.project_grep({pattern: "needle"}); '
            'text(result.matches[0].path + ":" + result.matches[0].line_number);',
            "search-code",
        )
    )
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Search the project with the available tools.",
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session(environment=Environment.local(cwd=tmp_path, tools=["project_grep"])) as session,
    ):
        assert (await (await session.submit("Find needle.")).get_result()).final_output == "done"
    assert "report.txt:2" in code_output(responses.requests[1], "search-code")


@local_search
async def test_restore_uses_current_root_and_current_search_grants(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "first.txt").write_text("needle first\n")
    (second / "second.txt").write_text("needle second\n")
    agent = Agent(instructions="Search the project.", provider=provider(responses))
    async with timeout(30):
        async with agent.session(
            environment=Environment.local(cwd=first, tools=["project_grep"])
        ) as session:
            responses.respond(function_call("project_grep", {"pattern": "needle"}, "first"))
            responses.respond(assistant_output("saved"))
            await (await session.submit("Search.")).get_result()
            saved = await session.save_state()
            assert "_sdk_project_search" not in saved.to_json()
        async with agent.session(
            state=saved, environment=Environment.local(cwd=second, tools=["project_grep"])
        ) as restored:
            responses.respond(function_call("project_grep", {"pattern": "needle"}, "second"))
            responses.respond(assistant_output("restored"))
            await (await restored.submit("Search again.")).get_result()
            result = json.loads(tool_output(responses.requests[-1], "second"))
            assert [match["path"] for match in result["matches"]] == ["second.txt"]
        async with agent.session(state=saved, environment=Environment.local(cwd=second)) as revoked:
            responses.respond(function_call("project_grep", {"pattern": "needle"}, "revoked"))
            responses.respond(assistant_output("unavailable"))
            await (await revoked.submit("Search again.")).get_result()
            assert not declarations(responses.requests[-1])
            assert "needle second" not in tool_output(responses.requests[-1], "revoked")


@local_search
async def test_turn_selection_can_narrow_but_not_expand_search_grants(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    responses.respond(assistant_output("selected"))
    async with Agent(instructions="Search.", provider=provider(responses)).session(
        environment=Environment.local(cwd=tmp_path, tools=["project_glob", "project_grep"])
    ) as session:
        await (
            await session.submit("Find files.", tools=[ToolRef("project_glob", "functions")])
        ).get_result()
        assert set(declarations(responses.requests[-1])) == {("functions", "project_glob")}
        with pytest.raises(ValueError):
            await session.submit("Run a command.", tools=[ToolRef("exec_command", "functions")])
        assert len(responses.requests) == 1


@local_search
async def test_cancelling_search_joins_approval_and_preserves_next_turn(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    entered, settled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def approval(context: ExecutorContext[None], arguments: Any) -> bool:
        assert context.tool_name == "project_grep"
        assert arguments["pattern"] == "needle"
        entered.set()
        try:
            await release.wait()
            return True
        finally:
            settled.set()

    responses.respond(function_call("project_grep", {"pattern": "needle"}, "held-search"))
    environment = Environment.local(cwd=tmp_path, tools=["project_grep"], approval=approval)
    async with (
        timeout(20),
        Agent(instructions="Search when asked.", provider=provider(responses)).session(
            environment=environment
        ) as session,
    ):
        turn = await session.submit("Find needle.")
        await entered.wait()
        await turn.cancel()
        assert settled.is_set()
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert len(responses.requests) == 1
        responses.respond(assistant_output("next turn"))
        result = await (await session.submit("Continue without searching.")).get_result()
        assert result.final_output == "next turn"


@local_search
@pytest.mark.parametrize(
    "name,programmatic,stage",
    [
        ("project_glob", False, "approval"),
        ("project_grep", False, "arguments"),
        ("project_glob", True, "approval"),
        ("project_grep", True, "arguments"),
    ],
)
async def test_search_policy_stops_its_owner_and_joins_cleanup(
    responses: ResponsesFixture,
    tmp_path: Path,
    name: ExecutorToolName,
    programmatic: bool,
    stage: str,
) -> None:
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    decisions: list[ExecutorContext[None]] = []

    @function_tool
    async def held() -> str:
        entered.set()
        try:
            await release.wait()
            return "held result"
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    async def decide(context: ExecutorContext[None], arguments: Any) -> bool | StopTurn:
        assert context.tool_name == name
        assert arguments == {"pattern": "needle"}
        if programmatic:
            await entered.wait()
        decisions.append(context)
        return False if programmatic and stage == "approval" else StopTurn("private reason")

    environment = Environment.local(
        cwd=tmp_path,
        tools=[name],
        approval=decide if stage == "approval" else None,
        transform_arguments=decide if stage == "arguments" else None,
    )
    if programmatic:
        responses.respond(
            code_call(
                "const owned = tools.held(); "
                f'try {{ await tools.{name}({{pattern: "needle"}}); }} catch (error) {{}} '
                'text("search incorrectly continued"); await owned;',
                "policy-search",
            )
        )
    else:
        responses.respond(function_call(name, {"pattern": "needle"}, "policy-search"))
    async with (
        timeout(30),
        Agent(
            instructions="Search.",
            tools=[held] if programmatic else [],
            provider=provider(responses),
            programmatic_tool_calling=programmatic,
        ).session(environment=environment) as session,
    ):
        turn = await session.submit("Search now.")
        waiter = asyncio.create_task(turn.get_result())
        try:
            if programmatic:
                await cleaning.wait()
                assert not waiter.done()
                assert not cleaned.is_set()
                release.set()
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                await waiter
            assert len(decisions) == 1
            assert decisions[0].submission is turn
            assert len(responses.requests) == 1
            if programmatic:
                assert cleaned.is_set()
            items = [item async for item in session.items()]
            outputs = [item for item in items if item.get("type") == "function_call_output"]
            assert "search incorrectly continued" not in str(outputs)
            assert "private reason" not in str(items)
            responses.respond(assistant_output("next turn"))
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "next turn"
        finally:
            release.set()
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


@local_search
@pytest.mark.parametrize("before_policy", [False, True])
async def test_search_result_policy_reaches_generated_code_once(
    responses: ResponsesFixture, tmp_path: Path, before_policy: bool
) -> None:
    (tmp_path / "private.txt").write_text("private needle\n")
    calls: list[ExecutorContext[None]] = []
    stages: list[str] = []

    def approve(context: ExecutorContext[None], value: Any) -> bool:
        assert value == {"pattern": "original"}
        stages.append("approval")
        return True

    def arguments(context: ExecutorContext[None], value: Any) -> Any:
        assert value == {"pattern": "original"}
        stages.append("arguments")
        return {"pattern": "needle"}

    def redact(context: ExecutorContext[None], value: Any) -> Any:
        assert context.tool_name == "project_grep"
        assert "private needle" in json.dumps(value)
        calls.append(context)
        stages.append("result")
        return {"matches": [], "redacted": True}

    pattern = "original" if before_policy else "needle"
    responses.respond(
        code_call(
            f'const result = await tools.project_grep({{pattern: "{pattern}"}}); '
            "text(JSON.stringify(result));",
            "redacted-search",
        )
    )
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Search.",
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session(
            environment=Environment.local(
                cwd=tmp_path,
                tools=["project_grep"],
                approval=approve if before_policy else None,
                transform_arguments=arguments if before_policy else None,
                transform_result=redact,
            )
        ) as session,
    ):
        assert (await (await session.submit("Search.")).get_result()).final_output == "done"
        assert len(calls) == 1
        assert stages == (["approval", "arguments", "result"] if before_policy else ["result"])
        output = code_output(responses.requests[-1], "redacted-search")
        assert '"redacted":true' in output
        assert "private needle" not in json.dumps(responses.requests)
        outputs = [
            item async for item in session.items() if item.get("type") == "function_call_output"
        ]
        assert "private needle" not in str(outputs)


@local_search
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX native hook commands")
@pytest.mark.parametrize("programmatic", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_search_result_policy_precedes_native_consumers(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    programmatic: bool,
    fail: bool,
) -> None:
    from agents import _native

    private = "private-search-result-6bc1"
    project = tmp_path / "project"
    project.mkdir()
    (project / "private.txt").write_text(private)
    observed = tmp_path / "observed.jsonl"
    hook = tmp_path / "observe.py"
    hook.write_text(
        "import json, pathlib, sys\n"
        f"with pathlib.Path({str(observed)!r}).open('a') as output:\n"
        "    output.write(json.dumps(json.load(sys.stdin)) + '\\n')\n"
        "print('{}')\n"
    )
    original_create = _native.create_engine

    async def create_with_observer(config_json: str, runtime_json: str, **kwargs: Any) -> Any:
        runtime = json.loads(runtime_json)
        runtime["bypass_hook_trust"] = True
        Path(runtime["codex_home"], "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PostToolUse": [
                            {
                                "matcher": "project_grep",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": shlex.join([sys.executable, str(hook)]),
                                    }
                                ],
                            }
                        ]
                    }
                }
            )
        )
        return await original_create(config_json, json.dumps(runtime), **kwargs)

    monkeypatch.setattr(_native, "create_engine", create_with_observer)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def redact(context: ExecutorContext[None], value: Any) -> Any:
        nonlocal calls
        calls += 1
        assert private in json.dumps(value)
        entered.set()
        await release.wait()
        if fail:
            raise ValueError(private)
        return {"matches": [], "redacted": True}

    if programmatic:
        responses.respond(
            code_call(
                'text(await tools.project_grep({pattern: "private-search"}));', "observed-search"
            )
        )
    else:
        responses.respond(
            function_call("project_grep", {"pattern": "private-search"}, "observed-search")
        )
    if not fail:
        responses.respond(assistant_output("done"))
    async with (
        timeout(30),
        Agent(
            instructions="Search.",
            provider=provider(responses),
            programmatic_tool_calling=programmatic,
        ).session(
            environment=Environment.local(
                cwd=project, tools=["project_grep"], transform_result=redact
            )
        ) as session,
    ):
        turn = await session.submit("Search.")
        waiter = asyncio.create_task(turn.get_result())
        try:
            await entered.wait()
            assert not waiter.done()
            assert not observed.exists(), "Native consumers ran before result policy completed"
            release.set()
            if fail:
                with pytest.raises(NativeError, match="Executor policy failed"):
                    await waiter
                assert not observed.exists(), "Failed policy must withhold the result"
            else:
                assert (await waiter).final_output == "done"
                records = [json.loads(line) for line in observed.read_text().splitlines()]
                assert len(records) == 1
                assert private not in json.dumps(records)
                assert '"redacted": true' in json.dumps(records)
            assert calls == 1
            assert private not in json.dumps(responses.requests)
        finally:
            release.set()
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

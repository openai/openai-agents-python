"""Restored operations use current MCP bindings and do not re-grant omitted leaves."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CapabilityError,
    FunctionTool,
    HttpMCP,
    InteractionExpiredError,
    NativeAgents,
    NativeError,
    ToolRef,
)
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
from tests.test_native_child_agents import ChildPeer, collaboration, handles, metadata, trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


@pytest.mark.parametrize("location", ["host", "host-with-remote", "environment"])
async def test_idle_stdio_restore_uses_only_selected_destination_launch(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    first_log, current_log = tmp_path / "old", tmp_path / "current"
    first_dir, current_dir = tmp_path / "first-dir", tmp_path / "current-dir"
    first_dir.mkdir()
    current_dir.mkdir()
    old = stdio(
        first_log, "--label", "old-stdio", env_vars=("N19_STDIO_CURRENT",), cwd=str(first_dir)
    )
    now = stdio(
        current_log,
        "--label",
        "current-stdio",
        "--field",
        "revision",
        env_vars=("N19_STDIO_CURRENT",),
        cwd=str(current_dir),
        location="environment" if location == "environment" else "host",
    )
    async with timeout(35), AsyncExitStack() as stack:
        monkeypatch.setenv("N19_STDIO_CURRENT", "old-env")
        source = Agent(instructions="Read.", tools=[old], provider=provider(responses))
        async with source.session() as session:
            assert records(first_log) == []
            responses.respond(assistant_output("no process"))
            assert (
                await (await session.submit("Omit first", tools=[])).get_result()
            ).final_output == "no process"
            assert records(first_log) == []
            responses.respond(mcp_call("search", "mcp__local", "before"))
            responses.respond(assistant_output("done"))
            assert (await (await session.submit("Read")).get_result()).final_output == "done"
            saved = await session.save_state()
        await assert_stopped(first_log)
        before = records(first_log)
        monkeypatch.setenv("N19_STDIO_CURRENT", "current-env")
        environment = None
        if location != "host":
            executor = await stack.enter_async_context(ExecutorPeer(tmp_path / "executor").serve())
            trust(executor, tmp_path, monkeypatch)
            # The saved host-only session did not use strict executor writes.
            environment = declaration(executor, write_policy="legacy")
        # [] at opening excludes the source definition and cannot authorize it
        # later by name or by a new object. No destination process is acquired.
        async with source.session(state=saved, tools=[], environment=environment) as empty:
            assert records(current_log) == [] and records(first_log) == before
            with pytest.raises(CapabilityError, match="opening bindings|allowed"):
                await empty.submit("Cannot widen", tools=[now])
        # Capture current defaults without starting their server. The independent
        # Environment tools remain fixed even when this operation omits MCP.
        fixed = [environment] if environment is not None else []
        async with source.session(state=saved, tools=[now], environment=environment) as restored:
            assert records(first_log) == before and records(current_log) == []
            responses.respond(assistant_output("dormant"))
            assert (
                await (await restored.submit("What is available?", tools=fixed)).get_result()
            ).final_output == "dormant"
            assert ("mcp__local", "search") not in declarations(responses.requests[-1])
            assert records(current_log) == []
            with pytest.raises(CapabilityError, match="opening bindings"):
                await restored.submit("Cannot rebind", tools=[*fixed, old])
            with pytest.raises(CapabilityError, match="must have been allowed"):
                await restored.submit(
                    "Cannot widen", tools=[*fixed, ToolRef("excluded", "mcp__local")]
                )
            call = function_call("search", {"revision": "new"}, "after")
            call["item"]["namespace"] = "mcp__local"
            responses.respond(call)
            responses.respond(assistant_output("current"))
            assert (
                await (await restored.submit("Continue", tools=[*fixed, now])).get_result()
            ).final_output == "current"
            offer = declarations(responses.requests[-2])[("mcp__local", "search")]
            assert "revision" in offer["parameters"]["properties"]
            assert "key" not in offer["parameters"]["properties"]
            assert "current-stdio" in tool_output(responses.requests[-1], "after")
            assert records(first_log) == before
            responses.respond(assistant_output("omitted again"))
            assert (
                await (await restored.submit("No MCP", tools=fixed)).get_result()
            ).final_output == "omitted again"
            assert ("mcp__local", "search") not in declarations(responses.requests[-1])
            call = function_call("search", {"revision": "default"}, "default")
            call["item"]["namespace"] = "mcp__local"
            responses.respond(call)
            responses.respond(assistant_output("default from current"))
            assert (
                await (await restored.submit("Use defaults")).get_result()
            ).final_output == "default from current"
            assert "current-stdio" in tool_output(responses.requests[-1], "default")
            assert records(first_log) == before
        await assert_stopped(current_log)
    starts = [row for row in records(current_log) if row["event"] == "start"]
    assert starts and all(
        row["cwd"] == str(current_dir) and row["selected_env"] == "current-env" for row in starts
    )
    calls = [
        row["message"]["params"]
        for row in records(current_log)
        if row.get("message", {}).get("method") == "tools/call"
    ]
    assert sorted(call["_meta"]["callId"] for call in calls) == ["after", "default"]


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("--invalid-init", "connection not ready"),
        ("--missing-tool", "selected tools unavailable"),
    ],
)
async def test_dormant_stdio_startup_failure_fences_until_process_retired(
    responses: ResponsesFixture, tmp_path: Path, fault: str, message: str
) -> None:
    log = tmp_path / "selected"
    agent = Agent(instructions="Read.", provider=provider(responses))
    async with timeout(30):
        async with agent.session() as original:
            saved = await original.save_state()
        tool = stdio(log, fault)
        async with agent.session(state=saved, tools=[tool]) as restored:
            assert records(log) == [] and not responses.requests
            responses.respond(assistant_output("omitted"))
            assert (
                await (await restored.submit("No process", tools=[])).get_result()
            ).final_output == "omitted"
            assert records(log) == []
            before = len(responses.requests)
            selecting = await restored.submit("Use current defaults")
            assert selecting.turn_id is None
            with pytest.raises(NativeError, match=message):
                await selecting.get_result()
            assert len(responses.requests) == before
            await assert_stopped(log)
            with pytest.raises(RuntimeError, match="unconfirmed|closed"):
                await restored.submit("Cannot run on an unknown roster")
        assert not any(row.get("message", {}).get("method") == "tools/call" for row in records(log))


@pytest.mark.parametrize("valid", [True, False], ids=["ready-current", "bad-current"])
async def test_admitted_manual_restore_readies_current_stdio_before_grant(
    responses: ResponsesFixture, tmp_path: Path, valid: bool
) -> None:
    original_log, current_log = tmp_path / "original", tmp_path / "current"
    original_manual = FunctionTool(
        name="lookup", description="Look up", input_type=LookupArgs, output_type=str
    )
    current_manual = FunctionTool(
        name="lookup", description="Look up", input_type=LookupArgs, output_type=str
    )
    old = stdio(original_log, "--label", "old")
    current = stdio(current_log, "--label", "current", *([] if valid else ["--invalid-init"]))
    agent = Agent(instructions="Read.", tools=[original_manual, old], provider=provider(responses))
    async with timeout(30):
        async with agent.session() as original:
            assert records(original_log) == []
            responses.respond(function_call("lookup", {"key": "A"}, "manual"))
            turn = await original.submit("Wait with admitted stdio")
            expired = await next_request(original.interactions(), original_manual)
            assert records(original_log), "Default MCP was admitted before the pending tool"
            saved = await original.suspend()
        await assert_stopped(original_log)
        before = len(responses.requests)
        if not valid:
            # Already-admitted work cannot expose any grant until the current
            # selected server is validated; failure belongs to opening here.
            with pytest.raises(NativeError, match="connection not ready"):
                async with agent.session(state=saved, tools=[current_manual, current]):
                    pytest.fail("Current admitted MCP was not readied")
            assert len(responses.requests) == before
        else:
            responses.respond(mcp_call("search", "mcp__local", "resumed"))
            responses.respond(assistant_output("current selected server"))
            async with agent.session(state=saved, tools=[current_manual, current]) as restored:
                assert records(current_log) and len(responses.requests) == before
                assert restored.restored_turn is not None
                assert restored.restored_turn.turn_id == turn.turn_id
                with pytest.raises(InteractionExpiredError):
                    await expired.respond("not a grant")
                request = await next_request(restored.interactions(), current_manual)
                await request.respond("current answer")
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "current selected server"
                assert "current" in tool_output(responses.requests[-1], "resumed")
        await assert_stopped(current_log)


@pytest.mark.parametrize("bad_generation", [False, True], ids=["confirmed", "wrong-generation"])
async def test_cancel_dormant_stdio_activation_joins_receipt_and_process_cleanup(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_generation: bool,
) -> None:
    import _agents_codex.session as session_module

    log, release = tmp_path / "stdio", tmp_path / "list-release"
    agent = Agent(instructions="Read.", provider=provider(responses))
    async with timeout(30):
        async with agent.session() as original:
            saved = await original.save_state()
        server = stdio(log, "--list-release", str(release), startup_timeout=6)
        async with AsyncExitStack() as stack:
            if bad_generation:
                # Failed interruption cleanup also surfaces on Session exit.
                stack.enter_context(pytest.raises(NativeError, match="generation did not match"))
            restored = await stack.enter_async_context(agent.session(state=saved, tools=[server]))
            assert records(log) == [] and not responses.requests
            responses.respond(assistant_output("inactive"))
            assert (
                await (await restored.submit("Omit MCP", tools=[])).get_result()
            ).final_output == "inactive"
            count = len(responses.requests)
            assert records(log) == []
            check = session_module._check_mcp_receipt
            receipts: list[dict] = []

            def intercept(raw, servers, available, generation):
                receipt = json.loads(raw)
                receipts.append(receipt)
                if bad_generation:
                    receipt["result"]["policy_generation"] = generation + 1
                    raw = json.dumps(receipt)
                return check(raw, servers, available, generation)

            monkeypatch.setattr(session_module, "_check_mcp_receipt", intercept)
            turn = await restored.submit("Select MCP", tools=[server])
            opening = None
            try:
                while not any(
                    row.get("message", {}).get("method") == "tools/list" for row in records(log)
                ):
                    await asyncio.sleep(0.01)
                opening = asyncio.create_task(turn.cancel())
                await asyncio.sleep(0)
                assert not opening.done()
                release.touch()
                if bad_generation:
                    with pytest.raises(NativeError):
                        await opening
                    with pytest.raises(NativeError):
                        await turn.get_result()
                else:
                    await opening
                assert len(receipts) == 1
                assert receipts[0]["result"]["servers"]["local"]["status"] == "connected"
                if bad_generation:
                    assert len(responses.requests) == count
                    with pytest.raises(RuntimeError, match="unconfirmed|closed"):
                        await restored.submit("Must not run")
                else:
                    # Once native admission succeeds an interrupted request may
                    # have reached the provider. No cancelled tool effect is authorized.
                    assert not any(
                        row.get("message", {}).get("method") == "tools/call" for row in records(log)
                    )
                    monkeypatch.setattr(session_module, "_check_mcp_receipt", check)
                    responses.respond(mcp_call("search", "mcp__local", "after-cancel"))
                    responses.respond(assistant_output("confirmed"))
                    assert (
                        await (await restored.submit("Read current")).get_result()
                    ).final_output == "confirmed"
                    responses.respond(assistant_output("omitted again"))
                    assert (
                        await (await restored.submit("Omit again", tools=[])).get_result()
                    ).final_output == "omitted again"
                    assert ("mcp__local", "search") not in declarations(responses.requests[-1])
            finally:
                release.touch(exist_ok=True)
                if opening is not None:
                    await asyncio.gather(opening, return_exceptions=True)
        await assert_stopped(log)
    calls = [
        row["message"]["params"]
        for row in records(log)
        if row.get("message", {}).get("method") == "tools/call"
    ]
    assert [call["_meta"]["callId"] for call in calls] == (
        [] if bad_generation else ["after-cancel"]
    )


class ColdChildPeer(ChildPeer):
    def __init__(self) -> None:
        super().__init__(("default",))
        self.counts: dict[str, int] = {}

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        path = meta["agent_name"]
        index = self.counts.get(path, 0)
        self.counts[path] = index + 1
        if path == "/root":
            if index == 0:
                return [assistant_output("no child or MCP")]
            if index == 1:
                return [
                    collaboration(
                        "spawn_agent",
                        {"task_name": "alpha", "message": "Read current", "fork_turns": "none"},
                        "spawn-alpha",
                    )
                ]
            return [assistant_output("root complete")]
        name = path.rsplit("/", 1)[-1]
        self.child_requests.setdefault(name, []).append(body)
        self.child_metadata.setdefault(name, []).append(meta)
        if index == 0:
            call = mcp_call("excluded", "mcp__vault", "child-excluded")
            call["item"]["id"] = "excluded-" + name
            return [call]
        if index == 1:
            call = mcp_call("search", "mcp__vault", "child-current")
            call["item"]["id"] = "done-" + name
            return [call]
        return [assistant_output("child complete")]


@pytest.mark.parametrize("restoring", [False, True], ids=["fresh", "restored"])
async def test_cold_child_after_restore_inherits_only_selected_current_leaves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restoring: bool,
) -> None:
    old, current = (
        Peer(names=("search", "excluded"), label="old"),
        Peer(names=("search", "excluded"), label="current"),
    )
    peer = ColdChildPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        old.serve() as old_url,
        current.serve() as url,
        ExecutorPeer(tmp_path / "executor").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        old_tool = HttpMCP(name="vault", url=old_url, tools=("search", "excluded"))
        agent = Agent(
            instructions="Read.",
            tools=[old_tool, NativeAgents()],
            model="gpt-5.5",
            provider=peer.provider(websocket=False),
        )
        async with agent.session(environment=declaration(executor)) as source:
            assert old.requests == [] and peer.requests == []
            saved = await source.save_state()
            assert saved.descendants == []
        before = list(old.requests)
        tool = HttpMCP(name="vault", url=url, tools=("search", "excluded"))
        environment = declaration(executor)
        children = next(t for t in agent.tools if isinstance(t, NativeAgents))
        async with agent.session(
            state=saved if restoring else None, tools=[tool, children], environment=environment
        ) as restored:
            try:
                assert current.requests == [] and old.requests == before == []
                fixed = [children, environment]
                omitted = await restored.submit("No child or MCP yet", tools=fixed)
                assert (await omitted.get_result()).final_output == "no child or MCP"
                assert current.requests == [] and old.requests == []
                root = await restored.submit("Start a child", tools=[*fixed, tool.tool("search")])
                await peer.entered["alpha"].wait()
                assert (await root.get_result()).final_output == "root complete"
                [child] = await handles(restored, 1)
                offer = declarations(peer.child_requests["alpha"][0])
                assert ("mcp__vault", "search") in offer and ("mcp__vault", "excluded") not in offer
                count = len(current.requests)
                with pytest.raises(RuntimeError, match="children remain open"):
                    turn = await restored.submit("Cannot change live child grants", tools=fixed)
                    await turn.get_result()
                assert len(current.requests) == count
                peer.release["alpha"].set()
                assert (await child.get_result()).final_output == "child complete"
                assert len(current.calls) == 1 and current.calls[0]["name"] == "search"
                assert old.requests == before
                assert "current" in tool_output(peer.child_requests["alpha"][-1], "child-current")
            finally:
                peer.release["alpha"].set()


@pytest.mark.parametrize("restoring", [False, True], ids=["fresh", "restored"])
async def test_unused_code_restore_exposes_only_current_selected_mcp_namespace(
    responses: ResponsesFixture,
    restoring: bool,
) -> None:
    old, current = (
        Peer(label="old", names=("search", "excluded")),
        Peer(label="current", names=("search", "excluded")),
    )
    async with timeout(45), old.serve() as old_url, current.serve() as url:
        old_tool = HttpMCP(name="vault", url=old_url, tools=("search", "excluded"))
        agent = Agent(
            instructions="Read.",
            model="gpt-5.5",
            tools=[old_tool],
            provider=provider(responses),
            programmatic_tool_calling=True,
        )
        async with agent.session() as source:
            assert old.requests == [] and responses.requests == []
            saved = await source.save_state()
        before = list(old.requests)
        tool = HttpMCP(name="vault", url=url, tools=("search", "excluded"))
        fixed = [ToolRef("exec", "functions"), ToolRef("wait", "functions")]
        async with agent.session(state=saved if restoring else None, tools=[tool]) as restored:
            assert current.requests == [] and old.requests == before == []
            responses.respond(assistant_output("no MCP or code"))
            assert (
                await (await restored.submit("Not using MCP or code", tools=fixed)).get_result()
            ).final_output == "no MCP or code"
            assert current.requests == [] and old.requests == []
            responses.respond(
                code_call(
                    "text(JSON.stringify([typeof tools.mcp__vault__search, "
                    "typeof tools.mcp__vault__excluded]));",
                    "absent",
                )
            )
            responses.respond(assistant_output("absent"))
            assert (
                await (await restored.submit("Inspect current tools", tools=fixed)).get_result()
            ).final_output == "absent"
            assert code_output(responses.requests[-1], "absent").count("undefined") == 2
            assert current.requests == [] and old.requests == []
            responses.respond(
                code_call('text(await tools.mcp__vault__search({key:"blocked"}));', "blocked")
            )
            responses.respond(assistant_output("blocked"))
            assert (
                await (await restored.submit("Try dormant", tools=fixed)).get_result()
            ).final_output == "blocked"
            assert "current" not in code_output(responses.requests[-1], "blocked")
            assert current.requests == [] and old.requests == []
            responses.respond(
                code_call(
                    'text(JSON.stringify([await tools.mcp__vault__search({key:"A"}), '
                    "typeof tools.mcp__vault__excluded]));",
                    "selected",
                )
            )
            responses.respond(assistant_output("selected"))
            assert (
                await (
                    await restored.submit("Read", tools=[*fixed, tool.tool("search")])
                ).get_result()
            ).final_output == "selected"
            result = code_output(responses.requests[-1], "selected")
            assert "current" in result and "undefined" in result
            assert [(call["name"], call["arguments"]) for call in current.calls] == [
                ("search", {"key": "A"})
            ]
            assert old.requests == before

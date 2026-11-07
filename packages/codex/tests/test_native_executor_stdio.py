"""Local MCP processes coexist with explicitly selected remote execution."""

from __future__ import annotations

import json
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Environment, NativeAgents, NativeRole
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import ChildPeer, agent_for, handles, metadata, trust
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import mcp_call


class StdioChildren(ChildPeer):
    def __init__(self, programmatic_tool_calling: bool):
        super().__init__(("default", "default"))
        self.programmatic_tool_calling = programmatic_tool_calling

    def response(self):
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            return super().response()
        name = meta["agent_name"].rsplit("/", 1)[-1]
        self.child_metadata.setdefault(name, []).append(meta)
        requests = self.child_requests.setdefault(name, [])
        requests.append(body)
        if len(requests) == 1:
            return [
                code_call('text(await tools.mcp__local__search({key:"43"}));', "local-code")
                if self.programmatic_tool_calling
                else mcp_call("search", "mcp__local", "local-call")
            ]
        return [assistant_output(name + " complete")]

    def application(self):
        app = web.Application()
        app.router.add_post("/v1/responses", self.handle)
        app.router.add_get("/v1/responses", self.websocket)
        app.on_shutdown.append(self.close_sockets)
        return app


@pytest.mark.parametrize("programmatic_tool_calling", [False, True])
async def test_descendants_use_local_stdio_with_remote_execution(
    tmp_path, monkeypatch, programmatic_tool_calling
) -> None:
    peer = StdioChildren(programmatic_tool_calling)
    log = tmp_path / "stdio.jsonl"
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with Agent(
            instructions="Start both children and read records.",
            model=agent_for(peer, executor).model,
            programmatic_tool_calling=programmatic_tool_calling,
            provider=peer.provider(websocket=False),
            tools=[
                stdio(log, "--label", "local-child-result", cwd=str(tmp_path)),
                NativeAgents(
                    additional_roles=[
                        NativeRole(name="reader", instructions="Read.", description="Reader")
                    ],
                    max_children=2,
                ),
            ],
        ).session(environment=declaration(executor)) as session:
            await (await session.submit("Start both children")).get_result()
            turns = await handles(session, 2)
            results = [await (turn).get_result() for turn in turns]
            assert {result.final_output for result in results} == {
                "alpha complete",
                "beta complete",
            }
            for name in ("alpha", "beta"):
                assert "local-child-result" in json.dumps(peer.child_requests[name][1])
        await assert_stopped(log)
        assert (
            len(
                [
                    item
                    for item in records(log)
                    if item.get("message", {}).get("method") == "tools/call"
                ]
            )
            == 2
        )
        assert "native_stdio_mcp.py" not in json.dumps(executor.requests)


@pytest.mark.parametrize("programmatic_tool_calling", [False, True])
async def test_local_stdio_works_with_remote_executor(
    responses, tmp_path, monkeypatch, programmatic_tool_calling
) -> None:
    log = tmp_path / "stdio.jsonl"
    monkeypatch.setenv("N19_STDIO_CURRENT", "local-process-value")
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        responses.respond(
            code_call('text(await tools.mcp__local__search({key:"43"}));', "local-code")
            if programmatic_tool_calling
            else mcp_call("search", "mcp__local", "local-call")
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Read the local MCP record.",
            tools=[stdio(log, cwd=str(tmp_path), env_vars=["N19_STDIO_CURRENT"])],
            programmatic_tool_calling=programmatic_tool_calling,
            provider=provider(responses),
        ).session(environment=declaration(peer)) as session:
            assert (await (await session.submit("read")).get_result()).final_output == "done"
        await assert_stopped(log)
        launch = next(item for item in records(log) if item["event"] == "start")
        assert launch["cwd"] == str(tmp_path)
        assert launch["selected_env"] == "local-process-value"
        assert (
            len(
                [
                    item
                    for item in records(log)
                    if item.get("message", {}).get("method") == "tools/call"
                ]
            )
            == 1
        )
        assert "local-process-value" not in json.dumps(peer.requests)
        assert "native_stdio_mcp.py" not in json.dumps(peer.requests)


@pytest.mark.parametrize("programmatic_tool_calling", [False, True])
@pytest.mark.parametrize("selection", ["default", "empty", "all-three"])
async def test_remote_stdio_uses_executor_cwd_and_variables_and_closes_process(
    responses, tmp_path, monkeypatch, programmatic_tool_calling, selection
) -> None:
    monkeypatch.setenv("N19_STDIO_CURRENT", "synthetic-remote-environment-value")
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        # The executor inherited its environment at startup. The host now has a
        # different value, which the SDK must never copy into the remote launch.
        monkeypatch.setenv("N19_STDIO_CURRENT", "synthetic-host-secret-must-stay-local")
        log = peer.root / "remote-stdio.jsonl"
        if selection == "all-three":
            environment = declaration(peer)
        else:
            environment = Environment(
                url=peer.url,
                **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
                cwd=str(peer.root),
                headers={"Authorization": "Bearer " + peer.token},
                **({"tools": []} if selection == "empty" else {}),
            )
        responses.respond(
            code_call('text(await tools.mcp__local__search({key:"43"}));', "remote-code")
            if programmatic_tool_calling
            else mcp_call("search", "mcp__local", "remote-call")
        )
        responses.respond(assistant_output("remote done"))
        async with Agent(
            instructions="Read the remote MCP record.",
            tools=[stdio(log, env_vars=["N19_STDIO_CURRENT"], location="environment")],
            programmatic_tool_calling=programmatic_tool_calling,
            provider=provider(responses),
        ).session(environment=environment) as session:
            assert (await (await session.submit("read")).get_result()).final_output == "remote done"
            if selection != "all-three":
                for request in responses.requests:
                    declared = list(request.get("tools", []))
                    for item in request["input"]:
                        if item.get("type") == "additional_tools":
                            declared.extend(item["tools"])
                    assert declared
                    catalog = json.dumps(declared)
                    for name in ("exec_command", "write_stdin", "apply_patch"):
                        assert json.dumps(name) not in catalog
        await assert_stopped(log)
        launch = next(item for item in records(log) if item["event"] == "start")
        assert launch["cwd"] == str(peer.root)
        assert launch["selected_env"] == "synthetic-remote-environment-value"
        assert (
            sum(item.get("message", {}).get("method") == "tools/call" for item in records(log)) == 1
        )
        executor_wire = json.dumps(peer.requests)
        assert "native_stdio_mcp.py" in executor_wire
        assert "synthetic-host-secret-must-stay-local" not in executor_wire
        assert "synthetic-host-secret-must-stay-local" not in json.dumps(responses.requests)


async def test_remote_stdio_without_session_environment_fails_before_io(
    responses, tmp_path
) -> None:
    log = tmp_path / "must-not-launch.jsonl"
    agent = Agent(
        instructions="Read.",
        provider=provider(responses),
        tools=[stdio(log, location="environment")],
    )
    with pytest.raises(ValueError, match="requires a Session environment"):
        async with agent.session():
            pytest.fail("Remote MCP opened without an environment")
    assert not log.exists()
    assert responses.requests == []


@pytest.mark.requires_native_linux_sandbox
async def test_local_stdio_does_not_change_remote_executor_selection(
    responses, tmp_path, monkeypatch
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    log = local / "stdio.jsonl"
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        (peer.root / "remote-marker").write_text("remote-read-value")
        (local / "remote-marker").write_text("host-read-value")
        responses.respond(mcp_call("search", "mcp__local", "local-call"))
        responses.respond(
            function_call("exec_command", {"cmd": "cat remote-marker"}, "remote-call")
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use both tools.",
            tools=[stdio(log, cwd=str(local))],
            provider=provider(responses),
        ).session(environment=declaration(peer)) as session:
            await (await session.submit("read both")).get_result()
        await assert_stopped(log)
        result = tool_output(responses.requests[-1], "remote-call")
        assert "remote-read-value" in result
        assert "host-read-value" not in result
        assert (peer.root / "remote-marker").read_text() == "remote-read-value"
        assert (local / "remote-marker").read_text() == "host-read-value"

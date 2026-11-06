"""Whole child-turn MCP StopTurn joins real code cells, including late work."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    Allow,
    ChildInvocation,
    HttpMCP,
    NativeAgents,
    NativeError,
    NativeRole,
    StopTurn,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_child_agents import handles, metadata, trust
from tests.test_native_child_environment_stop import YieldedEnvironmentPeer
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import assert_stopped, records, stdio, wait_for_call
from tests.test_native_mcp_tools import Peer


@pytest.mark.parametrize("transport", ["http", "stdio-host", "stdio-environment"])
@pytest.mark.parametrize("websocket", [False, True], ids=["http-model", "ws-model"])
@pytest.mark.parametrize("phase", ["before", "approval"])
async def test_child_mcp_stop_retires_only_its_owned_cells(
    tmp_path, monkeypatch, transport, websocket, phase
):
    """Caught rejection never permits another effect; stopped cells cannot survive in successors."""
    waiting, decide = asyncio.Event(), asyncio.Event()
    notices = []

    def body(ctx, call):
        assert isinstance(ctx.submission, ChildInvocation)
        notices.append((ctx, call))
        args = json.loads(call.input) if isinstance(call.input, str) else call.input
        return args["key"] == "stopped-alpha"

    async def before(ctx, call):
        selected = body(ctx, call)
        if selected and phase == "before":
            waiting.set()
            await decide.wait()
            return StopTurn("child-private-stop")
        return Allow()

    async def approval(ctx, call):
        selected = body(ctx, call)
        if selected and phase == "approval":
            waiting.set()
            await decide.wait()
            return StopTurn("child-private-stop")
        return True

    class StopPeer(YieldedEnvironmentPeer):
        def __init__(self):
            super().__init__()
            self.server_name = "vault" if transport == "http" else "local"
            self.beta_output = None

        async def hold(self, event):
            if event.get("item", {}).get("id") == "alpha-policy-ready":
                # The policy must own its callback before alpha completes. A
                # yielded cell alone does not hold SDK callback admission open.
                await waiting.wait()
            else:
                await super().hold(event)

        def response(self):
            result = super().response()
            name = metadata(self.requests[-1], self.request_headers[-1])["agent_name"]
            if name == "/root" or self.successor:
                return result
            name = name.rsplit("/", 1)[-1]
            if len(self.child_requests[name]) == 2:
                selected = f"tools.mcp__{self.server_name}__search"
                if name == "alpha":
                    return [
                        code_call(
                            f'const selected = {selected}({{key:"stopped-alpha"}});'
                            "yield_control(); try { await selected; } catch (denied) {}"
                            'text(await tools.mcp__unhooked__search({key:"late-alpha-effect"}));',
                            "shared-second-cell",
                        )
                    ]
                return [
                    code_call(f'text(await {selected}({{key:"live-beta"}}));', "shared-second-cell")
                ]
            if len(self.child_requests[name]) > 2:
                if name == "beta":
                    # Keep the permitted sibling live until its actual code call has
                    # settled. Native code can automatically yield before an MCP reply.
                    number = len(self.child_requests[name])
                    request = self.requests[-1]
                    output = (
                        code_output(request, "shared-second-cell")
                        if number == 3
                        else tool_output(request, f"wait-beta-second-{number - 1}")
                    )
                    prefix = "Script running with cell ID "
                    if output.startswith(prefix):
                        cell = output[len(prefix) :].split()[0].rstrip(".")
                        return [
                            function_call(
                                "wait",
                                {"cell_id": cell, "yield_time_ms": 1000},
                                f"wait-beta-second-{number}",
                            )
                        ]
                    self.beta_output = output
                result = assistant_output(name + " complete")
                if name == "alpha":
                    result["item"]["id"] = "alpha-policy-ready"
                return [result]
            return result

    peer = StopPeer()
    vault = Peer(names=("search",))
    unhooked = Peer(names=("search",))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with timeout(48), AsyncExitStack() as stack:
        server = await stack.enter_async_context(TestServer(app, shutdown_timeout=1))
        executor = await stack.enter_async_context(ExecutorPeer(tmp_path / "remote").serve())
        following = await stack.enter_async_context(unhooked.serve())
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        policy = {"before": before, "approval": approval}
        log = None
        if transport == "http":
            url = await stack.enter_async_context(vault.serve())
            descriptor = HttpMCP(name="vault", url=url, tools=["search"], **policy)
        else:
            location = transport.removeprefix("stdio-")
            log = (executor.root if location == "environment" else tmp_path) / "original-peer"
            descriptor = stdio(log, location=location, **policy)
        agent = Agent(
            instructions="Use both native children and selected tools.",
            model="gpt-5.5",
            provider=peer.provider(websocket=websocket),
            tools=[
                descriptor,
                HttpMCP(name="unhooked", url=following, tools=["search"]),
                NativeAgents(
                    additional_roles=[
                        NativeRole(name="research", description="Read", instructions="Read")
                    ]
                ),
            ],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor)) as session:
            try:
                root = await session.submit("Start both children.")
                assert (await root.get_result()).final_output == "root complete"
                await waiting.wait()
                child_turns = await handles(session, 2)
                alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
                alpha = next(turn for turn in child_turns if turn.child_id == alpha_id)
                beta = next(turn for turn in child_turns if turn is not alpha)
                assert (await beta.get_result()).final_output == "beta complete"
                expected_receipt = "native-receipt" if log is None else "private-stdio-result"
                assert expected_receipt in peer.beta_output, (
                    peer.beta_output,
                    [(ctx.submission.child_id, str(call)) for ctx, call in notices],
                    records(log) if log else vault.calls,
                )
                assert peer.cell_id.keys() == {"alpha", "beta"}
                child = next(
                    state
                    for state in session._child_callbacks.values()
                    if state.handle.child_id == alpha.child_id
                    and state.handle.turn_id == alpha.turn_id
                )
                await asyncio.shield(child.terminal)
                assert unhooked.calls == []
                stopped = asyncio.create_task(alpha.get_result())
                decide.set()
                with pytest.raises(NativeError) as stopped_error:
                    await stopped
                # These receipts are concrete effects, independent of the error status.
                # Native code may yield before the sibling's MCP request reaches its peer.
                if log is None:
                    await vault.call_entered.wait()
                else:
                    await wait_for_call(log)
                calls = (
                    vault.calls
                    if log is None
                    else [
                        item["message"]["params"]
                        for item in records(log)
                        if item.get("message", {}).get("method") == "tools/call"
                    ]
                )
                assert [call["arguments"]["key"] for call in calls] == ["live-beta"]
                assert unhooked.calls == []
                assert len(peer.child_requests["alpha"]) == 3
                assert "child-private-stop" not in json.dumps(peer.requests)
                for ctx, call in notices:
                    expected = alpha if ctx.submission.child_id == alpha_id else beta
                    assert ctx.submission.turn_id == expected.turn_id
                    assert ctx.submission.submission_id == expected.submission_id
                    assert (call.thread_id, call.turn_id) == (expected.child_id, expected.turn_id)
                    assert call.outer_call_id == "shared-second-cell"
                peer.successor.update(("alpha", "beta"))
                fresh = [
                    await child.start("Wait on your original cell.")
                    async for child in session.subagents()
                ]
                results = await asyncio.gather(
                    *(t.get_result() for t in fresh), return_exceptions=True
                )
                assert all(not isinstance(res, BaseException) for res in results), results
                assert "Script running" not in peer.after_wait["alpha"], peer.after_wait
                assert "Script running with cell ID " in peer.after_wait["beta"], peer.after_wait
                assert str(stopped_error.value) == "Turn stopped by policy"
                assert (await root.get_result()).final_output == "root complete"
                assert (await beta.get_result()).final_output == "beta complete"
                assert bool(peer.frames) is websocket
                assert bool(peer.http_requests) is (not websocket)
            finally:
                decide.set()
        if log is not None:
            await assert_stopped(log)

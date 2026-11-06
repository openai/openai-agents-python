"""MCP policy identity and effect isolation after the root turn has finished."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from agents import Allow, ChildInvocation, HttpMCP, NativeError, ReplaceArguments, StopTurn
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import handles, trust
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize("in_code", [False, True], ids=["direct", "code"])
@pytest.mark.parametrize("decision", ["allow", "replace", "deny", "stop-before", "stop-approval"])
async def test_mcp_before_approval_use_exact_child_after_root_completion(
    tmp_path, monkeypatch, in_code, decision
):
    memory = Memory()
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    before_calls, approvals = {}, {}
    owned = {}
    both = asyncio.Event()

    async def before(ctx, call):
        assert isinstance(ctx.submission, ChildInvocation)
        name = next(
            name
            for name, handle in owned.items()
            if handle.submission_id == ctx.submission.submission_id
        )
        assert ctx.context is memory
        assert ctx.submission is not root
        assert ctx.submission == memory.entries[name].submission
        assert call.thread_id == owned[name].child_id
        assert call.turn_id == owned[name].turn_id
        assert (call.namespace, call.name) == ("mcp__vault", "find_record")
        assert (ctx.server_name, ctx.tool_name) == ("vault", "find-record")
        assert ctx.call.tool == next(
            tool.tool("find-record") for tool in agent.tools if isinstance(tool, HttpMCP)
        )
        assert (ctx.call.session_id, ctx.call.turn_id) == (session.id, owned[name].turn_id)
        assert (ctx.call.child_id, ctx.call.child_incarnation) == (
            ctx.submission.child_id,
            ctx.submission.instance_id,
        )
        assert (ctx.call.role, ctx.call.model) == (call.agent_role, call.model)
        assert ctx.call.call_id == call.call_id
        if in_code:
            assert ctx.call.call_id is None
            assert ctx.call.outer_call_id == "shared-mcp-id"
            assert ctx.call.cell_id == call.cell_id
            assert ctx.call.nested_call_id == call.runtime_tool_call_id
        else:
            assert ctx.call.call_id == "shared-mcp-id" and ctx.call.route == "direct"
        original = json.loads(call.input) if isinstance(call.input, str) else call.input
        assert original == {"key": "43"}
        before_calls[name] = (ctx, call)
        if len(before_calls) == 2:
            both.set()
        await both.wait()
        if name == "alpha" and decision == "stop-before":
            return StopTurn("child-private-stop")
        if decision == "replace":
            return ReplaceArguments({"key": "selected-" + name})
        return Allow()

    def approval(ctx, call):
        name = next(
            name
            for name, handle in owned.items()
            if handle.submission_id == ctx.submission.submission_id
        )
        expected = "selected-" + name if decision == "replace" else "43"
        value = json.loads(call.input) if isinstance(call.input, str) else call.input
        assert value == {"key": expected}
        assert ctx.call.call_id == before_calls[name][0].call.call_id
        approvals[name] = ctx
        if name == "alpha":
            if decision == "stop-approval":
                return StopTurn("child-private-stop")
            if decision == "deny":
                return False
        return True

    async with (
        timeout(34),
        family_peer(programmatic_tool_calling=in_code) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = family_agent(peer, executor, url, memory)
        agent = replace(
            agent,
            tools=[
                replace(tool, before=before, approval=approval)
                if isinstance(tool, HttpMCP)
                else tool
                for tool in agent.tools
            ],
        )
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            root = await session.submit("Start alpha and beta and finish")
            try:
                await asyncio.gather(*(event.wait() for event in peer.entered.values()))
                assert (await root.get_result()).final_output == "root complete"
                by_id = {turn.child_id: turn for turn in await handles(session, 2)}
                for name in ("alpha", "beta"):
                    owned[name] = by_id[peer.child_metadata[name][0]["thread_id"]]
                for event in peer.release.values():
                    event.set()
                results = await asyncio.gather(
                    *(t.get_result() for t in owned.values()), return_exceptions=True
                )
                assert not isinstance(results[1], BaseException), (
                    repr(results),
                    before_calls,
                    approvals,
                    vault.calls,
                )
                assert results[1].final_output == "beta complete"
                failed = decision.startswith("stop") or (decision == "deny" and not in_code)
                if failed:
                    assert isinstance(results[0], NativeError), results[0]
                    if decision.startswith("stop"):
                        assert str(results[0]) == "Turn stopped by policy"
                else:
                    assert not isinstance(results[0], BaseException), repr(results[0])
                    assert results[0].final_output == "alpha complete"
                assert before_calls.keys() == {"alpha", "beta"}
                assert approvals.keys() == (
                    {"beta"} if decision == "stop-before" else {"alpha", "beta"}
                )
                expected = (
                    ["selected-alpha", "selected-beta"] if decision == "replace" else ["43", "43"]
                )
                if decision in {"deny", "stop-before", "stop-approval"}:
                    expected = ["43"]
                assert sorted(call["arguments"]["key"] for call in vault.calls) == expected
                assert len(memory.mcp) == len(expected)
                assert (await root.get_result()).final_output == "root complete"
                assert "child-private-stop" not in json.dumps(peer.requests)
                if not in_code:
                    assert {ctx.call.call_id for ctx in approvals.values()} == {"shared-mcp-id"}
                    if decision == "replace":
                        for name in owned:
                            call = next(
                                item
                                for item in peer.child_requests[name][-1]["input"]
                                if item.get("call_id") == "shared-mcp-id"
                                and item.get("type") == "function_call"
                            )
                            assert json.loads(call["arguments"]) == {"key": "selected-" + name}
                else:
                    output = code_output(peer.child_requests["beta"][-1], "shared-mcp-id")
                    assert "accepted-mcp:" + owned["beta"].child_id in output
            finally:
                both.set()
                for event in peer.release.values():
                    event.set()


@pytest.mark.parametrize("location", ["host", "environment"])
@pytest.mark.parametrize("in_code", [False, True], ids=["direct", "code"])
async def test_stdio_child_effects_approvals_and_cleanup(tmp_path, monkeypatch, location, in_code):
    memory = Memory()
    owned, before_calls, approvals = {}, {}, {}
    both = asyncio.Event()

    def owner(ctx):
        assert isinstance(ctx.submission, ChildInvocation)
        assert ctx.context is memory
        name = next(
            name
            for name, handle in owned.items()
            if handle.submission_id == ctx.submission.submission_id
        )
        assert ctx.submission == memory.entries[name].submission
        assert ctx.submission is not root
        return name

    async def before(ctx, call):
        name = owner(ctx)
        assert (call.thread_id, call.turn_id) == (owned[name].child_id, owned[name].turn_id)
        assert (call.namespace, call.name) == ("mcp__local", "search")
        value = json.loads(call.input) if isinstance(call.input, str) else call.input
        assert value == {"key": "43"}
        before_calls[name] = ctx
        if len(before_calls) == 2:
            both.set()
        await both.wait()
        return ReplaceArguments({"key": "approved-for-" + name})

    def approval(ctx, call):
        name = owner(ctx)
        assert ctx.call == before_calls[name].call
        value = json.loads(call.input) if isinstance(call.input, str) else call.input
        assert value == {"key": "approved-for-" + name}
        approvals[name] = ctx
        return name == "alpha"

    def after(ctx, value):
        assert owner(ctx) == "alpha"
        assert value["structuredContent"] == {"receipt": "private-stdio-result"}
        memory.mcp.append(ctx)
        return {
            "content": [{"type": "text", "text": "accepted-mcp:" + ctx.submission.child_id}],
            "isError": False,
        }

    async with (
        timeout(38),
        family_peer(programmatic_tool_calling=in_code) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        # Only select the spawned Stdio peer. The unused HTTP descriptor from the
        # family fixture is replaced before Session admission or network access.
        agent = family_agent(peer, executor, "http://127.0.0.1:1/unselected", memory)
        log = (executor.root if location == "environment" else tmp_path) / "child-stdio"
        descriptor = stdio(
            log,
            location=location,
            before=before,
            approval=approval,
            transform_result=after,
        )
        invoke = peer.invoke

        def invoke_stdio(name, step):
            if step == 1:
                if in_code:
                    return code_call(
                        'text("MCP:" + JSON.stringify('
                        'await tools.mcp__local__search({key:"43"})));',
                        "shared-mcp-id",
                    )
                return mcp_call("search", "mcp__local", "shared-mcp-id")
            return invoke(name, step)

        monkeypatch.setattr(peer, "invoke", invoke_stdio)
        async with replace(
            agent, tools=[descriptor if isinstance(tool, HttpMCP) else tool for tool in agent.tools]
        ).session(context=memory, environment=declaration(executor)) as session:
            root = await session.submit("Start alpha and beta and finish")
            try:
                await asyncio.gather(*(event.wait() for event in peer.entered.values()))
                assert (await root.get_result()).final_output == "root complete"
                by_id = {handle.child_id: handle for handle in await handles(session, 2)}
                for name in ("alpha", "beta"):
                    owned[name] = by_id[peer.child_metadata[name][0]["thread_id"]]
                assert owned["alpha"].submission_id != owned["beta"].submission_id
                for event in peer.release.values():
                    event.set()
                results = await asyncio.gather(
                    *(handle.get_result() for handle in owned.values()), return_exceptions=True
                )
                assert not isinstance(results[0], BaseException), (results, approvals, records(log))
                assert results[0].final_output == "alpha complete"
                if in_code:
                    assert not isinstance(results[1], BaseException), (results, approvals)
                    assert results[1].final_output == "beta complete"
                    output = code_output(peer.child_requests["alpha"][-1], "shared-mcp-id")
                    assert "accepted-mcp:" + owned["alpha"].child_id in output
                else:
                    # Denial can fail its own direct child; it must not fail alpha or the root.
                    assert isinstance(results[1], NativeError), results[1]
                    assert {c.call.call_id for c in before_calls.values()} == {"shared-mcp-id"}
                    called = next(
                        item
                        for item in peer.child_requests["alpha"][-1]["input"]
                        if item.get("type") == "function_call"
                        and item.get("call_id") == "shared-mcp-id"
                    )
                    assert json.loads(called["arguments"]) == {"key": "approved-for-alpha"}
                assert before_calls.keys() == approvals.keys() == {"alpha", "beta"}
                assert before_calls["alpha"].submission != before_calls["beta"].submission
                calls = [
                    item["message"]["params"]
                    for item in records(log)
                    if item.get("message", {}).get("method") == "tools/call"
                ]
                assert [call["arguments"] for call in calls] == [{"key": "approved-for-alpha"}]
                assert len(memory.mcp) == 1
                assert (await root.get_result()).final_output == "root complete"
                later = await session.submit("later")
                assert (await later.get_result()).final_output == "root complete"
                assert "private-stdio-result" not in json.dumps(peer.requests)
                assert peer.http_requests and not peer.frames
            finally:
                both.set()
                for event in peer.release.values():
                    event.set()
        await assert_stopped(log)

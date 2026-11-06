"""MCP before decisions over an actual reused native model WebSocket."""

from __future__ import annotations

import json

import pytest

from agents import (
    Agent,
    Allow,
    HttpMCP,
    NativeError,
    ReplaceArguments,
    StopTurn,
    TurnStoppedError,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_mcp_tools import Peer
from tests.test_native_provider_websockets import peer as websocket_peer
from tests.test_native_scoped_policies import selected


@pytest.mark.parametrize("decision", ["allow", "replace", "deny", "stop"])
async def test_ws_mcp_reused_connection_matches_approved_effect_and_provider_history(decision):
    peer = Peer(names=("search",))
    observed = []

    def before(ctx, call):
        assert ctx.call.call_id == call.call_id == "guarded-one"
        if decision == "stop":
            return StopTurn("private-stop-reason")
        if decision == "replace":
            return ReplaceArguments({"key": "effective"})
        return Allow()

    def approval(ctx, call):
        observed.append((ctx.submission, json.loads(call.input)))
        return decision != "deny"

    async with timeout(22), peer.serve() as url, websocket_peer() as model:
        model.respond(assistant_output("warm"))
        model.respond(selected("guarded-one", "original"))
        model.respond(assistant_output("finished"))
        native = model.provider()
        agent = Agent(
            instructions="Search",
            provider=native,
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    before=before,
                    approval=approval,
                )
            ],
        )
        async with agent.session() as session:
            assert (await (await session.submit("warm up")).get_result()).final_output == "warm"
            turn = await session.submit("invoke")
            if decision in {"deny", "stop"}:
                with pytest.raises(TurnStoppedError if decision == "stop" else NativeError):
                    await turn.get_result()
                assert not peer.calls
                # The denied root does not ask for a continuation. A later
                # explicit root can consume the still-unused provider response.
                assert len(model.requests) == 2
                assert (
                    await (await session.submit("later")).get_result()
                ).final_output == "finished"
            else:
                assert (await turn.get_result()).final_output == "finished"
                expected = {"key": "effective" if decision == "replace" else "original"}
                assert observed == [(turn, expected)]
                assert [c["arguments"] for c in peer.calls] == [expected]
                inputs = model.requests[-1]["input"]
                call = next(
                    item
                    for item in inputs
                    if item.get("call_id") == "guarded-one" and item.get("type") == "function_call"
                )
                assert json.loads(call["arguments"]) == expected, model.frames
                assert any(
                    item.get("type") == "function_call_output"
                    and item.get("call_id") == "guarded-one"
                    for item in inputs
                )
            if decision == "stop":
                assert observed == []
            session.capabilities.require("mcp_before_policy")
            if decision == "deny":
                assert observed == [(turn, {"key": "original"})]
            assert model.frames and model.connections == 1
            assert not model.http_requests and not model.errors
            assert "private-stop-reason" not in json.dumps(model.requests)


@pytest.mark.parametrize("decision", ["allow", "replace", "deny", "stop"])
async def test_ws_code_mcp_observes_effect_without_rewriting_generated_code(decision):
    remote = Peer(names=("search",))
    observed = []

    def before(ctx, call):
        assert call.route != "direct"
        assert (ctx.server_name, ctx.tool_name) == ("records", "search")
        assert ctx.call.call_id is call.call_id is None
        assert ctx.call.outer_call_id == call.outer_call_id == "code-owned"
        assert ctx.call.cell_id == call.cell_id and ctx.call.cell_id is not None
        assert ctx.call.nested_call_id == call.runtime_tool_call_id
        assert ctx.call.nested_call_id is not None
        assert len({ctx.call.outer_call_id, ctx.call.cell_id, ctx.call.nested_call_id}) == 3
        assert (ctx.call.role, ctx.call.model) == (call.agent_role, call.model)
        assert ctx.call.route == "code"
        if decision == "replace":
            return ReplaceArguments({"key": "effective"})
        if decision == "stop":
            return StopTurn("private-stop-reason")
        return Allow()

    def approval(ctx, call):
        observed.append(call.input)
        return decision != "deny"

    async with timeout(20), remote.serve() as url, websocket_peer() as model:
        code = 'text(await tools.mcp__records__search({key:"original"}));'
        model.respond(assistant_output("warm"))
        model.respond(code_call(code, "code-owned"))
        model.respond(assistant_output("finished"))
        agent = Agent(
            instructions="Use tools",
            provider=model.provider(),
            programmatic_tool_calling=True,
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    before=before,
                    approval=approval,
                )
            ],
        )
        async with agent.session() as session:
            assert (await (await session.submit("warm")).get_result()).final_output == "warm"
            turn = await session.submit("run")
            if decision == "stop":
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
                assert not remote.calls
                assert len(model.requests) == 2
                assert (
                    await (await session.submit("later")).get_result()
                ).final_output == "finished"
            else:
                assert (await turn.get_result()).final_output == "finished"
                expected = {"key": "effective" if decision == "replace" else "original"}
                assert [c["arguments"] for c in remote.calls] == (
                    [] if decision == "deny" else [expected]
                ), code_output(model.requests[-1], "code-owned")
                assert observed == [expected]
                code_call_item = next(
                    item
                    for item in model.requests[-1]["input"]
                    if item.get("call_id") == "code-owned"
                    and item.get("type") == "custom_tool_call"
                )
                assert code_call_item["input"] == code
                output = code_output(model.requests[-1], "code-owned")
                assert ("native-receipt" in output) is (decision != "deny")
            assert model.connections == 1 and not model.http_requests and not model.errors
            assert "private-stop-reason" not in json.dumps(model.requests)


@pytest.mark.parametrize(
    "configured_websocket", [True, False], ids=["426-fallback", "explicit-http"]
)
async def test_direct_replacement_matches_after_actual_fallback_or_explicit_http(
    configured_websocket,
):
    remote = Peer(names=("search",))
    approved = []
    seen = []
    async with timeout(22), remote.serve() as url, websocket_peer(upgrade_status=426) as model:
        model.respond(assistant_output("warm"))
        model.respond(selected("only-this-call", "original-input"))
        model.respond(assistant_output("finished"))
        native = model.provider(websocket=configured_websocket)

        def before(ctx, call):
            assert ctx.submission is turn and call.call_id == "only-this-call"
            seen.append(json.loads(call.input))
            # This check observes the real Core-selected model transport.
            assert len(model.http_requests) == len(model.requests) == 2
            assert not model.frames
            return ReplaceArguments({"key": "private-proposed-argument"})

        def approval(ctx, call):
            approved.append((ctx.submission, json.loads(call.input)))
            return True

        agent = Agent(
            instructions="Search",
            provider=native,
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    before=before,
                    approval=approval,
                )
            ],
        )
        async with agent.session() as session:
            assert (await (await session.submit("warm")).get_result()).final_output == "warm"
            turn = await session.submit("search")
            assert (await turn.get_result()).final_output == "finished"
            effective = {"key": "private-proposed-argument"}
            assert approved == [(turn, effective)]
            assert [c["arguments"] for c in remote.calls] == [effective]
            item = next(
                i
                for i in model.http_requests[-1]["input"]
                if i.get("type") == "function_call" and i.get("call_id") == "only-this-call"
            )
            assert json.loads(item["arguments"]) == effective
            assert seen == [{"key": "original-input"}]
            assert len(model.upgrades) == (1 if configured_websocket else 0)
            assert not model.frames and len(model.http_requests) == len(model.requests) == 3
            session.capabilities.require("mcp_before_policy")

"""Application policies can compose across the native tool declarations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from agents import Agent, Allow, HttpMCP, StopTurn, ToolContext, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["success", "mcp-stop", "child-cancel"])
async def test_native_mixed_tool_policy(terminal, responses):
    peer = Peer(names=("search",))
    app = object()
    order = []
    child_entered = asyncio.Event()
    child_cleanup = asyncio.Event()
    original_handles = {}
    owners = {}

    def seen(scope, phase, context):
        assert context.context is app
        handle = context.submission
        assert original_handles.setdefault(scope, handle) is handle
        order.append((scope, phase))

    def root_before(context):
        seen("root", "function-before", context)
        return Allow()

    def root_after(context, output):
        seen("root", "function-after", context)
        return output

    @function_tool(before=root_before, after=root_after)
    def direct(context: ToolContext, key: str) -> str:
        seen("root", "function-body", context)
        return "function " + key

    def mcp_before(context, call):
        seen("root", "mcp-before", context)
        assert call.name == "search"
        assert peer.calls == []
        if terminal == "mcp-stop":
            return StopTurn("application refuses the record lookup")
        return Allow()

    def mcp_after(context, result):
        seen("root", "mcp-after", context)
        assert context.call_id == "mixed-mcp"
        assert len(peer.calls) == 1
        return result

    def child_before(context):
        seen("root", "child-before", context)
        return Allow()

    def child_after(context, output):
        seen("root", "child-after", context)
        return output

    async def leaf_before(context):
        seen("child", "function-before", context)
        child_entered.set()
        if terminal == "child-cancel":
            try:
                await asyncio.Future()
            finally:
                child_cleanup.set()
        return Allow()

    def leaf_after(context, output):
        seen("child", "function-after", context)
        return output

    @function_tool(before=leaf_before, after=leaf_after)
    def child_effect(context: ToolContext, key: str) -> str:
        seen("child", "function-body", context)
        return "child function " + key

    def extract(result):
        order.append(("child", "extract"))
        assert result.turn is original_handles["child"]
        assert all(item.agent is owners["child"] for item in result.items)
        assert result.items
        return "selected child value"

    responses.respond(function_call("direct", {"key": "read"}, "mixed-function"))
    responses.respond(mcp_call("search", "mcp__records", "mixed-mcp"))
    if terminal != "mcp-stop":
        responses.respond(function_call("nested", {"input": "work"}, "mixed-nested"))
        responses.respond(function_call("child_effect", {"key": "read"}, "mixed-child-function"))
        if terminal != "child-cancel":
            responses.respond(assistant_output("child complete"))
            responses.respond(assistant_output("root complete"))
    responses.respond(assistant_output("follow-up"))

    async with timeout(20), peer.serve() as url:
        child = Agent(
            name="Worker",
            instructions="Run the function.",
            provider=provider(responses),
            tools=[child_effect],
        )
        owners["child"] = child
        child_tool = replace(
            child.as_tool("nested", custom_output_extractor=extract, return_items=True),
            before=child_before,
            after=child_after,
        )
        records = HttpMCP(
            name="records",
            url=url,
            tools=["search"],
            approval_mode="preapproved",
            before=mcp_before,
            transform_result=mcp_after,
        )
        root = Agent(
            name="Coordinator",
            instructions="Use each tool.",
            provider=provider(responses),
            tools=[direct, records, child_tool],
        )
        async with root.session(context=app) as session:
            turn = await session.submit("Read one record.")
            if terminal == "child-cancel":
                await child_entered.wait()
                await turn.cancel()
                assert child_cleanup.is_set()
                with pytest.raises((TurnStoppedError, asyncio.CancelledError)):
                    await turn.get_result()
            elif terminal == "mcp-stop":
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            else:
                assert (await turn.get_result()).final_output == "root complete"
                assert "selected child value" in json.dumps(responses.requests[-1]["input"])
            assert original_handles["root"] is turn
            if terminal != "mcp-stop":
                assert original_handles["child"].session_id != turn.session_id
            followup = await session.submit("Continue with no tools.")
            assert (await followup.get_result()).final_output == "follow-up"

    prefix = [
        ("root", "function-before"),
        ("root", "function-body"),
        ("root", "function-after"),
        ("root", "mcp-before"),
    ]
    if terminal != "mcp-stop":
        prefix += [("root", "mcp-after"), ("root", "child-before"), ("child", "function-before")]
    if terminal == "success":
        prefix += [
            ("child", "function-body"),
            ("child", "function-after"),
            ("child", "extract"),
            ("root", "child-after"),
        ]
    assert order == prefix
    assert len(peer.calls) == (0 if terminal == "mcp-stop" else 1)

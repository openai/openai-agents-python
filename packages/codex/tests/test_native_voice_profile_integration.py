"""Captured voice profiles retain terminal tools and per-call MCP metadata."""

from __future__ import annotations

import json

import pytest

from agents import Agent, SessionState, ToolRef, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_stdio import records, stdio
from tests.test_native_mcp_tools import mcp_call


@pytest.mark.parametrize("restore", [False, True])
async def test_handoff_terminal_tool_survives_replacement_and_restore(responses, restore):
    effects = []

    @function_tool(finish_turn=True)
    def done() -> str:
        effects.append("done")
        return "leaf terminal result"

    leaf = Agent(name="Leaf", instructions="Use done.", tools=[done])
    root = Agent(
        name="Root", instructions="Hand off to Leaf.", handoffs=[leaf], provider=provider(responses)
    )
    responses.respond(function_call("transfer_to_leaf", {}, "handoff"))
    responses.respond(function_call("done", {}, "first-finish"))
    async with timeout(25), root.session() as session:
        first = await (await session.submit("Finish through Leaf.")).get_result()
        assert first.final_output == "leaf terminal result"
        assert first.tool_call is not None and first.tool_call.tool == ToolRef("done")
        assert await session.get_active_agent() is leaf
        await session.update(tools=[done])
        if restore:
            saved = await session.save_state()
        else:
            responses.respond(function_call("done", {}, "second-finish"))
            second = await (await session.submit("Finish again.")).get_result()
            assert second.final_output == "leaf terminal result"
            assert second.tool_call is not None
            assert second.tool_call.call_id == "second-finish"
    if restore:
        responses.respond(function_call("done", {}, "second-finish"))
        async with (
            timeout(25),
            root.session(state=SessionState.from_json(saved.to_json())) as session,
        ):
            assert await session.get_active_agent() is leaf
            second = await (await session.submit("Finish again.")).get_result()
            assert second.final_output == "leaf terminal result"
            assert second.tool_call is not None
            assert second.tool_call.call_id == "second-finish"
    assert effects == ["done", "done"]
    assert len(responses.requests) == 3


async def test_handoff_profile_uses_its_mcp_request_metadata(responses, tmp_path):
    log = tmp_path / "leaf-metadata"
    seen = []
    sentinel = "private-leaf-metadata-5193"

    def metadata(ctx):
        seen.append(ctx)
        return {"app/leaf": sentinel}

    leaf = Agent(
        name="Leaf",
        instructions="Search records.",
        tools=[stdio(log, request_metadata=metadata)],
    )
    root = Agent(
        name="Root", instructions="Hand off to Leaf.", handoffs=[leaf], provider=provider(responses)
    )
    responses.respond(function_call("transfer_to_leaf", {}, "handoff"))
    responses.respond(mcp_call("search", "mcp__local", "leaf-search"))
    responses.respond(assistant_output("leaf complete"))
    async with timeout(25), root.session() as session:
        turn = await session.submit("Search through Leaf.")
        assert (await turn.get_result()).final_output == "leaf complete"
        assert await session.get_active_agent() is leaf
        assert len(seen) == 1
        assert seen[0].submission is turn
        assert seen[0].call.call_id == "leaf-search"
    calls = [
        row["message"]["params"]
        for row in records(log)
        if row.get("message", {}).get("method") == "tools/call"
    ]
    assert len(calls) == 1
    assert calls[0]["_meta"]["app/leaf"] == sentinel
    assert sentinel not in json.dumps(responses.requests)

"""Restored profile defaults never widen an explicit empty tool ceiling."""

from __future__ import annotations

import pytest

from agents import Agent, HttpMCP
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_code_mode import declared_names
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_profile_mcp_metadata import profile_peer as profile_peer


@pytest.mark.parametrize("ceiling", ["persistent_empty", "operation_empty"])
async def test_restored_profile_keeps_explicit_empty_mcp_ceiling(responses, profile_peer, ceiling):
    peer, url = profile_peer
    observed = []

    def graph(label):
        def metadata(ctx):
            observed.append(label)
            return {"app/profile": label}

        target = Agent(
            name="B",
            instructions="B",
            tools=[HttpMCP(name="local", url=url, tools=("search",), request_metadata=metadata)],
        )
        return Agent(
            name="A",
            instructions="A",
            handoffs=[target],
            model="gpt-5.5",
            provider=provider(responses),
        ), target

    source, target = graph("old")
    responses.respond(function_call("transfer_to_b", {}, "handoff-b"))
    responses.respond(mcp_call("search", "mcp__local", "first-search"))
    responses.respond(assistant_output("first accepted"))
    async with timeout(30):
        async with source.session() as session:
            assert (
                await (await session.submit("Route and search")).get_result()
            ).final_output == "first accepted"
            assert await session.get_active_agent() is target
            if ceiling == "persistent_empty":
                await session.update(tools=[])
            saved = await session.save_state()
        fresh_source, fresh_target = graph("fresh")
        responses.respond(assistant_output("closed scope"))
        async with fresh_source.session(state=SessionState.from_json(saved.to_json())) as restored:
            assert await restored.get_active_agent() is fresh_target
            result = await (
                await restored.submit(
                    "Do not use tools", **({"tools": []} if ceiling == "operation_empty" else {})
                )
            ).get_result()
            assert result.final_output == "closed scope"
    assert observed == ["old"]
    assert len(peer.calls) == 1
    offered = declared_names(responses.requests[-1])
    assert "search" not in offered and "mcp__local__search" not in offered


async def test_profile_code_mode_empty_operation_keeps_helpers_and_mcp_unoffered(
    responses, profile_peer
):
    peer, url = profile_peer
    observed = []

    def metadata(ctx):
        observed.append(ctx)
        return {"app/profile": "must-not-send"}

    target = Agent(
        name="B",
        instructions="B",
        programmatic_tool_calling=True,
        tools=[HttpMCP(name="local", url=url, tools=("search",), request_metadata=metadata)],
    )
    source = Agent(
        name="A",
        instructions="A",
        handoffs=[target],
        programmatic_tool_calling=True,
        model="gpt-5.5",
        provider=provider(responses),
    )
    responses.respond(assistant_output("closed scope"))
    async with timeout(30), source.session() as session:
        await session.update(agent=target)
        result = await (await session.submit("No tools", tools=[])).get_result()
        assert result.final_output == "closed scope"
    assert observed == [] and peer.calls == []
    assert declared_names(responses.requests[-1]).isdisjoint(
        {"exec", "wait", "search", "mcp__local__search"}
    )

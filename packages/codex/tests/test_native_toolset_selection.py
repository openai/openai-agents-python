"""Reject invalid selected declarations before MCP effects; keep the old roster usable."""

from __future__ import annotations

import pytest

from agents import Agent, CapabilityError, HttpMCP, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


@pytest.mark.parametrize("route", ["ref", "sequence"])
@pytest.mark.parametrize("initial_mcp", [False, True], ids=["dormant", "active"])
async def test_invalid_python_selection_cannot_publish_mcp(
    responses: ResponsesFixture, route: str, initial_mcp: bool
) -> None:
    peer = Peer()
    effects: list[str] = []

    @function_tool
    def lookup() -> str:
        effects.append("original")
        return "original-result"

    async with timeout(20), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        roster = [lookup, remote]
        agent = Agent(
            instructions="Use the selected tools.", tools=roster, provider=provider(responses)
        )
        if route == "ref":
            bad = [lookup.ref, lookup.ref, remote.tool("search")]
        else:
            bad = [lookup, lookup, remote]
        chosen = [lookup, remote] if initial_mcp else [lookup]
        async with agent.session() as session:
            assert not peer.requests
            responses.respond(assistant_output("initial selection"))
            await (await session.submit("initial", tools=chosen)).get_result()
            before = list(peer.requests)
            assert bool(before) == initial_mcp
            rejection = None
            try:
                await session.submit("invalid", tools=bad)
            except (ValueError, RuntimeError) as error:
                rejection = (type(error), str(error))
            after = list(peer.requests)

            # Exercise the original session even on failure so a red result shows fencing
            # independently of the error type and the peer's connection history.
            responses.respond(function_call("lookup", {}, "after-invalid"))
            responses.respond(assistant_output("original survived"))
            survived = None
            try:
                completed = await (await session.submit("original", tools=chosen)).get_result()
                survived = completed.final_output
            except RuntimeError as error:
                survived = str(error)
            assert (rejection, after[len(before) :], survived, effects) == (
                (CapabilityError, "Operation tools must select distinct captured slots"),
                [],
                "original survived",
                ["original"],
            )
            assert tool_output(responses.requests[-1], "after-invalid") == "original-result"
            offered = declarations(responses.requests[-2])
            assert ("functions", "lookup") in offered
            assert (("mcp__docs", "search") in offered) == initial_mcp

            # Next valid operation uses the current admitted server.
            responses.respond(mcp_call("search", "mcp__docs", "after-valid"))
            responses.respond(assistant_output("MCP accepted"))
            assert (
                await (await session.submit("MCP", tools=[lookup, remote])).get_result()
            ).final_output == "MCP accepted"
            assert [call["_meta"]["callId"] for call in peer.calls] == ["after-valid"]

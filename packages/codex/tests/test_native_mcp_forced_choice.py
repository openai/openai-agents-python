"""Captured MCP selection and named choice retain one callable identity."""

from __future__ import annotations

import pytest

from agents import Agent, HttpMCP, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_named_control_paths import provider_for


@pytest.mark.parametrize("partial", [True, False])
async def test_named_mcp_choice_uses_selected_raw_binding(responses, partial):
    peer = Peer(names=("a.b", "a_b"))
    async with timeout(15), peer.serve() as url, provider_for(responses) as native_provider:
        complete = HttpMCP(name="docs", url=url, tools=["a.b", "a_b"])
        selected = HttpMCP(name="docs", url=url, tools=["a.b"]) if partial else complete
        actual = complete.tool("a.b")
        responses.respond(mcp_call(actual.name, actual.namespace, "chosen"))
        responses.respond(assistant_output("complete"))
        async with Agent(
            model="fixture-unlisted-named",
            instructions="Read the selected tool.",
            tools=[selected],
            model_settings={"tool_choice": selected.tool("a.b")},
            provider=native_provider,
        ).session() as session:
            assert (await (await session.submit("read")).get_result()).final_output == "complete"
        assert [call["name"] for call in peer.calls] == ["a.b"]
        assert len(responses.requests) == 2
        assert (actual.namespace, actual.name) in list(declarations(responses.requests[0]))
        assert responses.requests[0]["tool_choice"] == "required"
        assert list(declarations(responses.requests[0])) == [(actual.namespace, actual.name)]


async def test_clearing_collision_choice_restores_the_current_admitted_catalog(responses):
    @function_tool
    def local() -> str:
        return "local"

    peer = Peer(names=("a.b", "a_b"))
    async with timeout(15), peer.serve() as url, provider_for(responses) as native_provider:
        complete = HttpMCP(name="docs", url=url, tools=["a.b", "a_b"])
        selected = HttpMCP(name="docs", url=url, tools=["a.b"])
        actual = complete.tool("a.b")
        responses.respond(mcp_call(actual.name, actual.namespace, "clear-choice"))
        responses.respond(assistant_output("named"))
        responses.respond(assistant_output("cleared"))
        async with Agent(
            model="fixture-unlisted-named",
            instructions="Choose a tool when useful.",
            tools=[selected, local],
            model_settings={"tool_choice": selected.tool("a.b")},
            provider=native_provider,
        ).session() as session:
            assert (await (await session.submit("named")).get_result()).final_output == "named"
            await session.update(model_settings={"tool_choice": None})
            assert (await (await session.submit("clear")).get_result()).final_output == "cleared"
        assert responses.requests[0]["tool_choice"] == "required"
        assert list(declarations(responses.requests[0])) == [(actual.namespace, actual.name)]
        assert responses.requests[2]["tool_choice"] == "auto"
        assert set(declarations(responses.requests[2])) == {
            (actual.namespace, actual.name),
            ("functions", "local"),
        }
        assert [call["name"] for call in peer.calls] == ["a.b"]

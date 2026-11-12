"""Native selected-tool error policy over a real borrowed MCP connection."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace

import pytest

from agents import Agent, ToolContext, ToolRef, TurnStoppedError
from agents.classic.mcp import MCPServerStreamableHttp
from agents.migration.mcp import MCPNamespace, select_mcp_tools
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize(
    "policy", ["custom", "default", "disabled", "constructed-disable", "raise", "cancel"]
)
async def test_borrowed_mcp_retains_native_error_policy(
    responses: ResponsesFixture, policy: str
) -> None:
    peer = Peer()
    context = object()
    calls = []
    defaults = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fallback(call: ToolContext[object], error: Exception) -> str:
        assert call.context is context
        assert call.call_id == "borrowed-failure"
        assert call.call.tool == ToolRef("search", "catalog")
        assert str(error) == "Selected MCP tool invocation failed"
        calls.append(call)
        entered.set()
        if policy == "cancel":
            await release.wait()
        return "native custom fallback"

    def default(call) -> str:
        assert call.context is context
        defaults.append(call)
        return "native default fallback"

    responses.respond(mcp_call("search", "catalog", "borrowed-failure"))
    recovered = policy in {"custom", "default"}
    if recovered:
        responses.respond(assistant_output("recovered"))
    async with (
        timeout(30),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        [selected] = await select_mcp_tools(
            MCPNamespace("catalog", client), context=context, tool_names=["search"]
        )
        if policy == "constructed-disable":
            captured = {
                item.name: getattr(selected, item.name)
                for item in fields(selected)
                if item.name not in {"error_function", "_allow_run_error_formatter"}
            }
            selected = type(selected)(**captured, error_function=None)
        elif policy != "default":
            selected = replace(selected, error_function=fallback)
            if policy in {"disabled", "raise"}:
                selected = replace(selected, error_function=None)
        peer.denied = True
        agent = Agent(
            instructions="Read one record.", provider=provider(responses), tools=[selected]
        )
        async with agent.session(
            context=context, tool_error_formatter=default if policy != "raise" else None
        ) as session:
            turn = await session.submit("Read")
            if policy == "cancel":
                await entered.wait()
                stopping = asyncio.create_task(turn.cancel())
                release.set()
                await stopping
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            elif recovered:
                assert (await turn.get_result()).final_output == "recovered"
                output = tool_output(responses.requests[1], "borrowed-failure")
                assert (
                    "native custom fallback" if policy == "custom" else "native default fallback"
                ) in output
                assert peer.label not in output
            else:
                with pytest.raises(RuntimeError, match="Selected MCP tool invocation failed"):
                    await turn.get_result()
        peer.denied = False
        assert (await client.list_tools())[0].name == "search"
        assert (
            len([request for request in peer.requests if request.get("method") == "tools/call"])
            == 1
        )
    assert len(calls) == (policy in {"custom", "cancel"})
    assert len(defaults) == (policy == "default")

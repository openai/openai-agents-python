"""Live voice declarations follow the accepted native MCP selection."""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, HttpMCP, MCPResultContext, NativeProvider, Turn
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_mcp_tools import Peer
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("with_graph", [False, True])
async def test_live_mcp_selection_can_be_cleared_and_restored(
    realtime_peer: RealtimePeer, with_graph: bool
) -> None:
    voice = realtime_peer
    mcp = Peer(names=("lookup",), label="selected MCP result")
    accepted: list[MCPResultContext[Any]] = []

    def accept(context: MCPResultContext[Any], result: Any) -> Any:
        accepted.append(context)
        return result

    async with (
        timeout(30),
        mcp.serve() as url,
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-mcp-selection-key",
            supports_websockets=False,
        ) as provider,
    ):
        descriptor = HttpMCP(name="records", url=url, tools=["lookup"], transform_result=accept)
        agent = Agent(
            name="A",
            instructions="Read the selected records.",
            tools=[descriptor],
            handoffs=[Agent(name="B", instructions="B has no tools.")] if with_graph else [],
            provider=provider,
        )
        async with (
            agent.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
        ):
            initial = await voice.frame(lambda frame: frame["type"] == "session.update")
            declaration = next(
                tool for tool in initial["session"]["tools"] if "lookup" in tool.get("name", "")
            )
            name = declaration["name"]
            await voice.function(name, {"key": "before"}, "mcp-before")
            await voice.frame(lambda frame: frame.get("item", {}).get("call_id") == "mcp-before")
            assert isinstance(accepted[-1].submission, Turn)
            await accepted[-1].submission.get_result()

            start = len(voice.frames)
            await session.update(tools=[])
            cleared = await voice.frame(
                lambda frame: frame["type"] == "session.update", after=start
            )
            assert all(tool.get("name") != name for tool in cleared["session"]["tools"])

            start = len(voice.frames)
            await session.update(tools=[descriptor])
            restored = await voice.frame(
                lambda frame: frame["type"] == "session.update", after=start
            )
            assert any(tool.get("name") == name for tool in restored["session"]["tools"])
            await voice.function(name, {"key": "after"}, "mcp-after")
            result = await voice.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "mcp-after"
            )
            assert "selected MCP result" in result["item"]["output"]
            assert [call["arguments"]["key"] for call in mcp.calls] == ["before", "after"]
            assert len(accepted) == 2
            assert voice.responses.requests == []
            # Keep the live owner in scope until its final tool batch settles.
            assert isinstance(accepted[-1].submission, Turn)
            await accepted[-1].submission.get_result()
            await owner.update({"instructions": "Keep listening."})

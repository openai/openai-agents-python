"""Captured Agent graphs retain native MCP before and approval callbacks."""

from __future__ import annotations

import pytest

from agents import Agent, HttpMCP, NativeError, NativeProvider, StopTurn, Turn, TurnStoppedError
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("activated_leaf", [False, True], ids=["captured-root", "activated-leaf"])
@pytest.mark.parametrize("policy", ["before", "approval"])
async def test_agent_graph_mcp_callbacks_prevent_execution(
    responses: ResponsesFixture, activated_leaf: bool, policy: str
) -> None:
    peer = Peer(names=("search",))
    observed: list[str] = []

    def before(context, call):
        assert context.submission is turn
        observed.append("before")
        return StopTurn("synthetic-agent-policy-stop")

    def approval(context, call):
        assert context.submission is turn
        observed.append("approval")
        return False

    responses.respond(mcp_call("search", "mcp__records", "captured-mcp-call"))
    responses.respond(assistant_output("Session remains usable."))
    async with timeout(20), peer.serve() as url:
        server = HttpMCP(
            name="records",
            url=url,
            tools=["search"],
            approval_mode="preapproved",
            before=before if policy == "before" else None,
            approval=approval if policy == "approval" else None,
        )
        leaf = Agent(name="Leaf", instructions="Use records.", tools=[server])
        root = Agent(
            name="Root",
            instructions="Use records.",
            provider=provider(responses),
            tools=[server],
            handoffs=[leaf],
        )
        async with root.session() as session:
            if activated_leaf:
                await session.update(agent=leaf)
                assert await session.get_active_agent() is leaf
            turn = await session.submit("Search the protected record.")
            failure: NativeError | TurnStoppedError | None = None
            try:
                await turn.get_result()
            except (NativeError, TurnStoppedError) as error:
                failure = error
            assert peer.calls == []
            assert observed == [policy]
            assert isinstance(failure, TurnStoppedError if policy == "before" else NativeError)
            assert len(responses.requests) == 1
            followup = await session.submit("Continue without calling tools.")
            assert (await followup.get_result()).final_output == "Session remains usable."


@pytest.mark.parametrize("policy", ["before", "approval"])
async def test_realtime_agent_graph_mcp_callbacks_prevent_execution(
    realtime_peer: RealtimePeer, policy: str
) -> None:
    voice = realtime_peer
    mcp = Peer(names=("lookup",))
    observed: list[str] = []
    turns: list[Turn[None]] = []

    def before(context, call):
        observed.append("before")
        turns.append(context.submission)
        return StopTurn("synthetic-voice-mcp-stop")

    def approval(context, call):
        observed.append("approval")
        turns.append(context.submission)
        return False

    voice.responses.respond(assistant_output("Session remains usable after voice."))
    async with (
        timeout(25),
        mcp.serve() as url,
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-voice-mcp-policy-key",
            supports_websockets=False,
        ) as native,
    ):
        server = HttpMCP(
            name="records",
            url=url,
            tools=["lookup"],
            approval_mode="preapproved",
            before=before if policy == "before" else None,
            approval=approval if policy == "approval" else None,
        )
        agent = Agent(
            name="Voice",
            instructions="Use records.",
            provider=native,
            tools=[server],
            handoffs=[Agent(name="Leaf", instructions="No tools.")],
        )
        async with agent.session() as session:
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner:
                initial = await voice.frame(lambda frame: frame["type"] == "session.update")
                declaration = next(
                    tool for tool in initial["session"]["tools"] if "lookup" in tool.get("name", "")
                )
                await voice.function(declaration["name"], {"key": "protected"}, "voice-mcp-policy")
                await voice.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "voice-mcp-policy"
                )
                assert observed == [policy]
                assert len(turns) == 1 and isinstance(turns[0], Turn)
                if policy == "before":
                    with pytest.raises(TurnStoppedError):
                        await turns[0].get_result()
                else:
                    # Voice approval denial publishes a tool result without a text turn.
                    assert (await turns[0].get_result()).final_output is None
                assert mcp.calls == []
                assert voice.responses.requests == []
                await owner.update({"instructions": "Keep listening without using tools."})
            followup = await session.submit("Continue after voice.")
            assert (
                await followup.get_result()
            ).final_output == "Session remains usable after voice."

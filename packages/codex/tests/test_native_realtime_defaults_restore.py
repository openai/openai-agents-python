"""Restored active MCP subsets stay distinct from declaration defaults."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agents import Agent, HttpMCP, MCPResultContext, NativeProvider, Turn
from agents.realtime import RealtimeConfig
from agents.state import SessionState
from tests._asyncio import timeout
from tests.test_native_mcp_tools import Peer
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("keep_lookup", [False, True])
async def test_mono_voice_start_acquires_only_restored_mcp_defaults(
    realtime_peer: RealtimePeer, keep_lookup: bool
) -> None:
    voice = realtime_peer
    mcp = Peer(names=("lookup", "other"))
    async with (
        timeout(30),
        mcp.serve() as url,
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-restored-mono-key",
            supports_websockets=False,
        ) as provider,
    ):
        descriptor = HttpMCP(name="records", url=url, tools=["lookup", "other"])
        agent = Agent(
            name="Reader",
            instructions="Read selected records.",
            tools=[descriptor],
            provider=provider,
        )
        async with agent.session() as session:
            await session.update(
                tools=[replace(descriptor, tools=["lookup"])] if keep_lookup else []
            )
            saved = await session.save_state()
        mcp.requests.clear()
        async with (
            agent.session(state=SessionState.from_json(saved.to_json())) as restored,
            restored.realtime(config=RealtimeConfig(model="fixture-realtime")),
        ):
            initial = await voice.frame(lambda frame: frame["type"] == "session.update")
            names = [tool["name"] for tool in initial["session"]["tools"]]
            assert any("lookup" in name for name in names) is keep_lookup
            assert not any("other" in name for name in names)
            if not keep_lookup:
                assert mcp.requests == []
        assert voice.responses.requests == []


async def test_explicit_empty_voice_tools_keep_lazy_mcp_unopened(
    realtime_peer: RealtimePeer,
) -> None:
    voice = realtime_peer
    mcp = Peer(names=("lookup",))
    async with (
        timeout(30),
        mcp.serve() as url,
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-empty-voice-key",
            supports_websockets=False,
        ) as provider,
    ):
        agent = Agent(
            name="Reader",
            instructions="Listen without looking up records.",
            tools=[HttpMCP(name="records", url=url, tools=["lookup"])],
            provider=provider,
        )
        async with (
            agent.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime", tools=())),
        ):
            initial = await voice.frame(lambda frame: frame["type"] == "session.update")
            assert not any("lookup" in tool["name"] for tool in initial["session"]["tools"])
            assert mcp.requests == []


@pytest.mark.parametrize("keep_lookup", [False, True])
async def test_restored_mcp_defaults_do_not_widen_until_explicit_template_activation(
    realtime_peer: RealtimePeer, keep_lookup: bool
) -> None:
    voice = realtime_peer
    mcp = Peer(names=("lookup", "other"), label="restored selected result")
    accepted: list[MCPResultContext[Any]] = []

    def accept(context: MCPResultContext[Any], result: Any) -> Any:
        accepted.append(context)
        return result

    async with (
        timeout(40),
        mcp.serve() as url,
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-restored-default-key",
            supports_websockets=False,
        ) as provider,
    ):

        def graph() -> tuple[Agent, Agent, HttpMCP]:
            descriptor = HttpMCP(
                name="records", url=url, tools=["lookup", "other"], transform_result=accept
            )
            target = Agent(name="B", instructions="B reads records.", tools=[descriptor])
            source = Agent(
                name="A", instructions="A routes to B.", handoffs=[target], provider=provider
            )
            return source, target, descriptor

        source, target, descriptor = graph()
        async with source.session() as session:
            await session.update(agent=target)
            await session.update(
                tools=[replace(descriptor, tools=["lookup"])] if keep_lookup else []
            )
            saved = await session.save_state()

        source, target, _ = graph()
        async with source.session(state=SessionState.from_json(saved.to_json())) as restored:
            assert await restored.get_active_agent() is target
            start = len(voice.frames)
            async with restored.realtime(config=RealtimeConfig(model="fixture-realtime")):
                initial = await voice.frame(
                    lambda frame: frame["type"] == "session.update", after=start
                )
                names = [tool["name"] for tool in initial["session"]["tools"]]
                assert not any("other" in name for name in names)
                assert any("lookup" in name for name in names) is keep_lookup
                if keep_lookup:
                    name = next(name for name in names if "lookup" in name)
                    await voice.function(name, {"key": "restored"}, "restored-lookup")
                    result = await voice.frame(
                        lambda frame: frame.get("item", {}).get("call_id") == "restored-lookup"
                    )
                    assert "restored selected result" in result["item"]["output"]
                    assert isinstance(accepted[-1].submission, Turn)
                    await accepted[-1].submission.get_result()

            await restored.update(agent=source)
            await restored.update(agent=target)
            start = len(voice.frames)
            async with restored.realtime(config=RealtimeConfig(model="fixture-realtime")):
                initial = await voice.frame(
                    lambda frame: frame["type"] == "session.update", after=start
                )
                names = [tool["name"] for tool in initial["session"]["tools"]]
                assert any("lookup" in name for name in names)
                name = next(name for name in names if "other" in name)
                await voice.function(name, {"key": "template"}, "template-other")
                result = await voice.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "template-other"
                )
                assert "restored selected result" in result["item"]["output"]
                assert isinstance(accepted[-1].submission, Turn)
                await accepted[-1].submission.get_result()

            assert [call["name"] for call in mcp.calls] == (
                ["lookup", "other"] if keep_lookup else ["other"]
            )
            assert voice.responses.requests == []

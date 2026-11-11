"""Live voice declarations follow the accepted native MCP selection."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import (
    Agent,
    HttpMCP,
    MCPResultContext,
    NativeProvider,
    ReplaceArguments,
    Turn,
    function_tool,
)
from agents.realtime import RealtimeAgentConfig, RealtimeConfig
from tests._asyncio import timeout
from tests.native_fixtures import function_call, text_response
from tests.test_native_mcp_tools import Peer
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("voice_only", [False, True], ids=["opening", "voice-only"])
async def test_realtime_rechecks_source_predicate_before_native_mcp(
    realtime_peer: RealtimePeer, voice_only: bool
) -> None:
    peer = realtime_peer
    mcp = Peer(names=("lookup",), label="selected MCP result")
    application = {"enabled": True}
    predicates = []

    def enabled(context, tool):
        predicates.append((context.agent, tool))
        assert context.context is application
        return application["enabled"]

    async with (
        timeout(20),
        mcp.serve() as url,
        NativeProvider(
            base_url=peer.responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            name="Records",
            instructions="Read selected records.",
            tools=[HttpMCP(name="records", url=url, tools=["lookup"])],
            provider=provider,
            realtime=RealtimeAgentConfig(tool_enabled=enabled),
        )
        opening = (
            Agent(name="Backend", instructions="Backend", provider=provider)
            if voice_only
            else agent
        )
        async with (
            opening.session(context=application) as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            start = len(peer.frames) if voice_only else 0
            if voice_only:
                await voice.update_agent(agent)
            configuration = await peer.frame(
                lambda frame: frame["type"] == "session.update", after=start
            )
            name = next(
                tool["name"]
                for tool in configuration["session"]["tools"]
                if "lookup" in tool["name"]
            )
            application["enabled"] = False
            await peer.function(name, {"key": "blocked"}, "mcp-blocked")
            await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "mcp-blocked")
            assert mcp.calls == []
            assert len(predicates) == 2
            assert all(source is agent for source, _ in predicates)
            assert predicates[0][1] == predicates[1][1]

            application["enabled"] = True
            await peer.function(name, {"key": "allowed"}, "mcp-allowed")
            result = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "mcp-allowed"
            )
            assert "selected MCP result" in result["item"]["output"]
            assert [call["arguments"]["key"] for call in mcp.calls] == ["allowed"]


@pytest.mark.parametrize("phase", ["before", "approval", "metadata"])
async def test_realtime_rechecks_mcp_revocation_after_dispatch_hook(
    realtime_peer: RealtimePeer, phase: str
) -> None:
    peer = realtime_peer
    mcp = Peer(names=("lookup",), label="selected MCP result")
    application = {"enabled": True}
    entered = asyncio.Event()
    release = asyncio.Event()
    predicate_tools = []

    def tool_enabled(context, tool):
        assert context.agent is agent
        predicate_tools.append(tool)
        return context.context["enabled"]

    async def before(context, call):
        if phase == "before":
            entered.set()
            await release.wait()
        return ReplaceArguments({"key": "replacement"})

    async def approval(context, call):
        entered.set()
        await release.wait()
        return True

    async def request_metadata(context):
        entered.set()
        await release.wait()
        return {"fixture": True}

    async with (
        timeout(20),
        mcp.serve() as url,
        NativeProvider(
            base_url=peer.responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            instructions="Read selected records.",
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["lookup"],
                    before=before,
                    approval=approval if phase == "approval" else None,
                    request_metadata=request_metadata if phase == "metadata" else None,
                )
            ],
            provider=provider,
            realtime=RealtimeAgentConfig(tool_enabled=tool_enabled),
        )
        async with (
            agent.session(context=application) as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")),
        ):
            configuration = await peer.frame(lambda frame: frame["type"] == "session.update")
            name = next(
                tool["name"]
                for tool in configuration["session"]["tools"]
                if "lookup" in tool["name"]
            )
            await peer.function(name, {"key": "original"}, "revoked")
            await entered.wait()
            application["enabled"] = False
            release.set()
            await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "revoked")
            assert mcp.calls == []

            application["enabled"] = True
            await peer.function(name, {"key": "original"}, "allowed")
            result = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "allowed"
            )
            assert "selected MCP result" in result["item"]["output"]
            assert [call["arguments"]["key"] for call in mcp.calls] == ["replacement"]
            assert all(tool == predicate_tools[0] for tool in predicate_tools)
            if phase == "metadata":
                assert mcp.calls[0]["_meta"]["fixture"] is True


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
            first_result = await voice.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "mcp-before"
            )
            assert accepted, (first_result, mcp.calls)
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


@pytest.mark.parametrize("reject", [True, False], ids=["rejected", "concurrent"])
async def test_native_voice_graph_admission_preserves_backend_tool_replacement(
    realtime_peer: RealtimePeer,
    reject: bool,
) -> None:
    @function_tool(name="backend")
    def original() -> str:
        raise AssertionError("The original backend tool should have been replaced")

    @function_tool(name="backend")
    def replacement() -> str:
        return "updated backend tool"

    responses = realtime_peer.responses
    responses.respond(function_call("backend", {}, "backend-after-rejection"))
    responses.respond(*text_response("backend-after-rejection", "Backend complete"))
    release = None if reject else asyncio.Event()
    mcp = Peer(denied=reject, release_ready=release)
    async with (
        timeout(20),
        mcp.serve() as url,
        NativeProvider(
            base_url=responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
    ):
        opening = Agent(instructions="Backend", tools=[original], provider=provider)
        target = Agent(
            instructions="Voice Agent with MCP",
            tools=[HttpMCP(name="records", url=url, tools=["search"], startup_timeout=3)],
        )
        async with (
            opening.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            if release is None:
                for _ in range(2):
                    with pytest.raises(RuntimeError, match="records"):
                        await voice.update_agent(target)
                assert await voice.get_active_agent() is opening
                await session.update(tools=[replacement])
            else:
                admission = asyncio.create_task(voice.update_agent(target))
                try:
                    await mcp.ready_entered.wait()
                    replacing = asyncio.create_task(session.update(tools=[replacement]))
                    await asyncio.wait((replacing,), timeout=0.05)
                finally:
                    release.set()
                await asyncio.gather(admission, replacing)
                assert await voice.get_active_agent() is target
            assert await session.get_active_agent() is opening
            turn = await session.submit("Call the updated backend tool")
            assert (await turn.get_result()).final_output == "Backend complete"
            assert "updated backend tool" in str(responses.requests[-1]["input"])


async def test_unpublished_voice_mcp_uses_its_captured_raw_selector(
    realtime_peer: RealtimePeer,
) -> None:
    voice = realtime_peer
    original_peer = Peer(names=("a-b",), label="original endpoint")
    target_peer = Peer(names=("a_b",), label="target endpoint")
    async with (
        timeout(25),
        original_peer.serve() as original_url,
        target_peer.serve() as target_url,
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-unpublished-mcp-selector",
            supports_websockets=False,
        ) as provider,
    ):
        original = Agent(
            name="A",
            instructions="Original records",
            provider=provider,
            tools=[
                HttpMCP(
                    name="records",
                    url=original_url,
                    tools=["a-b"],
                    approval_mode="preapproved",
                )
            ],
        )
        target = Agent(
            name="B",
            instructions="Target records",
            tools=[
                HttpMCP(
                    name="records",
                    url=target_url,
                    tools=["a_b"],
                    approval_mode="preapproved",
                )
            ],
        )
        async with (
            original.session() as session,
            session.realtime(
                config=RealtimeConfig(
                    model="fixture-realtime",
                    automatically_trigger_response_for_mcp_tool_calls=False,
                )
            ) as owner,
        ):
            initial = await voice.frame(lambda frame: frame["type"] == "session.update")
            name = next(
                tool["name"]
                for tool in initial["session"]["tools"]
                if tool["name"].startswith("mcp__records__")
            )
            assert name == "mcp__records__a_b"
            start = len(voice.frames)
            await owner.update_agent(target)
            updated = await voice.frame(
                lambda frame: frame["type"] == "session.update", after=start
            )
            assert [
                tool["name"]
                for tool in updated["session"]["tools"]
                if tool["name"].startswith("mcp__records__")
            ] == [name]
            await voice.function(name, {"key": "target"}, "target-raw-selector")
            result = await voice.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "target-raw-selector"
            )
            assert "target endpoint" in result["item"]["output"]
            assert [(call["name"], call["arguments"]) for call in target_peer.calls] == [
                ("a_b", {"key": "target"})
            ]
            assert original_peer.calls == []
            assert await owner.get_active_agent() is target

            start = len(voice.frames)
            await owner.update_agent(original)
            await voice.frame(lambda frame: frame["type"] == "session.update", after=start)
            await voice.function(name, {"key": "original"}, "original-raw-selector")
            restored = await voice.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "original-raw-selector"
            )
            assert "original endpoint" in restored["item"]["output"]
            assert [(call["name"], call["arguments"]) for call in original_peer.calls] == [
                ("a-b", {"key": "original"})
            ]
            assert len(target_peer.calls) == 1
            assert await owner.get_active_agent() is original
            assert voice.responses.requests == []

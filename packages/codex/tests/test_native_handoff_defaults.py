"""Generated handoff selectors retain their accepted defaults across public consumers."""

from __future__ import annotations

import json

import pytest

from agents import Agent, HttpMCP, NativeProvider, ToolRef
from agents.realtime import RealtimeConfig
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import function_declarations, provider
from tests.test_native_mcp_resources import ResourcePeer
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


def graph(native_provider: NativeProvider) -> tuple[Agent, Agent, Agent]:
    last = Agent(name="C", instructions="C answers.")
    middle = Agent(name="B", instructions="B routes to C.", handoffs=[last])
    first = Agent(
        name="A", instructions="A routes to B.", handoffs=[middle], provider=native_provider
    )
    return first, middle, last


@pytest.mark.parametrize("activate_middle", [False, True])
async def test_selected_handoff_default_survives_public_save_and_restore(
    responses, activate_middle: bool
) -> None:
    native_provider = provider(responses)
    source, middle, last = graph(native_provider)
    name = "transfer_to_c" if activate_middle else "transfer_to_b"
    async with timeout(25), source.session() as session:
        if activate_middle:
            await session.update(agent=middle)
        await session.update(tools=[ToolRef(name)])
        saved = await session.save_state()
        assert json.loads(saved.to_json())["agent_options"]["_sdk_default_tools"] == [
            {"name": name, "namespace": None}
        ]

    source, middle, last = graph(native_provider)
    responses.respond(function_call(name, {}, "restored-selected-handoff"))
    responses.respond(assistant_output("Selected handoff completed"))
    async with (
        timeout(25),
        source.session(state=SessionState.from_json(saved.to_json())) as restored,
    ):
        assert await restored.get_active_agent() is (middle if activate_middle else source)
        turn = await restored.submit("Use the selected route.")
        assert (await turn.get_result()).final_output == "Selected handoff completed"
        assert await restored.get_active_agent() is (last if activate_middle else middle)
        assert name in {tool["name"] for tool in function_declarations(responses.requests[0])}


@pytest.mark.parametrize("activate_middle", [False, True])
async def test_selected_handoff_default_allows_public_realtime_start(
    realtime_peer: RealtimePeer, activate_middle: bool
) -> None:
    voice = realtime_peer
    async with (
        timeout(25),
        NativeProvider(
            base_url=voice.responses.url,
            api_key="synthetic-selected-handoff",
            supports_websockets=False,
        ) as native_provider,
    ):
        source, middle, _ = graph(native_provider)
        name = "transfer_to_c" if activate_middle else "transfer_to_b"
        async with source.session() as session:
            if activate_middle:
                await session.update(agent=middle)
            await session.update(tools=[ToolRef(name)])
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                initial = await voice.frame(lambda frame: frame["type"] == "session.update")
                assert name in {tool["name"] for tool in initial["session"]["tools"]}
            assert await session.get_active_agent() is (middle if activate_middle else source)
        assert voice.responses.requests == []


@pytest.mark.parametrize("select_handoff", [False, True])
async def test_idle_mcp_request_uses_effective_profile_and_selected_handoff_defaults(
    responses, select_handoff: bool
) -> None:
    peer = ResourcePeer()
    async with timeout(25), peer.serve() as url:
        descriptor = HttpMCP(name="records", url=url, tools=[])
        last = Agent(name="C", instructions="C answers.")
        middle = Agent(
            name="B", instructions="B reads records.", tools=[descriptor], handoffs=[last]
        )
        source = Agent(
            name="A", instructions="A routes to B.", handoffs=[middle], provider=provider(responses)
        )
        async with source.session() as session:
            await session.update(agent=middle)
            await session.update(
                tools=[ToolRef("transfer_to_c"), descriptor] if select_handoff else [descriptor]
            )
            assert (await session.list_mcp_resources("records"))["resources"]
            assert await session.get_active_agent() is middle
            assert [row["method"] for row in peer.requests].count("resources/list") == 1
            assert responses.requests == []

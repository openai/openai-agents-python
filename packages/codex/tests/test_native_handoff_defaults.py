"""Generated handoff selectors retain their accepted defaults across public consumers."""

from __future__ import annotations

import json
from contextlib import aclosing

import pytest

from agents import Agent, HttpMCP, NativeProvider, ToolRef, function_tool
from agents.realtime import RealtimeConfig, RealtimeToolCall
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


@pytest.mark.parametrize("selection", ["omitted", "subset", "empty"])
@pytest.mark.parametrize("restore", [False, True])
async def test_realtime_handoff_defaults_advertise_exact_selected_routes(
    realtime_peer: RealtimePeer, selection: str, restore: bool
) -> None:
    peer = realtime_peer
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-handoff-default-ceiling",
            supports_websockets=False,
        ) as native_provider,
    ):
        last = Agent(name="D", instructions="D answers.")
        included = Agent(name="B", instructions="B routes to D.", handoffs=[last])
        excluded = Agent(name="C", instructions="C answers.")
        source = Agent(
            name="A",
            instructions="Choose an admitted route.",
            handoffs=[included, excluded],
            provider=native_provider,
        )

        async def verify(session):
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                initial = await peer.frame(lambda frame: frame["type"] == "session.update")
                offered = {
                    tool["name"]
                    for tool in initial["session"]["tools"]
                    if tool["name"].startswith("transfer_to_")
                }
                assert offered == (
                    {"transfer_to_b", "transfer_to_c"}
                    if selection == "omitted"
                    else {"transfer_to_b"}
                    if selection == "subset"
                    else set()
                )
                if selection != "empty":
                    start = len(peer.frames)
                    await peer.function("transfer_to_b", {}, "selected-route")
                    updated = await peer.frame(
                        lambda frame: frame["type"] == "session.update", after=start
                    )
                    assert await session.get_active_agent() is included
                    assert {
                        tool["name"]
                        for tool in updated["session"]["tools"]
                        if tool["name"].startswith("transfer_to_")
                    } == {"transfer_to_d"}

        async with source.session() as session:
            if selection != "omitted":
                await session.update(
                    tools=[ToolRef("transfer_to_b")] if selection == "subset" else []
                )
            if restore:
                saved = await session.save_state()
            else:
                await verify(session)
        if restore:
            async with source.session(state=SessionState.from_json(saved.to_json())) as session:
                await verify(session)
        assert peer.responses.requests == []


@pytest.mark.parametrize("selection", ["subset", "empty"])
async def test_realtime_handoff_defaults_reject_excluded_route(
    realtime_peer: RealtimePeer, selection: str
) -> None:
    peer = realtime_peer
    included = Agent(name="B", instructions="B answers.")
    excluded = Agent(name="C", instructions="C answers.")
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-excluded-handoff",
            supports_websockets=False,
        ) as native_provider,
        Agent(
            name="A",
            instructions="Choose an admitted route.",
            handoffs=[included, excluded],
            provider=native_provider,
        ).session() as session,
    ):
        await session.update(tools=[ToolRef("transfer_to_b")] if selection == "subset" else [])
        active = await session.get_active_agent()
        async with (
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
            aclosing(owner.events()) as events,
        ):
            await peer.frame(lambda frame: frame["type"] == "session.update")
            await peer.function("transfer_to_c", {}, "excluded-route")
            # Native rejects unadvertised calls before application dispatch and
            # closes the transport; its internal rejection text is not a wire event.
            with pytest.raises(RuntimeError, match="Realtime connection closed unexpectedly"):
                async for event in events:
                    if isinstance(event, RealtimeToolCall) and event.call_id == "excluded-route":
                        pytest.fail("Excluded handoff entered application dispatch")
            assert await session.get_active_agent() is active
        assert peer.responses.requests == []


async def test_handoff_uses_leaf_captured_model_request_controls(responses) -> None:
    effects: list[str] = []

    @function_tool
    def selected() -> str:
        effects.append("selected")
        return "selected result"

    @function_tool
    def excluded() -> str:
        pytest.fail("Named leaf choice must exclude this tool")

    leaf = Agent(
        name="Leaf",
        instructions="Use the selected leaf tool.",
        # Live handoffs require qualified metadata for retained approval authority.
        model="gpt-5.5",
        tools=[selected, excluded],
        model_settings={
            "max_output_tokens": 23,
            "temperature": 0.4,
            "metadata": {"workflow": "captured-leaf"},
            "tool_choice": selected.ref,
            "invocation_timeout_ms": 10_000,
        },
    )
    responses.respond(function_call("transfer_to_leaf", {}, "leaf-route"))
    responses.respond(function_call("selected", {}, "selected-leaf-tool"))
    responses.respond(assistant_output("Leaf controls accepted"))
    async with (
        timeout(25),
        NativeProvider(
            base_url=responses.url,
            api_key="synthetic-profile-model-controls",
            supports_websockets=False,
        ) as native_provider,
        Agent(
            name="Root",
            instructions="Hand off to Leaf.",
            model="gpt-5.5",
            handoffs=[leaf],
            provider=native_provider,
            model_settings={"max_output_tokens": 11, "invocation_timeout_ms": 50_000},
        ).session() as session,
    ):
        result = await (await session.submit("Use the leaf controls.")).get_result()
        assert result.final_output == "Leaf controls accepted"
        assert await session.get_active_agent() is leaf
        saved = await session.save_state()
        snapshots = [
            row["payload"]["thread_settings"]
            for row in saved.history
            if row.get("type") == "event_msg"
            and row.get("payload", {}).get("type") == "thread_settings_applied"
            and row["payload"].get("thread_id") == saved.source_session_id
        ]
        assert snapshots[-1]["invocation_timeout_ms"] == 10_000
    assert effects == ["selected"]
    assert len(responses.requests) == 3
    assert responses.requests[0]["max_output_tokens"] == 11
    for request in responses.requests[1:]:
        assert request["max_output_tokens"] == 23
        assert request["temperature"] == 0.4
        assert request["metadata"] == {"workflow": "captured-leaf"}
        assert request["tool_choice"] == "required"
        assert [tool["name"] for tool in function_declarations(request)] == ["selected"]

"""Voice selectors resolve current bindings without changing their source authority."""

from contextlib import aclosing
from dataclasses import replace

import pytest

from agents import Agent, CapabilityError, NativeProvider, function_tool
from agents.realtime import RealtimeConfig, RealtimeDelegation
from agents.voice import NativeSessionVoiceWorkflow
from tests._asyncio import timeout
from tests.native_fixtures import function_call, text_response
from tests.test_native_agent import function_declarations
from tests.test_native_realtime import realtime_peer as realtime_peer


@pytest.mark.parametrize("graph", [False, True])
async def test_background_voice_original_selector_uses_current_replacement(realtime_peer, graph):
    peer = realtime_peer
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "record"

    @function_tool
    def excluded() -> str:
        raise AssertionError("Unselected function must never execute")

    fresh = replace(lookup, description="Current acknowledged lookup")
    foreign = replace(lookup, description="Other Agent declaration")
    target = Agent(name="Other", instructions="Other source.", tools=[foreign])
    peer.responses.respond(function_call("lookup", {}, "backend-lookup"))
    peer.responses.respond(*text_response("answer", "accepted"))
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-selector", supports_websockets=False
        ) as provider,
        Agent(
            name="Root",
            instructions="Use lookup.",
            tools=[lookup, excluded],
            handoffs=[target] if graph else [],
            provider=provider,
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))) as voice,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        await session.update(tools=[fresh, excluded])
        await peer.handoff("Look up the record.")
        async with aclosing(voice.events()) as events:
            async for event in events:
                if isinstance(event, RealtimeDelegation) and event.status in {
                    "failed",
                    "completed",
                }:
                    assert event.status == "completed"
                    break
            else:
                pytest.fail("Background delegation did not settle")
        assert effects == ["lookup"]
        offered = function_declarations(peer.responses.requests[0])
        assert [tool["name"] for tool in offered] == ["lookup"]
        assert offered[0]["description"] == "Current acknowledged lookup"


@pytest.mark.parametrize("change_profile", [False, True], ids=["foreign-selector", "other-profile"])
async def test_voice_selector_rejects_foreign_same_ref_in_current_source(
    realtime_peer, change_profile
):
    @function_tool
    def lookup() -> str:
        raise AssertionError("Foreign selector must not invoke a function")

    foreign = replace(lookup, description="Other Agent declaration")
    target = Agent(name="Other", instructions="Other source.", tools=[foreign])
    async with (
        timeout(15),
        NativeProvider(
            base_url=realtime_peer.responses.url,
            api_key="synthetic-foreign-selector",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="Root",
            instructions="Use lookup.",
            tools=[lookup],
            handoffs=[target],
            provider=provider,
        ).session() as session,
    ):
        workflow = NativeSessionVoiceWorkflow(
            session, tools=(lookup if change_profile else foreign,)
        )
        if change_profile:
            await session.update(agent=target)
        with pytest.raises(CapabilityError, match="captured"):
            async with aclosing(workflow.run("Try lookup.")) as chunks:
                async for _ in chunks:
                    pytest.fail("Foreign selector produced output")
        assert realtime_peer.responses.requests == []


async def test_closed_voice_workflow_with_explicit_tool_reports_session_closed(realtime_peer):
    @function_tool
    def lookup() -> str:
        raise AssertionError("Closed Session must not invoke a function")

    async with NativeProvider(
        base_url=realtime_peer.responses.url,
        api_key="synthetic-closed-selector",
        supports_websockets=False,
    ) as provider:
        async with Agent(
            instructions="Use lookup.", tools=[lookup], provider=provider
        ).session() as session:
            workflow = NativeSessionVoiceWorkflow(session, tools=(lookup,))
        with pytest.raises(RuntimeError, match="Session is closed"):
            async with aclosing(workflow.run("Try lookup.")) as chunks:
                async for _ in chunks:
                    pytest.fail("Closed Session produced output")
        assert realtime_peer.responses.requests == []

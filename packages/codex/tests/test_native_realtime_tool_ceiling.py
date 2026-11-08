import asyncio
from contextlib import aclosing

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.realtime import NativeRealtimeToolChoice, RealtimeConfig
from tests._asyncio import timeout
from tests.native_fixtures import function_call, text_response
from tests.test_native_agent import function_declarations
from tests.test_native_realtime import realtime_peer as realtime_peer


@pytest.mark.parametrize("mode", ["omitted", "empty", "subset"])
async def test_background_realtime_tool_ceiling(realtime_peer, mode):
    peer = realtime_peer
    effects = []

    @function_tool
    def included() -> str:
        return "included"

    @function_tool
    def excluded() -> str:
        effects.append("excluded")
        return "excluded-effect"

    peer.responses.respond(function_call("excluded", {}, "excluded-backend"))
    peer.responses.respond(*text_response("backend-final", "accepted"))
    config = RealtimeConfig(
        model="fixture-realtime",
        tools=None if mode == "omitted" else () if mode == "empty" else (included,),
        settings={"tool_choice": {"type": "function", "name": "background_agent"}}
        if mode == "empty"
        else None,
    )
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-ceiling", supports_websockets=False
        ) as provider,
        Agent(
            instructions="Use current tools.", tools=[included, excluded], provider=provider
        ).session() as session,
        session.realtime(config=config),
    ):
        initial = await peer.frame(lambda f: f["type"] == "session.update")
        if mode == "empty":
            assert initial["session"]["tool_choice"] == {
                "type": "function",
                "name": "background_agent",
            }
        await peer.handoff("Attempt excluded backend tool.")
        await peer.frame(
            lambda f: (
                f.get("item", {}).get("call_id") == "voice-call"
                and f.get("item", {}).get("type") == "function_call_output"
            )
        )
        offered = [t.get("name") for t in function_declarations(peer.responses.requests[0])]
        assert effects == (["excluded"] if mode == "omitted" else [])
        assert ("excluded" in offered) is (mode == "omitted")


@pytest.mark.parametrize("change", ["tools", "profile"])
async def test_realtime_capture_keeps_ceiling_after_source_change(realtime_peer, change):
    from dataclasses import replace

    from agents.realtime import RealtimeDelegation

    peer = realtime_peer
    effects = []

    @function_tool
    def included() -> str:
        return "old included"

    @function_tool
    def excluded() -> str:
        effects.append("excluded")
        return "excluded-effect"

    fresh = replace(included, description="Fresh admitted source")
    target = Agent(name="B", instructions="Target.", tools=[fresh, excluded])
    peer.responses.respond(function_call("excluded", {}, "excluded-backend"))
    peer.responses.respond(*text_response("backend-final", "accepted"))
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-stale-ceiling",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="A",
            instructions="Use current tools.",
            tools=[included, excluded],
            handoffs=[target] if change == "profile" else [],
            provider=provider,
        ).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(included,))
        ) as owner,
    ):
        await peer.frame(lambda f: f["type"] == "session.update")
        if change == "profile":
            await session.update(agent=target)
        else:
            await session.update(tools=[fresh, excluded])

        async def terminal():
            async with aclosing(owner.events()) as events:
                async for event in events:
                    if isinstance(event, RealtimeDelegation) and event.status in (
                        "failed",
                        "completed",
                    ):
                        return event

        waiting = asyncio.create_task(terminal())
        await peer.handoff("Attempt excluded backend tool after source changed.")
        event = await waiting
        assert event is not None
        assert not effects
        if change == "profile":
            assert event.status == "failed"
            assert peer.responses.requests == []
        else:
            assert event.status == "completed"
            offered = function_declarations(peer.responses.requests[0])
            assert [tool["name"] for tool in offered] == ["included"]
            assert offered[0]["description"] == "Fresh admitted source"


async def test_voice_workflow_captures_tool_sequence(realtime_peer):
    from agents.voice import NativeSessionVoiceWorkflow

    peer = realtime_peer
    effects = []

    @function_tool
    def included() -> str:
        return "included"

    @function_tool
    def excluded() -> str:
        effects.append("excluded")
        return "excluded-effect"

    peer.responses.respond(function_call("excluded", {}, "excluded-backend"))
    peer.responses.respond(*text_response("backend-final", "accepted"))
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-captured-ceiling",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Use selected tools.", tools=[included, excluded], provider=provider
        ).session() as session,
    ):
        selected = [included]
        workflow = NativeSessionVoiceWorkflow(session, tools=selected)
        selected.append(excluded)
        assert [part async for part in workflow.run("Use current tools.")] == ["accepted"]
        assert not effects
        assert [t["name"] for t in function_declarations(peer.responses.requests[0])] == [
            "included"
        ]


async def test_realtime_tool_choice_uses_real_ack_and_preserves_accepted_subset(realtime_peer):
    peer = realtime_peer

    @function_tool
    def weather() -> str:
        return "sunny"

    @function_tool
    def excluded() -> str:
        raise AssertionError("Excluded function must never execute")

    named = {"type": "function", "name": "weather"}
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-tool-choice", supports_websockets=False
        ) as provider,
        Agent(
            instructions="Use weather.", tools=[weather, excluded], provider=provider
        ).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                tools=(weather,),
                settings={"tool_choice": {"type": "function", "name": "weather"}},
            )
        ) as voice,
    ):
        initial = await peer.frame(lambda f: f["type"] == "session.update")
        assert initial["session"]["tool_choice"] == named
        assert not any(tool.get("name") == "excluded" for tool in initial["session"]["tools"])
        before = len(peer.frames)
        with pytest.raises(Exception, match="advertised function subset"):
            await voice.update({"tool_choice": {"type": "function", "name": "excluded"}})
        assert not any(f["type"] == "session.update" for f in peer.frames[before:])

        # A provider ACK, rather than a mocked call completion, settles the update.
        peer.auto_ack_updates = False
        updating = asyncio.create_task(voice.update({"output_speed": 1.1}))
        acknowledgement = None
        try:
            sparse = await peer.frame(lambda f: f["type"] == "session.update", after=before)
            assert "tool_choice" not in sparse["session"]
            acknowledgement = await peer.pending_updates.get()
            assert acknowledgement["session"]["tool_choice"] == named
            assert not updating.done()
            assert peer.socket is not None
            peer.auto_ack_updates = True
            await peer.socket.send_json(acknowledgement)
            acknowledgement = None
            await updating
        finally:
            peer.auto_ack_updates = True
            if acknowledgement is not None and peer.socket is not None:
                await peer.socket.send_json(acknowledgement)
            while not peer.pending_updates.empty():
                await peer.ack_next_update()
            if not updating.done():
                updating.cancel()
            await asyncio.gather(updating, return_exceptions=True)

        modes: tuple[NativeRealtimeToolChoice, ...] = ("none", "required", "auto")
        for mode in modes:
            before = len(peer.frames)
            await voice.update({"tool_choice": mode})
            reset = await peer.frame(lambda f: f["type"] == "session.update", after=before)
            assert reset["session"]["tool_choice"] == mode
        before = len(peer.frames)
        await voice.update({"output_speed": 1.2})
        await peer.frame(lambda f: f["type"] == "session.update", after=before)

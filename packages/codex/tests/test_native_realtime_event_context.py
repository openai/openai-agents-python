"""Public observations over real native ownership and a scripted provider wire."""

import asyncio
import json

import pytest

from agents import Agent, Allow, NativeProvider, ToolOutput, function_tool
from agents.realtime import RealtimeConfig, RealtimeResponse, RealtimeToolCall
from tests._asyncio import timeout
from tests.test_native_manual_tools import next_request
from tests.test_native_realtime import realtime_peer as realtime_peer


class Observations:
    def __init__(self):
        self.items = []
        self.changed = asyncio.Condition()

    async def read(self, voice):
        async for event in voice.events():
            async with self.changed:
                self.items.append(event)
                self.changed.notify_all()

    async def event(self, kind, **fields):
        def matches(event):
            return isinstance(event, kind) and all(
                getattr(event, key) == value for key, value in fields.items()
            )

        async with self.changed:
            await self.changed.wait_for(lambda: any(matches(event) for event in self.items))
            return next(event for event in self.items if matches(event))


@pytest.mark.parametrize("terminal", ["done", "cancelled"])
async def test_response_context_keeps_issuing_agent_after_voice_update(realtime_peer, terminal):
    peer = realtime_peer
    application = {"identity": object()}
    target = Agent(name="Target", instructions="Target source")
    seen = Observations()
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-events", supports_websockets=False
        ) as provider,
    ):
        original = Agent(name="Original", instructions="Original source", provider=provider)
        async with original.session(context=application) as session:
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
                reading = asyncio.create_task(seen.read(voice))
                try:
                    await peer.frame(lambda frame: frame["type"] == "session.update")
                    assert peer.socket is not None
                    await peer.socket.send_json(
                        {"type": "response.created", "response": {"id": "issued-A"}}
                    )
                    start = await seen.event(
                        RealtimeResponse, status="created", response_id="issued-A"
                    )
                    assert start.run_context.agent is original
                    assert start.run_context.context is application
                    await voice.update_agent(target)
                    await peer.socket.send_json(
                        {
                            "type": f"response.{terminal}",
                            "response": {"id": "issued-A", "output": []},
                        }
                    )
                    end = await seen.event(
                        RealtimeResponse, status=terminal, response_id="issued-A"
                    )
                    assert end.run_context.agent is original
                    assert end.run_context.context is application
                    await peer.socket.send_json(
                        {"type": "response.created", "response": {"id": "issued-B"}}
                    )
                    await peer.socket.send_json(
                        {"type": "response.done", "response": {"id": "issued-B", "output": []}}
                    )
                    fresh = await seen.event(
                        RealtimeResponse, status="done", response_id="issued-B"
                    )
                    assert fresh.run_context.agent is target
                    assert fresh.run_context.context is application
                finally:
                    await voice.aclose()
                    await reading
        assert end.run_context.agent is original
        assert fresh.run_context.agent is target
        assert end.run_context.context is application


async def test_tool_events_keep_admitted_identity_and_accepted_output_after_replacement(
    realtime_peer,
):
    peer = realtime_peer
    application = {"identity": object()}
    before_entered, release_before, body_entered, release_body = (asyncio.Event() for _ in range(4))
    seen = Observations()
    effects = []

    async def before(context):
        assert context.context is application
        before_entered.set()
        await release_before.wait()
        return Allow()

    def after(context, output):
        assert context.context is application
        return ToolOutput.text("accepted-original")

    @function_tool(name="lookup", before=before, after=after)
    async def old_lookup(key: str) -> str:
        body_entered.set()
        await release_body.wait()
        effects.append(key)
        return "untransformed-original"

    @function_tool(name="lookup")
    def fresh_lookup(key: str) -> str:
        effects.append(key)
        return "accepted-fresh"

    target = Agent(name="Fresh", instructions="Fresh source", tools=[fresh_lookup])
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-events", supports_websockets=False
        ) as provider,
    ):
        original = Agent(
            name="Original", instructions="Original source", tools=[old_lookup], provider=provider
        )
        async with original.session(context=application) as session:
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
                reading = asyncio.create_task(seen.read(voice))
                try:
                    await peer.frame(lambda frame: frame["type"] == "session.update")
                    await peer.function("lookup", {"key": "old"}, "old-call")
                    await before_entered.wait()
                    submitted = await seen.event(
                        RealtimeToolCall, call_id="old-call", status="started"
                    )
                    assert submitted.arguments == json.dumps({"key": "old"})
                    assert submitted.item_id == "item-old-call"
                    assert submitted.response_id == "response-old-call"
                    assert not any(
                        isinstance(event, RealtimeToolCall) and event.status == "executing"
                        for event in seen.items
                    )
                    release_before.set()
                    await body_entered.wait()
                    entered = await seen.event(
                        RealtimeToolCall, call_id="old-call", status="executing"
                    )
                    assert entered.tool is old_lookup
                    assert entered.run_context.agent is original
                    assert entered.run_context.context is application
                    await voice.update_agent(target)
                    await peer.function("lookup", {"key": "fresh"}, "fresh-call")
                    fresh = await seen.event(
                        RealtimeToolCall, call_id="fresh-call", status="completed"
                    )
                    assert fresh.tool is fresh_lookup
                    assert fresh.run_context.agent is target
                    assert fresh.output == "accepted-fresh"
                    release_body.set()
                    settled = await seen.event(
                        RealtimeToolCall, call_id="old-call", status="completed"
                    )
                    assert settled.tool is old_lookup
                    assert settled.run_context.agent is original
                    assert settled.run_context.context is application
                    assert settled.output == "accepted-original"
                    wire = await peer.frame(
                        lambda frame: frame.get("item", {}).get("call_id") == "old-call"
                    )
                    assert settled.output == wire["item"]["output"]
                    assert effects == ["fresh", "old"]
                finally:
                    release_before.set()
                    release_body.set()
                    await voice.aclose()
                    await reading
        assert settled.tool is old_lookup
        assert settled.run_context.agent is original


async def test_rejected_tool_event_reports_native_fallback_without_body_identity(realtime_peer):
    peer = realtime_peer
    seen = Observations()
    effects = []

    @function_tool(needs_approval=True)
    def risky() -> str:
        effects.append("forbidden")
        return "forbidden"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-events", supports_websockets=False
        ) as provider,
    ):
        original = Agent(instructions="Use risky.", tools=[risky], provider=provider)
        async with original.session() as session:
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
                reading = asyncio.create_task(seen.read(voice))
                try:
                    await peer.function("risky", {}, "denied-call")
                    request = await next_request(session.interactions(), risky)
                    await request.reject("Application declined")
                    failed = await seen.event(
                        RealtimeToolCall, call_id="denied-call", status="failed"
                    )
                    assert failed.output == "Application declined"
                    assert failed.tool is None
                    assert failed.run_context.agent is original
                    assert failed.arguments == "{}"
                    assert effects == []
                    assert not any(
                        isinstance(event, RealtimeToolCall) and event.status == "executing"
                        for event in seen.items
                    )
                finally:
                    await voice.aclose()
                    await reading

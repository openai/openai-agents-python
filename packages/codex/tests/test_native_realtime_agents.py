"""Contextual configuration and structured handoffs through native receipts."""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeAgentConfig, RealtimeConfig, realtime_handoff
from tests._asyncio import timeout
from tests.test_native_realtime import realtime_peer as realtime_peer


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_native_voice_resolves_contextual_instructions_and_tool_predicate(
    realtime_peer, asynchronous
):
    peer = realtime_peer
    application = {"language": "French", "enabled": True}
    observed = []
    effects = []

    @function_tool
    def visible() -> str:
        effects.append("visible")
        return "visible-result"

    @function_tool
    def hidden() -> str:
        raise AssertionError("Hidden tool cannot run")

    def resolve(context):
        observed.append(context)
        return "Speak " + context.context["language"]

    async def async_resolve(context):
        await asyncio.sleep(0)
        return resolve(context)

    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-contextual-voice-key",
            supports_websockets=False,
        ) as provider,
    ):
        agent = Agent(
            instructions="Backend instructions",
            provider=provider,
            tools=[visible, hidden],
            realtime=RealtimeAgentConfig(
                instructions=async_resolve if asynchronous else resolve,
                prompt={"id": "synthetic-voice-prompt"},
                tool_enabled=lambda context, tool: (
                    tool.name != "hidden" and context.context["enabled"]
                ),
            ),
        )
        async with agent.session(context=application) as session:
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                initial = await peer.frame(lambda frame: frame["type"] == "session.update")
                assert initial["session"]["instructions"] == "Speak French"
                assert initial["session"]["prompt"] == {"id": "synthetic-voice-prompt"}
                names = {tool["name"] for tool in initial["session"]["tools"]}
                assert "visible" in names and "hidden" not in names
                assert observed[0].context is application
                assert observed[0].agent is agent
                application["enabled"] = False
                await peer.function("visible", {}, "disabled-call")
                denied = await peer.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "disabled-call"
                )
                assert "Tool disabled by its source Agent" in denied["item"]["output"]
                assert effects == []
            assert agent.instructions == "Backend instructions"
            assert peer.responses.requests == []


@pytest.mark.parametrize("dataclass_input", [False, True])
async def test_native_custom_handoff_waits_for_callback_and_configuration_ack(
    realtime_peer, dataclass_input
):
    peer = realtime_peer
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    application = {"marker": "handoff-context"}

    class Transfer(BaseModel):
        reason: str

    @dataclass
    class DataclassTransfer:
        reason: str

    async def on_handoff(context, payload):
        calls.append((context, payload))
        entered.set()
        await release.wait()

    async def target_instructions(context):
        assert context.context is application
        return "Resolved target instructions"

    target = Agent(
        name="B",
        instructions="Backend target",
        realtime=RealtimeAgentConfig(instructions=target_instructions),
    )
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-structured-handoff-key",
            supports_websockets=False,
        ) as provider,
    ):
        source = Agent(
            name="A",
            instructions="Source",
            provider=provider,
            handoffs=[
                realtime_handoff(
                    target,
                    name="transfer_record",
                    description="Transfer with a reason",
                    input_type=DataclassTransfer if dataclass_input else Transfer,
                    on_handoff=on_handoff,
                )
            ],
        )
        async with (
            source.session(context=application) as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            initial = await peer.frame(lambda frame: frame["type"] == "session.update")
            declarations = [
                tool
                for tool in initial["session"]["tools"]
                if tool.get("name") == "transfer_record"
            ]
            assert len(declarations) == 1
            assert declarations[0]["description"] == "Transfer with a reason"
            assert "reason" in declarations[0]["parameters"]["properties"]
            start = len(peer.frames)
            peer.auto_ack_updates = False
            await peer.function("transfer_record", {"reason": "requested"}, "structured-transfer")
            await entered.wait()
            assert await session.get_active_agent() is source
            assert not any(frame["type"] == "session.update" for frame in peer.frames[start:])
            assert calls[0][0].context is application
            assert calls[0][0].agent is source
            assert calls[0][1].reason == "requested"
            release.set()
            updated = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
            assert updated["session"]["instructions"] == "Resolved target instructions"
            await voice.send_audio(b"\0\0")
            await peer.frame(
                lambda frame: frame["type"] == "input_audio_buffer.append", after=start
            )
            assert not any(frame["type"] == "response.create" for frame in peer.frames[start:])
            peer.auto_ack_updates = True
            await peer.ack_next_update()
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            assert await session.get_active_agent() is target
            assert len(calls) == 1


async def test_live_voice_graph_keeps_issued_callback_and_root_checkpoint(realtime_peer):
    peer = realtime_peer
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @function_tool
    async def old_lookup() -> str:
        entered.set()
        await release.wait()
        effects.append("old-source")
        return "old-source-result"

    @function_tool
    async def fresh_lookup() -> str:
        effects.append("new-source")
        return "new-source-result"

    target = Agent(
        name="Fresh",
        instructions="Fresh voice source",
        tools=[fresh_lookup],
        realtime=RealtimeAgentConfig(instructions=lambda _: "Fresh voice instructions"),
    )
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-live-source-key",
            supports_websockets=False,
        ) as provider,
    ):
        original = Agent(
            name="Original", instructions="Original backend", tools=[old_lookup], provider=provider
        )
        async with original.session() as session:
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice:
                await peer.frame(lambda frame: frame["type"] == "session.update")
                await peer.function("old_lookup", {}, "issued-before-update")
                await entered.wait()
                start = len(peer.frames)
                await voice.update_agent(target)
                updated = await peer.frame(
                    lambda frame: frame["type"] == "session.update", after=start
                )
                assert updated["session"]["instructions"] == "Fresh voice instructions"
                offered = {tool["name"] for tool in updated["session"]["tools"]}
                assert "fresh_lookup" in offered and "old_lookup" not in offered
                assert await voice.get_active_agent() is target
                assert await session.get_active_agent() is original
                await peer.function("fresh_lookup", {}, "issued-after-update")
                fresh = await peer.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "issued-after-update"
                )
                assert fresh["item"]["output"] == "new-source-result"
                release.set()
                old = await peer.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "issued-before-update"
                )
                assert old["item"]["output"] == "old-source-result"
                assert effects == ["new-source", "old-source"]
            assert not session._voice_profiles
            saved = await session.save_state()
        async with original.session(state=saved) as restored:
            assert await restored.get_active_agent() is original
            assert not restored._voice_profiles

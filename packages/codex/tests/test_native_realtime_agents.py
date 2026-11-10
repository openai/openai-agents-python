"""Contextual configuration and structured handoffs through native receipts."""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeAgentConfig, RealtimeConfig, RealtimeSession, realtime_handoff
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


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("target_instructions", ["Target backend", ""])
async def test_voice_agent_change_uses_destination_instructions(
    realtime_peer, handoff, target_instructions
):
    peer = realtime_peer
    target = Agent(name="Target", instructions=target_instructions)
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-voice-instructions-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="Source",
            instructions="Source backend",
            realtime=RealtimeAgentConfig(instructions=lambda _: "Source voice instructions"),
            provider=provider,
            handoffs=[realtime_handoff(target, name="switch_agent")],
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        assert initial["session"]["instructions"] == "Source voice instructions"
        start = len(peer.frames)
        if handoff:
            await peer.function("switch_agent", {}, "clear-voice-instructions")
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
        else:
            await voice.update_agent(target)
        updated = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        assert updated["session"]["instructions"] == target_instructions
        assert await voice.get_active_agent() is target


async def test_conflicting_handoffs_in_one_response_invoke_only_the_first_target(realtime_peer):
    peer = realtime_peer
    effects = []

    class Transfer(BaseModel):
        marker: str

    async def on_handoff(_, payload):
        effects.append(payload.marker)
        await asyncio.sleep(0)

    target_a = Agent(
        name="A",
        instructions="Backend A",
        realtime=RealtimeAgentConfig(instructions="Voice target A"),
    )
    target_b = Agent(
        name="B",
        instructions="Backend B",
        realtime=RealtimeAgentConfig(instructions="Voice target B"),
    )
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-voice-handoff-reservation-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="Source",
            instructions="Source backend",
            provider=provider,
            handoffs=[
                realtime_handoff(target_a, name="to_a", input_type=Transfer, on_handoff=on_handoff),
                realtime_handoff(target_b, name="to_b", input_type=Transfer, on_handoff=on_handoff),
            ],
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        await peer.functions(
            [("to_a", {"marker": "A"}, "handoff-a"), ("to_b", {"marker": "B"}, "handoff-b")],
            response_id="conflicting-handoffs",
        )
        outputs = {}
        for marker in ("a", "b"):
            call_id = f"handoff-{marker}"
            result = await peer.frame(
                lambda frame, call_id=call_id: frame.get("item", {}).get("call_id") == call_id,
                after=start,
            )
            outputs[marker.upper()] = result["item"]["output"]
        updated = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
        assert effects == ["A"]
        assert outputs["A"] == "Handoff reserved."
        assert outputs["B"] == "Multiple handoffs detected, ignoring this one."
        assert updated["session"]["instructions"] == "Voice target A"
        assert await voice.get_active_agent() is target_a


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


@pytest.mark.parametrize("raw", [False, True])
async def test_acknowledged_session_overrides_survive_voice_agent_change(realtime_peer, raw):
    peer = realtime_peer
    target = Agent(
        name="Target",
        instructions="Target backend",
        realtime=RealtimeAgentConfig(
            instructions="Target instructions", prompt={"id": "target-prompt"}
        ),
    )
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-voice-overrides-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="Opening", instructions="Opening backend", provider=provider
        ).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                instructions="Opening instructions",
                settings={"prompt": {"id": "opening-prompt"}},
            )
        ) as voice,
    ):
        instructions = "" if raw else "Accepted voice instructions"
        prompt = None if raw else {"id": "accepted-prompt"}
        peer.auto_ack_updates = False
        start = len(peer.frames)
        updating = asyncio.create_task(
            voice.send_event(
                {
                    "type": "session.update",
                    "session": {"instructions": instructions, "prompt": prompt},
                }
            )
            if raw
            else voice.update({"instructions": instructions, "prompt": prompt})
        )
        submitted = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        pending_before_ack = not updating.done()
        await peer.ack_next_update()
        await updating
        assert submitted["session"]["instructions"] == instructions
        assert submitted["session"]["prompt"] == prompt
        assert pending_before_ack
        peer.auto_ack_updates = True
        start = len(peer.frames)
        await voice.update_agent(target)
        refreshed = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        assert refreshed["session"]["instructions"] == instructions
        assert refreshed["session"]["prompt"] == prompt
        assert await voice.get_active_agent() is target


async def test_failed_configuration_does_not_exhaust_voice_agent_graphs(realtime_peer, monkeypatch):
    peer = realtime_peer
    monkeypatch.setattr(RealtimeSession, "_HANDOFF_LIMIT", 3)

    @function_tool
    def retained_lookup() -> str:
        return "retained-result"

    def fail(_):
        raise RuntimeError("Configuration failed")

    accepted = Agent(name="Accepted", instructions="Keep me", tools=[retained_lookup])
    child = Agent(name="Child", instructions="Unused handoff target")
    rejected = Agent(
        name="Rejected",
        instructions="Never published",
        realtime=RealtimeAgentConfig(instructions=fail),
        handoffs=[realtime_handoff(child)],
    )
    replacement = Agent(name="Replacement", instructions="Valid replacement")
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-voice-configuration-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Opening backend", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        await voice.update_agent(accepted)
        start = len(peer.frames)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="^Configuration failed$"):
                await voice.update_agent(rejected)
            assert await voice.get_active_agent() is accepted
        assert all(frame["type"] != "session.update" for frame in peer.frames[start:])

        await peer.function("retained_lookup", {}, "after-rejected-graphs")
        result = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "after-rejected-graphs"
        )
        assert result["item"]["output"] == "retained-result"
        await voice.update_agent(replacement)
        assert await voice.get_active_agent() is replacement


async def test_agent_configuration_uses_override_accepted_during_callback(realtime_peer):
    peer = realtime_peer
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(_):
        entered.set()
        await release.wait()
        return "Resolved instructions"

    target = Agent(
        name="Target",
        instructions="Target backend",
        realtime=RealtimeAgentConfig(instructions=resolve),
    )
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-voice-overlap-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Opening backend", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        refreshing = asyncio.create_task(voice.update_agent(target))
        try:
            await entered.wait()
            await voice.update({"instructions": "Accepted during callback"})
            start = len(peer.frames)
        finally:
            release.set()
        await refreshing
        refreshed = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        assert refreshed["session"]["instructions"] == "Accepted during callback"

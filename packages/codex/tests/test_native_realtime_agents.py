"""Contextual configuration and structured handoffs through native receipts."""

import asyncio
import json
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    Allow,
    DecisionRequest,
    DecisionResponse,
    NativeProvider,
    ToolContext,
    function_tool,
)
from agents.observation import AgentProfileChanged
from agents.realtime import RealtimeAgentConfig, RealtimeConfig, RealtimeSession, realtime_handoff
from tests._asyncio import timeout
from tests.native_fixtures import function_call, text_response
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


@pytest.mark.parametrize("handoff", [False, True])
async def test_native_voice_rechecks_source_predicate_after_dispatch_hook(realtime_peer, handoff):
    peer = realtime_peer
    entered, release = asyncio.Event(), asyncio.Event()
    application = {"enabled": True}
    effects = []
    armed = False

    async def wait_for_policy(_):
        if armed:
            entered.set()
            await release.wait()
        return True

    async def before(context):
        await wait_for_policy(context)
        return Allow()

    @function_tool(before=before)
    def apply_change() -> str:
        effects.append("function")
        return "applied"

    def on_handoff(_):
        effects.append("handoff")

    target = Agent(name="Destination", instructions="Destination instructions")
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-realtime-dispatch-key",
            supports_websockets=False,
        ) as provider,
    ):
        source = Agent(
            name="Source",
            instructions="Source instructions",
            provider=provider,
            tools=[] if handoff else [apply_change],
            handoffs=[
                realtime_handoff(
                    target,
                    name="apply_change",
                    is_enabled=wait_for_policy,
                    on_handoff=on_handoff,
                )
            ]
            if handoff
            else [],
            realtime=RealtimeAgentConfig(
                tool_enabled=lambda context, tool: context.context["enabled"]
            ),
        )
        async with (
            source.session(context=application) as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")),
        ):
            initial = await peer.frame(lambda frame: frame["type"] == "session.update")
            assert "apply_change" in {tool["name"] for tool in initial["session"]["tools"]}
            armed = True
            await peer.function("apply_change", {}, "revoked")
            await entered.wait()
            application["enabled"] = False
            release.set()
            await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "revoked")
            assert effects == []
            assert await session.get_active_agent() is source

            application["enabled"] = True
            await peer.function("apply_change", {}, "allowed")
            await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "allowed")
            assert effects == ["handoff" if handoff else "function"]


@pytest.mark.parametrize(
    ("revoked_grant", "denial_reason"),
    [("handoff", "This handoff is disabled."), ("source", "Tool disabled by its source Agent")],
)
async def test_native_voice_rechecks_handoff_grants_after_awaited_policy(
    realtime_peer, revoked_grant, denial_reason
):
    peer = realtime_peer
    policy_waiting, release = asyncio.Event(), asyncio.Event()
    application = {"handoff": True, "source": True}
    effects = []
    armed = False
    handoff_checks = 0

    def handoff_enabled(context):
        nonlocal handoff_checks
        if armed:
            handoff_checks += 1
        if revoked_grant == "source" and handoff_checks == 2:

            async def check_after_release():
                policy_waiting.set()
                await release.wait()
                return context.context["handoff"]

            return check_after_release()
        return context.context["handoff"]

    async def source_enabled(context, _tool):
        if revoked_grant == "handoff" and handoff_checks:
            policy_waiting.set()
            await release.wait()
        return context.context["source"]

    def on_handoff(_):
        effects.append("handoff")

    target = Agent(name="Destination", instructions="Destination instructions")
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-realtime-handoff-grant-key",
            supports_websockets=False,
        ) as provider,
    ):
        source = Agent(
            name="Source",
            instructions="Source instructions",
            provider=provider,
            handoffs=[
                realtime_handoff(
                    target,
                    name="transfer",
                    is_enabled=handoff_enabled,
                    on_handoff=on_handoff,
                )
            ],
            realtime=RealtimeAgentConfig(tool_enabled=source_enabled),
        )
        async with (
            source.session(context=application) as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")),
        ):
            initial = await peer.frame(lambda frame: frame["type"] == "session.update")
            assert "transfer" in {tool["name"] for tool in initial["session"]["tools"]}
            armed = True
            await peer.function("transfer", {}, "revoked-handoff")
            await policy_waiting.wait()
            application[revoked_grant] = False
            release.set()
            denied = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "revoked-handoff"
            )
            assert effects == []
            assert denial_reason in denied["item"]["output"]
            assert await session.get_active_agent() is source

            application[revoked_grant] = True
            await peer.function("transfer", {}, "allowed-handoff")
            await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "allowed-handoff"
            )
            assert effects == ["handoff"]


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
            reader = session.events()
            try:
                async for event in reader:
                    if isinstance(event, AgentProfileChanged):
                        assert event.agent_name == "B"
                        assert event.previous_agent_name == "A"
                        break
            finally:
                await reader.aclose()


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


@pytest.mark.parametrize("setting", ["instructions", "model"])
async def test_replaced_voice_agent_preserves_backend_settings_updates(realtime_peer, setting):
    peer = realtime_peer
    peer.responses.respond(*text_response("backend-updated", "Updated backend"))
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
    ):
        source = Agent(instructions="Opening backend instructions", provider=provider)
        target = Agent(instructions="Voice instructions")
        async with (
            source.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            await voice.update_agent(target)
            if setting == "instructions":
                await session.update(instructions="Updated backend instructions")
            else:
                await session.update(model="gpt-5.5")
            assert await voice.get_active_agent() is target
            assert await session.get_active_agent() is source

            turn = await session.submit("Use the updated backend settings")
            assert (await turn.get_result()).final_output == "Updated backend"
            request = peer.responses.requests[-1]
            if setting == "instructions":
                assert "Updated backend instructions" in json.dumps(request)
            else:
                assert request["model"] == "gpt-5.5"


async def test_backend_sync_joins_before_concurrent_voice_agent_update(realtime_peer):
    peer = realtime_peer
    syncing = asyncio.Event()
    release_sync = asyncio.Event()
    order = []
    block_sync = False

    async def first_instructions(_context):
        if block_sync:
            syncing.set()
            await release_sync.wait()
            order.append("first synchronized")
        return "First voice instructions"

    def next_instructions(_context):
        order.append("next configured")
        return "Next voice instructions"

    peer.responses.respond(*text_response("backend-updated", "Updated backend"))
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
    ):
        source = Agent(instructions="Opening backend instructions", provider=provider)
        first = Agent(
            instructions="First",
            realtime=RealtimeAgentConfig(instructions=first_instructions),
        )
        next_agent = Agent(
            instructions="Next",
            realtime=RealtimeAgentConfig(instructions=next_instructions),
        )
        async with (
            source.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            await voice.update_agent(first)
            block_sync = True
            backend_update = asyncio.create_task(
                session.update(instructions="Updated backend instructions")
            )
            await syncing.wait()
            replacement = asyncio.create_task(voice.update_agent(next_agent))
            try:
                await asyncio.wait((replacement,), timeout=1)
            finally:
                release_sync.set()
            await asyncio.gather(backend_update, replacement)

            assert order == ["first synchronized", "next configured"]
            assert await voice.get_active_agent() is next_agent
            turn = await session.submit("Use the updated backend settings")
            assert (await turn.get_result()).final_output == "Updated backend"
            assert "Updated backend instructions" in json.dumps(peer.responses.requests[-1])


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_close_cancels_voice_agent_configuration(realtime_peer, cancel_waiter):
    peer = realtime_peer
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def resolve_instructions(_context):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "Replacement voice instructions"

    target = Agent(
        instructions="Replacement backend instructions",
        realtime=RealtimeAgentConfig(instructions=resolve_instructions),
    )
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
    ):
        source = Agent(instructions="Backend instructions", provider=provider)
        async with (
            source.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            update = asyncio.create_task(voice.update_agent(target))
            closing = None
            try:
                await entered.wait()
                if cancel_waiter:
                    update.cancel()
                    await asyncio.sleep(0)
                    assert not update.done()
                    assert not cancelled.is_set()

                closing = asyncio.create_task(voice.aclose())
                done, _ = await asyncio.wait((closing,), timeout=5)
                assert closing in done, "Voice close waited for the Agent configuration callback"
                await closing
                assert cancelled.is_set()
                with pytest.raises(asyncio.CancelledError):
                    await update
                assert await session.get_active_agent() is source
            finally:
                release.set()
                await asyncio.gather(update, return_exceptions=True)
                if closing is not None:
                    await asyncio.gather(closing, return_exceptions=True)


async def test_session_close_retires_replacement_voice_agent(realtime_peer):
    peer = realtime_peer
    replacement = Agent(instructions="Replacement voice instructions")
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
        Agent(instructions="Opening backend", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        await voice.update_agent(replacement)
        assert await voice.get_active_agent() is replacement
        await session.aclose()
        await voice.aclose()


@pytest.mark.parametrize("close_voice", [False, True])
async def test_replaced_voice_agent_preserves_backend_tool_replacement(realtime_peer, close_voice):
    peer = realtime_peer

    @function_tool(name="lookup")
    def original() -> str:
        raise AssertionError("The original backend tool should have been replaced")

    @function_tool(name="lookup")
    def replacement() -> str:
        return "updated backend tool"

    peer.responses.respond(function_call("lookup", {}, "updated-lookup"))
    peer.responses.respond(*text_response("backend-tool-updated", "Updated backend"))
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url, api_key="test-key", supports_websockets=False
        ) as provider,
        Agent(
            instructions="Opening backend", tools=[original], provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        await voice.update_agent(Agent(instructions="Voice instructions"))
        if close_voice:
            await voice.aclose()
        await session.update(tools=[replacement])
        turn = await session.submit("Call the updated backend tool")
        assert (await turn.get_result()).final_output == "Updated backend"
        assert "updated backend tool" in json.dumps(peer.responses.requests[-1]["input"])


@pytest.mark.parametrize("with_provider", [False, True])
async def test_updated_voice_tool_uses_its_source_decision_provider(realtime_peer, with_provider):
    peer = realtime_peer

    @dataclass
    class Decisions:
        source: str

        async def get_decision(self, request: DecisionRequest) -> DecisionResponse:
            return DecisionResponse(value=self.source)

    @function_tool
    async def choose_source(context: ToolContext[None]) -> str:
        return await context.decide(
            "Which Agent issued the call?", input="voice tool", choices=("opening", "voice")
        )

    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-voice-decision-key",
            supports_websockets=False,
        ) as provider,
    ):
        source = Agent(
            name="Opening",
            instructions="Opening Agent",
            provider=provider,
            decisions=Decisions("opening"),
        )
        target = Agent(
            name="Voice",
            instructions="Voice Agent",
            tools=[choose_source],
            decisions=Decisions("voice") if with_provider else None,
        )
        async with (
            source.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            await voice.update_agent(target)
            await peer.function("choose_source", {}, "voice-source-decision")
            result = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "voice-source-decision"
            )
            assert result["item"]["output"] == ("voice" if with_provider else "opening")


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


async def test_global_voice_policy_keeps_target_source_and_handoff_kind(realtime_peer):
    peer = realtime_peer
    instructions_seen = []
    tools_seen = []
    defaults_seen = []
    effects = []

    def agent_defaults(context):
        defaults_seen.append(context.agent.name)
        return "Agent default"

    async def global_instructions(context):
        await asyncio.sleep(0)
        instructions_seen.append(context.agent.name)
        return "Global " + context.agent.name

    def global_tools(context, tool):
        tools_seen.append((context.agent.name, tool.type, tool.name))
        return tool.type == "handoff"

    @function_tool
    def ordinary() -> str:
        raise AssertionError("Global policy excludes ordinary functions")

    destination = Agent(
        name="C",
        instructions="Backend C",
        realtime=RealtimeAgentConfig(instructions=agent_defaults),
    )
    target = Agent(
        name="B",
        instructions="Backend B",
        tools=[ordinary],
        realtime=RealtimeAgentConfig(
            instructions=agent_defaults,
            prompt={"id": "target-prompt"},
        ),
        handoffs=[
            realtime_handoff(
                destination,
                name="transfer_to_c",
                on_handoff=lambda _: effects.append("handoff"),
            )
        ],
    )
    next_target = Agent(
        name="D",
        instructions="Backend D",
        realtime=RealtimeAgentConfig(instructions=agent_defaults),
    )
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-composed-voice-policy",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="A",
            instructions="Backend A",
            provider=provider,
            realtime=RealtimeAgentConfig(instructions=agent_defaults),
        ).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                instructions=global_instructions,
                is_tool_enabled=global_tools,
            )
        ) as voice,
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        assert initial["session"]["instructions"] == "Global A"
        start = len(peer.frames)
        await voice.update_agent(target)
        updated = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        assert updated["session"]["instructions"] == "Global B"
        assert updated["session"]["prompt"] == {"id": "target-prompt"}
        names = {tool["name"] for tool in updated["session"]["tools"]}
        assert "transfer_to_c" in names and "ordinary" not in names
        assert ("B", "handoff", "transfer_to_c") in tools_seen
        assert ("B", "function", "ordinary") in tools_seen
        start = len(peer.frames)
        await peer.function("transfer_to_c", {}, "composed-handoff")
        await peer.frame(
            lambda frame: (
                frame["type"] == "session.update"
                and frame["session"].get("instructions") == "Global C"
            ),
            after=start,
        )
        assert effects == ["handoff"]
        handoff_checks = [kind for name, kind, tool in tools_seen if tool == "transfer_to_c"]
        assert len(handoff_checks) >= 2 and set(handoff_checks) == {"handoff"}
        assert instructions_seen == ["A", "B", "C"]
        await voice.update({"instructions": "Explicit override"})
        start = len(peer.frames)
        await voice.update_agent(next_target)
        explicit = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        assert explicit["session"]["instructions"] == "Explicit override"
        assert await voice.get_active_agent() is next_target
        assert instructions_seen == ["A", "B", "C"]
        assert defaults_seen == []
        assert peer.responses.requests == []


@pytest.mark.parametrize("updated", [False, True], ids=["opening", "updated"])
async def test_session_output_guard_covers_backend_after_voice_update(realtime_peer, updated):
    from agents import GuardrailResult, OutputGuardrail

    peer = realtime_peer
    checked = []

    def check(completion, agent):
        checked.append((completion.final_output, agent.name))
        return GuardrailResult(False)

    peer.responses.respond(*text_response("guarded-backend", "guarded-result"))
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-global-voice-output-guard",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="A" if updated else "B",
            instructions="Root",
            provider=provider,
        ).session(output_guardrails=(OutputGuardrail("global-voice", check),)) as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        target = Agent(name="B", instructions="Target")
        if updated:
            await voice.update_agent(target)
        backend = await session.get_active_agent()
        await peer.handoff("Complete the guarded backend task", call_id="voice-global-guard")
        result = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "voice-global-guard"
        )
        assert result["item"]["output"] == "guarded-result"
        assert checked == [("guarded-result", backend.name)]
        if updated:
            assert await voice.get_active_agent() is target
        assert len(peer.responses.requests) == 1


async def test_native_structured_handoff_interrupt_retires_callback_without_target(realtime_peer):
    peer = realtime_peer
    entered, release, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects = []

    class Transfer(BaseModel):
        reason: str

    async def on_handoff(context, payload):
        assert context.agent is source
        assert payload.reason == "requested"
        effects.append("entered")
        entered.set()
        try:
            await release.wait()
            effects.append("accepted")
        finally:
            retired.set()

    target = Agent(name="B", instructions="Target voice instructions")
    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-interrupted-handoff-key",
            supports_websockets=False,
        ) as provider,
    ):
        source = Agent(
            name="A",
            instructions="Source voice instructions",
            provider=provider,
            handoffs=[
                realtime_handoff(
                    target,
                    name="transfer_record",
                    input_type=Transfer,
                    on_handoff=on_handoff,
                )
            ],
        )
        async with (
            source.session() as session,
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        ):
            try:
                await peer.frame(lambda frame: frame["type"] == "session.update")
                await peer.function(
                    "transfer_record", {"reason": "requested"}, "cancelled-transfer"
                )
                await entered.wait()
                start = len(peer.frames)
                await voice.interrupt("response-cancelled-transfer")
                await retired.wait()
                release.set()
                await voice.send_text("Continue with the source")
                await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
                assert effects == ["entered"]
                assert await voice.get_active_agent() is source
                assert await session.get_active_agent() is source
                assert not any(
                    frame["type"] == "session.update"
                    and "Target voice instructions" in frame["session"].get("instructions", "")
                    for frame in peer.frames[start:]
                )
            finally:
                release.set()

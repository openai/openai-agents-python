from __future__ import annotations

import asyncio

import pytest

from agents import GuardrailFunctionOutput, ToolGuardrailFunctionOutput, ToolInputGuardrailData
from agents.decorators import output_guardrail, tool, tool_input_guardrail
from agents.realtime import (
    AssistantMessageItem,
    AssistantText,
    InputAudio,
    RealtimeAgent,
    RealtimeGuardrailTripped,
    RealtimeHandoffEvent,
    RealtimeHistoryAdded,
    RealtimeHistoryUpdated,
    RealtimeModelUsageEvent,
    RealtimeRawModelEvent,
    RealtimeRunner,
    RealtimeToolApprovalRequired,
    UserMessageItem,
)

pytestmark = pytest.mark.realtime


async def test_realtime_text_session_completes_and_updates_history(
    integration_realtime_model: str,
) -> None:
    agent = RealtimeAgent(
        name="Packaged realtime agent",
        instructions="Reply with exactly REALTIME_READY.",
    )
    runner = RealtimeRunner(agent)
    observed_events: list[str] = []
    assistant_text: list[str] = []

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:
        await session.send_message("Confirm the realtime connection.")

        async def receive() -> None:
            async for event in session:
                observed_events.append(event.type)
                if isinstance(event, RealtimeHistoryAdded | RealtimeHistoryUpdated):
                    items = (
                        [event.item] if isinstance(event, RealtimeHistoryAdded) else event.history
                    )
                    for item in items:
                        if not isinstance(item, AssistantMessageItem):
                            continue
                        assistant_text.extend(
                            content.text
                            for content in item.content
                            if isinstance(content, AssistantText) and content.text
                        )
                if event.type == "agent_end":
                    return

        await asyncio.wait_for(receive(), timeout=45)

    assert "agent_start" in observed_events
    assert "agent_end" in observed_events
    assert any("REALTIME_READY" in text for text in assistant_text)


async def test_realtime_function_tool_emits_start_and_end_events(
    integration_realtime_model: str,
) -> None:
    calls: list[str] = []

    @tool
    def lookup_city(city: str) -> str:
        """Return a deterministic city status."""
        calls.append(city)
        return "sunny"

    agent = RealtimeAgent(
        name="Packaged realtime tool agent",
        instructions="Call lookup_city with Tokyo, then reply with TOKYO_SUNNY.",
        tools=[lookup_city],
    )
    runner = RealtimeRunner(agent)
    observed_events: list[str] = []

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:
        await session.send_message("What is the weather in Tokyo? Use the function tool.")

        async def receive() -> None:
            async for event in session:
                observed_events.append(event.type)
                if event.type == "agent_end" and "tool_end" in observed_events:
                    return

        await asyncio.wait_for(receive(), timeout=60)

    assert calls == ["Tokyo"]
    assert "tool_start" in observed_events
    assert "tool_end" in observed_events


async def test_realtime_session_preserves_history_across_text_turns(
    integration_realtime_model: str,
) -> None:
    agent = RealtimeAgent(
        name="Packaged realtime conversation agent",
        instructions="Remember user-provided verification words and answer concisely.",
    )
    runner = RealtimeRunner(agent)
    assistant_text: list[str] = []

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:

        async def receive_turn() -> None:
            async for event in session:
                if isinstance(event, RealtimeHistoryAdded | RealtimeHistoryUpdated):
                    items = (
                        [event.item] if isinstance(event, RealtimeHistoryAdded) else event.history
                    )
                    for item in items:
                        if isinstance(item, AssistantMessageItem):
                            assistant_text.extend(
                                content.text
                                for content in item.content
                                if isinstance(content, AssistantText) and content.text
                            )
                if event.type == "agent_end":
                    return

        await session.send_message("Remember the verification word SIERRA. Reply only STORED.")
        await asyncio.wait_for(receive_turn(), timeout=45)
        await session.send_message("What was the verification word? Reply only with that word.")
        await asyncio.wait_for(receive_turn(), timeout=45)

    assert any("SIERRA" in text.upper() for text in assistant_text)


async def test_realtime_usage_events_accumulate_once_per_completed_turn(
    integration_realtime_model: str,
) -> None:
    agent = RealtimeAgent(
        name="Packaged realtime usage agent",
        instructions="Reply with exactly REALTIME_USAGE_READY.",
    )
    runner = RealtimeRunner(agent)
    observed_usage: list[int] = []
    completed_totals: list[int] = []

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:

        async def receive_turn() -> None:
            async for event in session:
                if isinstance(event, RealtimeRawModelEvent) and isinstance(
                    event.data, RealtimeModelUsageEvent
                ):
                    observed_usage.append(event.data.usage.total_tokens)
                if event.type == "agent_end":
                    completed_totals.append(event.info.context.usage.total_tokens)
                    return

        await session.send_message("Confirm realtime usage for turn one.")
        await asyncio.wait_for(receive_turn(), timeout=45)
        await session.send_message("Confirm realtime usage for turn two.")
        await asyncio.wait_for(receive_turn(), timeout=45)

    assert len(observed_usage) == 2
    assert all(value > 0 for value in observed_usage)
    assert completed_totals == [observed_usage[0], sum(observed_usage)]


async def test_realtime_handoff_updates_the_active_agent(
    integration_realtime_model: str,
) -> None:
    specialist = RealtimeAgent(
        name="Packaged realtime specialist",
        instructions="Reply with exactly REALTIME_HANDOFF_READY.",
    )
    coordinator = RealtimeAgent(
        name="Packaged realtime coordinator",
        instructions="Immediately transfer to the packaged realtime specialist.",
        handoffs=[specialist],
    )
    runner = RealtimeRunner(coordinator)
    handoffs: list[RealtimeHandoffEvent] = []
    ended_agents: list[str] = []

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:
        await session.send_message("Transfer me to the specialist.")

        async def receive() -> None:
            async for event in session:
                if isinstance(event, RealtimeHandoffEvent):
                    handoffs.append(event)
                if event.type == "agent_end":
                    ended_agents.append(event.agent.name)
                    if event.agent is specialist:
                        return

        await asyncio.wait_for(receive(), timeout=60)

    assert len(handoffs) == 1
    assert handoffs[0].from_agent is coordinator
    assert handoffs[0].to_agent is specialist
    assert specialist.name in ended_agents


async def test_realtime_update_agent_replaces_instructions_and_tool_dispatch(
    integration_realtime_model: str,
) -> None:
    calls: list[str] = []

    @tool
    def replacement_checkpoint(checkpoint: str) -> str:
        """Resolve the replacement agent's release checkpoint."""
        calls.append(checkpoint)
        return "REALTIME_UPDATED_READY"

    initial = RealtimeAgent(
        name="Packaged initial realtime agent",
        instructions="Reply only INITIAL_AGENT_ACTIVE.",
    )
    replacement = RealtimeAgent(
        name="Packaged replacement realtime agent",
        instructions=(
            "Call replacement_checkpoint with checkpoint='updated', "
            "then reply exactly REALTIME_UPDATED_READY."
        ),
        tools=[replacement_checkpoint],
    )
    runner = RealtimeRunner(initial, config={"async_tool_calls": False})
    ended_agents: list[str] = []

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:
        await session.update_agent(replacement)
        await session.send_message(
            "You must call replacement_checkpoint with checkpoint='updated'. "
            "Do not reply before calling the function."
        )

        async def receive() -> None:
            async for event in session:
                if event.type == "agent_end":
                    ended_agents.append(event.agent.name)
                    if event.agent is replacement:
                        if not calls and len(ended_agents) == 1:
                            await session.send_message(
                                "Call replacement_checkpoint now with checkpoint='updated'."
                            )
                            continue
                        return

        await asyncio.wait_for(receive(), timeout=60)

    assert calls == ["updated"]
    assert ended_agents
    assert all(name == replacement.name for name in ended_agents)


@pytest.mark.nightly
@pytest.mark.parametrize("approved", [False, True], ids=["rejected", "approved"])
async def test_realtime_function_tool_approval_controls_side_effects(
    integration_realtime_model: str,
    approved: bool,
) -> None:
    calls: list[str] = []
    approvals: list[str] = []

    @tool(needs_approval=True)
    def publish_checkpoint(checkpoint: str) -> str:
        """Publish a release checkpoint only after approval."""
        calls.append(checkpoint)
        return "REALTIME_APPROVED"

    agent = RealtimeAgent(
        name="Packaged realtime approval agent",
        instructions=(
            "Call publish_checkpoint with checkpoint='release'. If it succeeds reply "
            "REALTIME_APPROVED. If it is rejected reply REALTIME_REJECTED."
        ),
        tools=[publish_checkpoint],
    )
    runner = RealtimeRunner(agent)

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:
        await session.send_message("Publish the release checkpoint with the tool.")

        async def receive() -> None:
            async for event in session:
                if isinstance(event, RealtimeToolApprovalRequired):
                    approvals.append(event.call_id)
                    if approved:
                        await session.approve_tool_call(event.call_id)
                    else:
                        await session.reject_tool_call(
                            event.call_id,
                            rejection_message="Publishing the checkpoint was rejected.",
                        )
                if approved and event.type == "tool_end" and approvals:
                    return
                if not approved and event.type == "agent_end" and approvals:
                    return

        await asyncio.wait_for(receive(), timeout=60)

    assert len(approvals) == 1
    assert calls == (["release"] if approved else [])


@pytest.mark.nightly
async def test_realtime_accepts_committed_pcm_audio_input(
    integration_realtime_model: str, integration_pcm_audio: bytes
) -> None:
    agent = RealtimeAgent(
        name="Packaged realtime audio input agent",
        instructions="Respond to the user's speech with exactly REALTIME_AUDIO_READY.",
    )
    runner = RealtimeRunner(agent)
    assistant_text: list[str] = []
    received_audio = False

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:
        await session.send_audio(integration_pcm_audio, commit=True)
        await session.send_message("Respond to the committed user audio.")

        async def receive() -> None:
            nonlocal received_audio
            async for event in session:
                if isinstance(event, RealtimeHistoryAdded | RealtimeHistoryUpdated):
                    items = (
                        [event.item] if isinstance(event, RealtimeHistoryAdded) else event.history
                    )
                    for item in items:
                        if isinstance(item, UserMessageItem):
                            received_audio = received_audio or any(
                                isinstance(content, InputAudio) for content in item.content
                            )
                        if isinstance(item, AssistantMessageItem):
                            assistant_text.extend(
                                content.text
                                for content in item.content
                                if isinstance(content, AssistantText) and content.text
                            )
                if event.type == "agent_end":
                    return

        await asyncio.wait_for(receive(), timeout=60)

    assert received_audio
    assert any("REALTIME_AUDIO_READY" in text for text in assistant_text)


@pytest.mark.nightly
async def test_realtime_output_guardrails_interrupt_audio_transcripts(
    integration_realtime_model: str,
) -> None:
    inspected: list[str] = []

    @output_guardrail
    async def reject_release_output(
        _context: object, _agent: object, text: str
    ) -> GuardrailFunctionOutput:
        inspected.append(text)
        return GuardrailFunctionOutput(output_info={"blocked": True}, tripwire_triggered=True)

    agent = RealtimeAgent(
        name="Packaged guarded realtime output agent",
        instructions="Reply with exactly BLOCKED_RELEASE_CONTENT.",
        output_guardrails=[reject_release_output],
    )
    runner = RealtimeRunner(
        agent,
        config={
            "output_guardrails": [reject_release_output],
            "guardrails_settings": {"debounce_text_length": 5},
        },
    )
    tripped: list[RealtimeGuardrailTripped] = []

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["audio"],
            }
        }
    ) as session:
        await session.send_message("Return the blocked release content.")

        async def receive() -> None:
            async for event in session:
                if isinstance(event, RealtimeGuardrailTripped):
                    tripped.append(event)
                    return

        await asyncio.wait_for(receive(), timeout=45)

    assert len(tripped) == 1
    assert len(inspected) == 1
    assert tripped[0].message == inspected[0]
    assert tripped[0].guardrail_results[0].output.tripwire_triggered


@pytest.mark.nightly
@pytest.mark.parametrize("pre_approval", [False, True], ids=["after-approval", "before-approval"])
async def test_realtime_tool_input_guardrails_control_approval_and_execution(
    integration_realtime_model: str,
    pre_approval: bool,
) -> None:
    approvals: list[str] = []
    calls: list[str] = []
    checks: list[str] = []

    @tool_input_guardrail
    def block_checkpoint(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        checks.append(data.context.tool_name)
        return ToolGuardrailFunctionOutput.reject_content("Release execution was blocked.")

    @tool(needs_approval=True, tool_input_guardrails=[block_checkpoint])
    def guarded_checkpoint(value: str) -> str:
        """Execute a release checkpoint only when its guardrail allows it."""
        calls.append(value)
        return "CHECKPOINT_READY"

    agent = RealtimeAgent(
        name="Packaged guarded realtime tool agent",
        instructions=(
            "Call guarded_checkpoint with value='release'. If blocked, reply exactly "
            "REALTIME_TOOL_BLOCKED."
        ),
        tools=[guarded_checkpoint],
    )
    runner = RealtimeRunner(
        agent,
        config={"tool_execution": {"pre_approval_tool_input_guardrails": pre_approval}},
    )

    async with await runner.run(
        model_config={
            "initial_model_settings": {
                "model_name": integration_realtime_model,
                "output_modalities": ["text"],
            }
        }
    ) as session:
        await session.send_message("Execute the protected release checkpoint.")

        async def receive() -> None:
            async for event in session:
                if isinstance(event, RealtimeToolApprovalRequired):
                    approvals.append(event.call_id)
                    await session.approve_tool_call(event.call_id)
                if event.type == "agent_end" and checks:
                    return

        await asyncio.wait_for(receive(), timeout=60)

    assert calls == []
    assert checks == ["guarded_checkpoint"]
    assert len(approvals) == (0 if pre_approval else 1)

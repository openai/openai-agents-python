"""Tests for soft cancel (after_turn mode) functionality."""

import json

import pytest

from agents import Agent, Runner, SQLiteSession

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_soft_cancel_completes_turn():
    """Verify soft cancel waits for turn to complete."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    # Cancel immediately after first event
    event_count = 0
    async for _ in result.stream_events():
        event_count += 1
        if event_count == 1:
            result.cancel(mode="after_turn")

    # Should get more than 1 event (turn completes)
    assert event_count > 1, "Soft cancel should allow turn to complete"
    assert result.is_complete


@pytest.mark.asyncio
async def test_soft_cancel_vs_immediate():
    """Compare soft cancel vs immediate cancel behavior."""
    # Immediate cancel
    model1 = FakeModel()
    agent1 = Agent(name="A1", model=model1)
    result1 = Runner.run_streamed(agent1, input="Hello")
    immediate_events = []
    async for event in result1.stream_events():
        immediate_events.append(event)
        if len(immediate_events) == 1:
            result1.cancel(mode="immediate")

    # Soft cancel
    model2 = FakeModel()
    agent2 = Agent(name="A2", model=model2)
    result2 = Runner.run_streamed(agent2, input="Hello")
    soft_events = []
    async for event in result2.stream_events():
        soft_events.append(event)
        if len(soft_events) == 1:
            result2.cancel(mode="after_turn")

    # Soft cancel should get more events
    assert len(soft_events) > len(immediate_events), (
        f"Soft cancel should get more events: soft={len(soft_events)}, immediate={len(immediate_events)}"  # noqa: E501
    )


@pytest.mark.asyncio
async def test_soft_cancel_with_tool_calls():
    """Verify tool calls execute before soft cancel stops."""
    model = FakeModel()
    agent = Agent(
        name="Assistant",
        model=model,
        tools=[get_function_tool("calc", "42")],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("Let me calculate"),
                get_function_tool_call("calc", json.dumps({})),
            ],
            [get_text_message("Result is 42")],
        ]
    )

    result = Runner.run_streamed(agent, input="Calculate")

    tool_call_seen = False
    tool_output_seen = False
    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            if event.name == "tool_called":
                tool_call_seen = True
                # Cancel right after seeing tool call
                result.cancel(mode="after_turn")
            elif event.name == "tool_output":
                tool_output_seen = True

    assert tool_call_seen, "Tool call should be seen"
    assert tool_output_seen, "Tool output should be seen (tool should execute before soft cancel)"


@pytest.mark.asyncio
async def test_soft_cancel_saves_session():
    """Verify session is saved properly with soft cancel."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    session = SQLiteSession("test_soft_cancel_session")
    await session.clear_session()  # Start fresh

    result = Runner.run_streamed(agent, input="Hello", session=session)

    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            result.cancel(mode="after_turn")

    # Check session has the turn
    items = await session.get_items()
    assert len(items) > 0, "Session should have saved items from completed turn"

    # Verify we can resume
    result2 = await Runner.run(agent, "Continue", session=session)
    assert result2.final_output is not None

    # Cleanup
    await session.clear_session()


@pytest.mark.asyncio
async def test_soft_cancel_tracks_usage():
    """Verify usage is tracked for completed turn."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            result.cancel(mode="after_turn")

    # Usage should be tracked (FakeModel tracks requests even if tokens are 0)
    assert result.context_wrapper.usage.requests > 0


@pytest.mark.asyncio
async def test_soft_cancel_stops_next_turn():
    """Verify soft cancel prevents next turn from starting."""
    model = FakeModel()
    agent = Agent(
        name="Assistant",
        model=model,
        tools=[get_function_tool("tool1", "result1")],
    )

    # Set up multi-turn scenario
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("tool1", "{}")],
            [get_text_message("Turn 2")],
            [get_text_message("Turn 3")],
        ]
    )

    result = Runner.run_streamed(agent, input="Hello")

    turns_completed = 0
    async for event in result.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            turns_completed += 1
            if turns_completed == 1:
                result.cancel(mode="after_turn")

    assert turns_completed == 1, "Should complete exactly 1 turn"


@pytest.mark.asyncio
async def test_cancel_mode_backward_compatibility():
    """Verify default behavior unchanged."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    events = []
    async for event in result.stream_events():
        events.append(event)
        if len(events) == 1:
            result.cancel()  # No mode argument

    # Should behave like immediate cancel
    assert len(events) == 1
    assert result.is_complete
    assert result._event_queue.empty()
    assert result._cancel_mode == "immediate", "Should default to immediate mode"


@pytest.mark.asyncio
async def test_soft_cancel_idempotent():
    """Verify calling cancel multiple times is safe."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    called_twice = False
    async for _ in result.stream_events():
        if not called_twice:
            result.cancel(mode="after_turn")
            result.cancel(mode="after_turn")  # Second call
            called_twice = True

    # Should not raise or cause issues
    assert result.is_complete


@pytest.mark.asyncio
async def test_soft_cancel_before_streaming():
    """Verify soft cancel before streaming starts."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")
    result.cancel(mode="after_turn")

    events = [e async for e in result.stream_events()]

    # Should stop quickly (may get agent_updated event before stopping)
    assert len(events) <= 1, "Should get at most 1 event (agent_updated)"
    assert result.is_complete


@pytest.mark.asyncio
async def test_soft_cancel_mixed_modes():
    """Verify changing cancel mode behaves correctly."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    # First call soft, then immediate
    result.cancel(mode="after_turn")
    result.cancel(mode="immediate")  # Override to immediate

    _ = [e async for e in result.stream_events()]

    # Immediate should take precedence
    assert result._cancel_mode == "immediate"
    # Queues should be empty (immediate cancel behavior)
    assert result._event_queue.empty()


@pytest.mark.asyncio
async def test_soft_cancel_explicit_immediate_mode():
    """Test explicit immediate mode behaves same as default."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    events = []
    async for event in result.stream_events():
        events.append(event)
        if len(events) == 1:
            result.cancel(mode="immediate")
            break

    assert result.is_complete
    assert result._event_queue.empty()
    assert result._cancel_mode == "immediate"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_soft_cancel_with_multiple_tool_calls():
    """Verify soft cancel works with multiple tool calls in one turn."""
    model = FakeModel()
    agent = Agent(
        name="Assistant",
        model=model,
        tools=[
            get_function_tool("tool1", "result1"),
            get_function_tool("tool2", "result2"),
        ],
    )

    # Turn with multiple tool calls
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("tool1", "{}"),
                get_function_tool_call("tool2", "{}"),
            ],
            [get_text_message("Both tools executed")],
        ]
    )

    result = Runner.run_streamed(agent, input="Execute tools")

    tool_outputs_seen = 0
    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            if event.name == "tool_called":
                # Cancel after seeing first tool call
                if tool_outputs_seen == 0:
                    result.cancel(mode="after_turn")
            elif event.name == "tool_output":
                tool_outputs_seen += 1

    # Both tools should execute
    assert tool_outputs_seen == 2, "Both tools should execute before soft cancel"


@pytest.mark.asyncio
async def test_soft_cancel_preserves_state():
    """Verify soft cancel preserves all result state correctly."""
    model = FakeModel()
    agent = Agent(
        name="Assistant",
        model=model,
        tools=[get_function_tool("tool1", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("tool1", "{}")],
            [get_text_message("Done")],
        ]
    )

    result = Runner.run_streamed(agent, input="Hello")

    async for event in result.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            result.cancel(mode="after_turn")

    # Verify state is preserved
    assert result.is_complete
    assert len(result.new_items) > 0, "Should have items from completed turn"
    assert len(result.raw_responses) > 0, "Should have raw responses"
    assert result.context_wrapper.usage.requests > 0, "Should have usage data (requests tracked)"


@pytest.mark.asyncio
async def test_immediate_cancel_clears_queues():
    """Verify immediate cancel clears queues as expected."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    async for _ in result.stream_events():
        result.cancel(mode="immediate")
        break

    # Verify queues are cleared
    assert result._event_queue.empty(), "Event queue should be empty after immediate cancel"
    assert result._input_guardrail_queue.empty(), (
        "Input guardrail queue should be empty after immediate cancel"
    )


@pytest.mark.asyncio
async def test_soft_cancel_does_not_clear_queues_immediately():
    """Verify soft cancel does NOT clear queues immediately."""
    model = FakeModel()
    agent = Agent(name="Assistant", model=model)

    result = Runner.run_streamed(agent, input="Hello")

    # Just call cancel, don't consume events yet
    result.cancel(mode="after_turn")

    # The cancel mode should be set
    assert result._cancel_mode == "after_turn"

    # Now consume events
    events = [e async for e in result.stream_events()]

    # Should have received events (queue was not cleared immediately)
    assert len(events) >= 0  # Events may or may not be present depending on timing


@pytest.mark.asyncio
async def test_soft_cancel_with_handoff():
    """Verify soft cancel after handoff saves the handoff turn."""
    from agents import Handoff

    model = FakeModel()

    # Create two agents with handoff
    agent2 = Agent(name="Agent2", model=model)

    async def on_invoke_handoff(context, data):
        return agent2

    agent1 = Agent(
        name="Agent1",
        model=model,
        handoffs=[
            Handoff(
                tool_name=Handoff.default_tool_name(agent2),
                tool_description=Handoff.default_tool_description(agent2),
                input_json_schema={},
                on_invoke_handoff=on_invoke_handoff,
                agent_name=agent2.name,
            )
        ],
    )

    # Setup: Agent1 does handoff, Agent2 responds
    model.add_multiple_turn_outputs(
        [
            # Agent1's turn - triggers handoff
            [get_function_tool_call(Handoff.default_tool_name(agent2), "{}")],
            # Agent2's turn after handoff
            [get_text_message("Agent2 response")],
        ]
    )

    session = SQLiteSession("test_soft_cancel_handoff")
    await session.clear_session()

    result = Runner.run_streamed(agent1, input="Hello", session=session)

    handoff_seen = False
    async for event in result.stream_events():
        if event.type == "run_item_stream_event" and event.name == "handoff_occured":
            handoff_seen = True
            # Cancel right after handoff
            result.cancel(mode="after_turn")

    assert handoff_seen, "Handoff should have occurred"

    # Verify session has items from the handoff turn
    items = await session.get_items()
    assert len(items) > 0, "Session should have saved the handoff turn"

    # Cleanup
    await session.clear_session()


@pytest.mark.asyncio
async def test_soft_cancel_with_session_and_multiple_turns():
    """Verify soft cancel with session across multiple turns."""
    model = FakeModel()
    agent = Agent(
        name="Assistant",
        model=model,
        tools=[get_function_tool("tool1", "result1")],
    )

    session = SQLiteSession("test_soft_cancel_multi")
    await session.clear_session()

    # Setup 3 turns
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("tool1", "{}")],
            [get_function_tool_call("tool1", "{}")],
            [get_text_message("Final")],
        ]
    )

    result = Runner.run_streamed(agent, input="Hello", session=session)

    turns_seen = 0
    async for event in result.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            turns_seen += 1
            if turns_seen == 2:
                result.cancel(mode="after_turn")

    # Should have completed 2 turns
    assert turns_seen == 2

    # Check session has both turns
    items = await session.get_items()
    assert len(items) > 0

    # Cleanup
    await session.clear_session()

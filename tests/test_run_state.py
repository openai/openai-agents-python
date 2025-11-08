"""Tests for RunState serialization, approval/rejection, and state management.

These tests match the TypeScript implementation from openai-agents-js to ensure parity.
"""

import json
from typing import Any

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from agents import Agent
from agents._run_impl import NextStepInterruption
from agents.items import MessageOutputItem, ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.run_state import CURRENT_SCHEMA_VERSION, RunState, _build_agent_map
from agents.usage import Usage


class TestRunState:
    """Test RunState initialization, serialization, and core functionality."""

    def test_initializes_with_default_values(self):
        """Test that RunState initializes with correct default values."""
        context = RunContextWrapper(context={"foo": "bar"})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)

        assert state._current_turn == 0
        assert state._current_agent == agent
        assert state._original_input == "input"
        assert state._max_turns == 3
        assert state._model_responses == []
        assert state._generated_items == []
        assert state._current_step is None
        assert state._context is not None
        assert state._context.context == {"foo": "bar"}

    def test_to_json_and_to_string_produce_valid_json(self):
        """Test that toJSON and toString produce valid JSON with correct schema."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="Agent1")
        state = RunState(
            context=context, original_input="input1", starting_agent=agent, max_turns=2
        )

        json_data = state.to_json()
        assert json_data["$schemaVersion"] == CURRENT_SCHEMA_VERSION
        assert json_data["currentTurn"] == 0
        assert json_data["currentAgent"] == {"name": "Agent1"}
        assert json_data["originalInput"] == "input1"
        assert json_data["maxTurns"] == 2
        assert json_data["generatedItems"] == []
        assert json_data["modelResponses"] == []

        str_data = state.to_string()
        assert isinstance(str_data, str)
        assert json.loads(str_data) == json_data

    async def test_throws_error_if_schema_version_is_missing_or_invalid(self):
        """Test that deserialization fails with missing or invalid schema version."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="Agent1")
        state = RunState(
            context=context, original_input="input1", starting_agent=agent, max_turns=2
        )

        json_data = state.to_json()
        del json_data["$schemaVersion"]

        str_data = json.dumps(json_data)
        with pytest.raises(Exception, match="Run state is missing schema version"):
            await RunState.from_string(agent, str_data)

        json_data["$schemaVersion"] = "0.1"
        with pytest.raises(
            Exception,
            match=(
                f"Run state schema version 0.1 is not supported. "
                f"Please use version {CURRENT_SCHEMA_VERSION}"
            ),
        ):
            await RunState.from_string(agent, json.dumps(json_data))

    def test_approve_updates_context_approvals_correctly(self):
        """Test that approve() correctly updates context approvals."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="Agent2")
        state = RunState(context=context, original_input="", starting_agent=agent, max_turns=1)

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="toolX",
            call_id="cid123",
            status="completed",
            arguments="arguments",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)

        state.approve(approval_item)

        # Check that the tool is approved
        assert state._context is not None
        assert state._context.is_tool_approved(tool_name="toolX", call_id="cid123") is True

    def test_returns_undefined_when_approval_status_is_unknown(self):
        """Test that isToolApproved returns None for unknown tools."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        assert context.is_tool_approved(tool_name="unknownTool", call_id="cid999") is None

    def test_reject_updates_context_approvals_correctly(self):
        """Test that reject() correctly updates context approvals."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="Agent3")
        state = RunState(context=context, original_input="", starting_agent=agent, max_turns=1)

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="toolY",
            call_id="cid456",
            status="completed",
            arguments="arguments",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)

        state.reject(approval_item)

        assert state._context is not None
        assert state._context.is_tool_approved(tool_name="toolY", call_id="cid456") is False

    def test_reject_permanently_when_always_reject_option_is_passed(self):
        """Test that reject with always_reject=True sets permanent rejection."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="Agent4")
        state = RunState(context=context, original_input="", starting_agent=agent, max_turns=1)

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="toolZ",
            call_id="cid789",
            status="completed",
            arguments="arguments",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)

        state.reject(approval_item, always_reject=True)

        assert state._context is not None
        assert state._context.is_tool_approved(tool_name="toolZ", call_id="cid789") is False

        # Check that it's permanently rejected
        assert state._context is not None
        approvals = state._context._approvals
        assert "toolZ" in approvals
        assert approvals["toolZ"].approved is False
        assert approvals["toolZ"].rejected is True

    def test_approve_raises_when_context_is_none(self):
        """Test that approve raises UserError when context is None."""
        agent = Agent(name="Agent5")
        state: RunState[dict[str, str], Agent[Any]] = RunState(
            context=RunContextWrapper(context={}),
            original_input="",
            starting_agent=agent,
            max_turns=1,
        )
        state._context = None  # Simulate None context

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="tool",
            call_id="cid",
            status="completed",
            arguments="",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)

        with pytest.raises(Exception, match="Cannot approve tool: RunState has no context"):
            state.approve(approval_item)

    def test_reject_raises_when_context_is_none(self):
        """Test that reject raises UserError when context is None."""
        agent = Agent(name="Agent6")
        state: RunState[dict[str, str], Agent[Any]] = RunState(
            context=RunContextWrapper(context={}),
            original_input="",
            starting_agent=agent,
            max_turns=1,
        )
        state._context = None  # Simulate None context

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="tool",
            call_id="cid",
            status="completed",
            arguments="",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)

        with pytest.raises(Exception, match="Cannot reject tool: RunState has no context"):
            state.reject(approval_item)

    async def test_from_string_reconstructs_state_for_simple_agent(self):
        """Test that fromString correctly reconstructs state for a simple agent."""
        context = RunContextWrapper(context={"a": 1})
        agent = Agent(name="Solo")
        state = RunState(context=context, original_input="orig", starting_agent=agent, max_turns=7)
        state._current_turn = 5

        str_data = state.to_string()
        new_state = await RunState.from_string(agent, str_data)

        assert new_state._max_turns == 7
        assert new_state._current_turn == 5
        assert new_state._current_agent == agent
        assert new_state._context is not None
        assert new_state._context.context == {"a": 1}
        assert new_state._generated_items == []
        assert new_state._model_responses == []

    async def test_from_json_reconstructs_state(self):
        """Test that from_json correctly reconstructs state from dict."""
        context = RunContextWrapper(context={"test": "data"})
        agent = Agent(name="JsonAgent")
        state = RunState(
            context=context, original_input="test input", starting_agent=agent, max_turns=5
        )
        state._current_turn = 2

        json_data = state.to_json()
        new_state = await RunState.from_json(agent, json_data)

        assert new_state._max_turns == 5
        assert new_state._current_turn == 2
        assert new_state._current_agent == agent
        assert new_state._context is not None
        assert new_state._context.context == {"test": "data"}

    def test_get_interruptions_returns_empty_when_no_interruptions(self):
        """Test that get_interruptions returns empty list when no interruptions."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="Agent5")
        state = RunState(context=context, original_input="", starting_agent=agent, max_turns=1)

        assert state.get_interruptions() == []

    def test_get_interruptions_returns_interruptions_when_present(self):
        """Test that get_interruptions returns interruptions when present."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="Agent6")
        state = RunState(context=context, original_input="", starting_agent=agent, max_turns=1)

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="toolA",
            call_id="cid111",
            status="completed",
            arguments="args",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)
        state._current_step = NextStepInterruption(interruptions=[approval_item])

        interruptions = state.get_interruptions()
        assert len(interruptions) == 1
        assert interruptions[0] == approval_item

    async def test_serializes_and_restores_approvals(self):
        """Test that approval state is preserved through serialization."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="ApprovalAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=3)

        # Approve one tool
        raw_item1 = ResponseFunctionToolCall(
            type="function_call",
            name="tool1",
            call_id="cid1",
            status="completed",
            arguments="",
        )
        approval_item1 = ToolApprovalItem(agent=agent, raw_item=raw_item1)
        state.approve(approval_item1, always_approve=True)

        # Reject another tool
        raw_item2 = ResponseFunctionToolCall(
            type="function_call",
            name="tool2",
            call_id="cid2",
            status="completed",
            arguments="",
        )
        approval_item2 = ToolApprovalItem(agent=agent, raw_item=raw_item2)
        state.reject(approval_item2, always_reject=True)

        # Serialize and deserialize
        str_data = state.to_string()
        new_state = await RunState.from_string(agent, str_data)

        # Check approvals are preserved
        assert new_state._context is not None
        assert new_state._context.is_tool_approved(tool_name="tool1", call_id="cid1") is True
        assert new_state._context.is_tool_approved(tool_name="tool2", call_id="cid2") is False


class TestBuildAgentMap:
    """Test agent map building for handoff resolution."""

    def test_build_agent_map_collects_agents_without_looping(self):
        """Test that buildAgentMap handles circular handoff references."""
        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")

        # Create a cycle A -> B -> A
        agent_a.handoffs = [agent_b]
        agent_b.handoffs = [agent_a]

        agent_map = _build_agent_map(agent_a)

        assert agent_map.get("AgentA") is not None
        assert agent_map.get("AgentB") is not None
        assert agent_map.get("AgentA").name == agent_a.name  # type: ignore[union-attr]
        assert agent_map.get("AgentB").name == agent_b.name  # type: ignore[union-attr]
        assert sorted(agent_map.keys()) == ["AgentA", "AgentB"]

    def test_build_agent_map_handles_complex_handoff_graphs(self):
        """Test that buildAgentMap handles complex handoff graphs."""
        agent_a = Agent(name="A")
        agent_b = Agent(name="B")
        agent_c = Agent(name="C")
        agent_d = Agent(name="D")

        # Create graph: A -> B, C; B -> D; C -> D
        agent_a.handoffs = [agent_b, agent_c]
        agent_b.handoffs = [agent_d]
        agent_c.handoffs = [agent_d]

        agent_map = _build_agent_map(agent_a)

        assert len(agent_map) == 4
        assert all(agent_map.get(name) is not None for name in ["A", "B", "C", "D"])


class TestSerializationRoundTrip:
    """Test that serialization and deserialization preserve state correctly."""

    async def test_preserves_usage_data(self):
        """Test that usage data is preserved through serialization."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        context.usage.requests = 5
        context.usage.input_tokens = 100
        context.usage.output_tokens = 50
        context.usage.total_tokens = 150

        agent = Agent(name="UsageAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=10)

        str_data = state.to_string()
        new_state = await RunState.from_string(agent, str_data)

        assert new_state._context is not None
        assert new_state._context.usage.requests == 5
        assert new_state._context.usage is not None
        assert new_state._context.usage.input_tokens == 100
        assert new_state._context.usage is not None
        assert new_state._context.usage.output_tokens == 50
        assert new_state._context.usage is not None
        assert new_state._context.usage.total_tokens == 150

    def test_serializes_generated_items(self):
        """Test that generated items are serialized and restored."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="ItemAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=5)

        # Add a message output item with proper ResponseOutputMessage structure
        message = ResponseOutputMessage(
            id="msg_123",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text="Hello!", annotations=[])],
        )
        message_item = MessageOutputItem(agent=agent, raw_item=message)
        state._generated_items.append(message_item)

        # Serialize
        json_data = state.to_json()
        assert len(json_data["generatedItems"]) == 1
        assert json_data["generatedItems"][0]["type"] == "message_output_item"

    async def test_serializes_current_step_interruption(self):
        """Test that current step interruption is serialized correctly."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="InterruptAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=3)

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="myTool",
            call_id="cid_int",
            status="completed",
            arguments='{"arg": "value"}',
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)
        state._current_step = NextStepInterruption(interruptions=[approval_item])

        json_data = state.to_json()
        assert json_data["currentStep"] is not None
        assert json_data["currentStep"]["type"] == "next_step_interruption"
        assert len(json_data["currentStep"]["data"]["interruptions"]) == 1

        # Deserialize and verify
        new_state = await RunState.from_json(agent, json_data)
        assert isinstance(new_state._current_step, NextStepInterruption)
        assert len(new_state._current_step.interruptions) == 1
        restored_item = new_state._current_step.interruptions[0]
        assert isinstance(restored_item, ToolApprovalItem)
        assert restored_item.raw_item.name == "myTool"

    async def test_deserializes_various_item_types(self):
        """Test that deserialization handles different item types."""
        from agents.items import ToolCallItem, ToolCallOutputItem

        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="ItemAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=5)

        # Add various item types
        # 1. Message output item
        msg = ResponseOutputMessage(
            id="msg_1",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text="Hello", annotations=[])],
        )
        state._generated_items.append(MessageOutputItem(agent=agent, raw_item=msg))

        # 2. Tool call item
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="my_tool",
            call_id="call_1",
            status="completed",
            arguments='{"arg": "val"}',
        )
        state._generated_items.append(ToolCallItem(agent=agent, raw_item=tool_call))

        # 3. Tool call output item
        tool_output = {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        }
        state._generated_items.append(
            ToolCallOutputItem(agent=agent, raw_item=tool_output, output="result")  # type: ignore[arg-type]
        )

        # Serialize and deserialize
        json_data = state.to_json()
        new_state = await RunState.from_json(agent, json_data)

        # Verify all items were restored
        assert len(new_state._generated_items) == 3
        assert isinstance(new_state._generated_items[0], MessageOutputItem)
        assert isinstance(new_state._generated_items[1], ToolCallItem)
        assert isinstance(new_state._generated_items[2], ToolCallOutputItem)

    async def test_deserialization_handles_unknown_agent_gracefully(self):
        """Test that deserialization skips items with unknown agents."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="KnownAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=5)

        # Add an item
        msg = ResponseOutputMessage(
            id="msg_1",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text="Test", annotations=[])],
        )
        state._generated_items.append(MessageOutputItem(agent=agent, raw_item=msg))

        # Serialize
        json_data = state.to_json()

        # Modify the agent name to an unknown one
        json_data["generatedItems"][0]["agent"]["name"] = "UnknownAgent"

        # Deserialize - should skip the item with unknown agent
        new_state = await RunState.from_json(agent, json_data)

        # Item should be skipped
        assert len(new_state._generated_items) == 0

    async def test_deserialization_handles_malformed_items_gracefully(self):
        """Test that deserialization handles malformed items without crashing."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=5)

        # Serialize
        json_data = state.to_json()

        # Add a malformed item
        json_data["generatedItems"] = [
            {
                "type": "message_output_item",
                "agent": {"name": "TestAgent"},
                "rawItem": {
                    # Missing required fields - will cause deserialization error
                    "type": "message",
                },
            }
        ]

        # Should not crash, just skip the malformed item
        new_state = await RunState.from_json(agent, json_data)

        # Malformed item should be skipped
        assert len(new_state._generated_items) == 0


class TestRunContextApprovals:
    """Test RunContext approval edge cases for coverage."""

    def test_approval_takes_precedence_over_rejection_when_both_true(self):
        """Test that approval takes precedence when both approved and rejected are True."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})

        # Manually set both approved and rejected to True (edge case)
        context._approvals["test_tool"] = type(
            "ApprovalEntry", (), {"approved": True, "rejected": True}
        )()

        # Should return True (approval takes precedence)
        result = context.is_tool_approved("test_tool", "call_id")
        assert result is True

    def test_individual_approval_takes_precedence_over_individual_rejection(self):
        """Test individual call_id approval takes precedence over rejection."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})

        # Set both individual approval and rejection lists with same call_id
        context._approvals["test_tool"] = type(
            "ApprovalEntry", (), {"approved": ["call_123"], "rejected": ["call_123"]}
        )()

        # Should return True (approval takes precedence)
        result = context.is_tool_approved("test_tool", "call_123")
        assert result is True

    def test_returns_none_when_no_approval_or_rejection(self):
        """Test that None is returned when no approval/rejection info exists."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})

        # Tool exists but no approval/rejection
        context._approvals["test_tool"] = type(
            "ApprovalEntry", (), {"approved": [], "rejected": []}
        )()

        # Should return None (unknown status)
        result = context.is_tool_approved("test_tool", "call_456")
        assert result is None


class TestRunStateEdgeCases:
    """Test RunState edge cases and error conditions."""

    def test_to_json_raises_when_no_current_agent(self):
        """Test that to_json raises when current_agent is None."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=5)
        state._current_agent = None  # Simulate None agent

        with pytest.raises(Exception, match="Cannot serialize RunState: No current agent"):
            state.to_json()

    def test_to_json_raises_when_no_context(self):
        """Test that to_json raises when context is None."""
        agent = Agent(name="TestAgent")
        state: RunState[dict[str, str], Agent[Any]] = RunState(
            context=RunContextWrapper(context={}),
            original_input="test",
            starting_agent=agent,
            max_turns=5,
        )
        state._context = None  # Simulate None context

        with pytest.raises(Exception, match="Cannot serialize RunState: No context"):
            state.to_json()


class TestDeserializeHelpers:
    """Test deserialization helper functions and round-trip serialization."""

    async def test_serialization_includes_handoff_fields(self):
        """Test that handoff items include source and target agent fields."""
        from agents.items import HandoffOutputItem

        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")
        agent_a.handoffs = [agent_b]

        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        state = RunState(
            context=context,
            original_input="test handoff",
            starting_agent=agent_a,
            max_turns=2,
        )

        # Create a handoff output item
        handoff_item = HandoffOutputItem(
            agent=agent_b,
            raw_item={"type": "handoff_output", "status": "completed"},  # type: ignore[arg-type]
            source_agent=agent_a,
            target_agent=agent_b,
        )
        state._generated_items.append(handoff_item)

        json_data = state.to_json()
        assert len(json_data["generatedItems"]) == 1
        item_data = json_data["generatedItems"][0]
        assert "sourceAgent" in item_data
        assert "targetAgent" in item_data
        assert item_data["sourceAgent"]["name"] == "AgentA"
        assert item_data["targetAgent"]["name"] == "AgentB"

        # Test round-trip deserialization
        restored = await RunState.from_string(agent_a, state.to_string())
        assert len(restored._generated_items) == 1
        assert restored._generated_items[0].type == "handoff_output_item"

    async def test_model_response_serialization_roundtrip(self):
        """Test that model responses serialize and deserialize correctly."""
        from agents.items import ModelResponse

        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=2)

        # Add a model response
        response = ModelResponse(
            usage=Usage(requests=1, input_tokens=10, output_tokens=20, total_tokens=30),
            output=[
                ResponseOutputMessage(
                    type="message",
                    id="msg1",
                    status="completed",
                    role="assistant",
                    content=[ResponseOutputText(text="Hello", type="output_text", annotations=[])],
                )
            ],
            response_id="resp123",
        )
        state._model_responses.append(response)

        # Round trip
        json_str = state.to_string()
        restored = await RunState.from_string(agent, json_str)

        assert len(restored._model_responses) == 1
        assert restored._model_responses[0].response_id == "resp123"
        assert restored._model_responses[0].usage.requests == 1
        assert restored._model_responses[0].usage.input_tokens == 10

    async def test_interruptions_serialization_roundtrip(self):
        """Test that interruptions serialize and deserialize correctly."""
        from agents._run_impl import NextStepInterruption

        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="InterruptAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=2)

        # Create tool approval item for interruption
        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="sensitive_tool",
            call_id="call789",
            status="completed",
            arguments='{"data": "value"}',
            id="1",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)

        # Set interruption
        state._current_step = NextStepInterruption(interruptions=[approval_item])

        # Round trip
        json_str = state.to_string()
        restored = await RunState.from_string(agent, json_str)

        assert restored._current_step is not None
        assert isinstance(restored._current_step, NextStepInterruption)
        assert len(restored._current_step.interruptions) == 1
        assert restored._current_step.interruptions[0].raw_item.name == "sensitive_tool"  # type: ignore[union-attr]

    async def test_json_decode_error_handling(self):
        """Test that invalid JSON raises appropriate error."""
        agent = Agent(name="TestAgent")

        with pytest.raises(Exception, match="Failed to parse run state JSON"):
            await RunState.from_string(agent, "{ invalid json }")

    async def test_missing_agent_in_map_error(self):
        """Test error when agent not found in agent map."""
        agent_a = Agent(name="AgentA")
        state: RunState[dict[str, str], Agent[Any]] = RunState(
            context=RunContextWrapper(context={}),
            original_input="test",
            starting_agent=agent_a,
            max_turns=2,
        )

        # Serialize with AgentA
        json_str = state.to_string()

        # Try to deserialize with a different agent that doesn't have AgentA in handoffs
        agent_b = Agent(name="AgentB")
        with pytest.raises(Exception, match="Agent AgentA not found in agent map"):
            await RunState.from_string(agent_b, json_str)

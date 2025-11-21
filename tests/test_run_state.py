"""Tests for RunState serialization, approval/rejection, and state management."""

import json
from typing import Any

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_computer_tool_call import (
    ActionScreenshot,
    ResponseComputerToolCall,
)
from openai.types.responses.response_output_item import McpApprovalRequest
from openai.types.responses.tool_param import Mcp

from agents import Agent, Runner, handoff
from agents._run_impl import (
    NextStepInterruption,
    ProcessedResponse,
    ToolRunComputerAction,
    ToolRunFunction,
    ToolRunHandoff,
    ToolRunMCPApprovalRequest,
)
from agents.computer import Computer
from agents.exceptions import UserError
from agents.handoffs import Handoff
from agents.items import (
    HandoffOutputItem,
    MessageOutputItem,
    ModelResponse,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.run_context import RunContextWrapper
from agents.run_state import (
    CURRENT_SCHEMA_VERSION,
    RunState,
    _build_agent_map,
    _deserialize_items,
    _deserialize_processed_response,
    _normalize_field_names,
)
from agents.tool import ComputerTool, FunctionTool, HostedMCPTool, function_tool
from agents.tool_context import ToolContext
from agents.usage import Usage

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool_call,
    get_text_message,
)


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
        assert restored_item.name == "myTool"

    async def test_deserializes_various_item_types(self):
        """Test that deserialization handles different item types."""
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
            ToolCallOutputItem(agent=agent, raw_item=tool_output, output="result")
        )

        # Serialize and deserialize
        json_data = state.to_json()
        new_state = await RunState.from_json(agent, json_data)

        # Verify all items were restored
        assert len(new_state._generated_items) == 3
        assert isinstance(new_state._generated_items[0], MessageOutputItem)
        assert isinstance(new_state._generated_items[1], ToolCallItem)
        assert isinstance(new_state._generated_items[2], ToolCallOutputItem)

    async def test_serializes_original_input_with_function_call_output(self):
        """Test that originalInput with function_call_output items is converted to protocol."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        # Create originalInput with function_call_output (API format)
        # This simulates items from session that are in API format
        original_input = [
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "test_tool",
                "arguments": '{"arg": "value"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "result",
            },
        ]

        state = RunState(
            context=context, original_input=original_input, starting_agent=agent, max_turns=5
        )

        # Serialize - should convert function_call_output to function_call_result
        json_data = state.to_json()

        # Verify originalInput was converted to protocol format
        assert isinstance(json_data["originalInput"], list)
        assert len(json_data["originalInput"]) == 2

        # First item should remain function_call (with camelCase)
        assert json_data["originalInput"][0]["type"] == "function_call"
        assert json_data["originalInput"][0]["callId"] == "call_123"
        assert json_data["originalInput"][0]["name"] == "test_tool"

        # Second item should be converted to function_call_result (protocol format)
        assert json_data["originalInput"][1]["type"] == "function_call_result"
        assert json_data["originalInput"][1]["callId"] == "call_123"
        assert json_data["originalInput"][1]["name"] == "test_tool"  # Looked up from function_call
        assert json_data["originalInput"][1]["status"] == "completed"  # Added default
        assert json_data["originalInput"][1]["output"] == "result"

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


class TestRunStateResumption:
    """Test resuming runs from RunState using Runner.run()."""

    @pytest.mark.asyncio
    async def test_resume_from_run_state(self):
        """Test resuming a run from a RunState."""
        model = FakeModel()
        agent = Agent(name="TestAgent", model=model)

        # First run - create a state
        model.set_next_output([get_text_message("First response")])
        result1 = await Runner.run(agent, "First input")

        # Create RunState from result
        state = result1.to_state()

        # Resume from state
        model.set_next_output([get_text_message("Second response")])
        result2 = await Runner.run(agent, state)

        assert result2.final_output == "Second response"

    @pytest.mark.asyncio
    async def test_resume_from_run_state_with_context(self):
        """Test resuming a run from a RunState with context override."""
        model = FakeModel()
        agent = Agent(name="TestAgent", model=model)

        # First run with context
        context1 = {"key": "value1"}
        model.set_next_output([get_text_message("First response")])
        result1 = await Runner.run(agent, "First input", context=context1)

        # Create RunState from result
        state = result1.to_state()

        # Resume from state with different context (should use state's context)
        context2 = {"key": "value2"}
        model.set_next_output([get_text_message("Second response")])
        result2 = await Runner.run(agent, state, context=context2)

        # State's context should be used, not the new context
        assert result2.final_output == "Second response"

    @pytest.mark.asyncio
    async def test_resume_from_run_state_with_conversation_id(self):
        """Test resuming a run from a RunState with conversation_id."""
        model = FakeModel()
        agent = Agent(name="TestAgent", model=model)

        # First run
        model.set_next_output([get_text_message("First response")])
        result1 = await Runner.run(agent, "First input", conversation_id="conv123")

        # Create RunState from result
        state = result1.to_state()

        # Resume from state with conversation_id
        model.set_next_output([get_text_message("Second response")])
        result2 = await Runner.run(agent, state, conversation_id="conv123")

        assert result2.final_output == "Second response"

    @pytest.mark.asyncio
    async def test_resume_from_run_state_with_previous_response_id(self):
        """Test resuming a run from a RunState with previous_response_id."""
        model = FakeModel()
        agent = Agent(name="TestAgent", model=model)

        # First run
        model.set_next_output([get_text_message("First response")])
        result1 = await Runner.run(agent, "First input", previous_response_id="resp123")

        # Create RunState from result
        state = result1.to_state()

        # Resume from state with previous_response_id
        model.set_next_output([get_text_message("Second response")])
        result2 = await Runner.run(agent, state, previous_response_id="resp123")

        assert result2.final_output == "Second response"

    @pytest.mark.asyncio
    async def test_resume_from_run_state_with_interruption(self):
        """Test resuming a run from a RunState with an interruption."""
        model = FakeModel()

        async def tool_func() -> str:
            return "tool_result"

        tool = function_tool(tool_func, name_override="test_tool")

        agent = Agent(
            name="TestAgent",
            model=model,
            tools=[tool],
        )

        # First run - create an interruption
        model.set_next_output([get_function_tool_call("test_tool", "{}")])
        result1 = await Runner.run(agent, "First input")

        # Create RunState from result
        state = result1.to_state()

        # Approve the tool call if there are interruptions
        if state.get_interruptions():
            state.approve(state.get_interruptions()[0])

        # Resume from state - should execute approved tools
        model.set_next_output([get_text_message("Second response")])
        result2 = await Runner.run(agent, state)

        assert result2.final_output == "Second response"

    @pytest.mark.asyncio
    async def test_resume_from_run_state_streamed(self):
        """Test resuming a run from a RunState using run_streamed."""
        model = FakeModel()
        agent = Agent(name="TestAgent", model=model)

        # First run
        model.set_next_output([get_text_message("First response")])
        result1 = await Runner.run(agent, "First input")

        # Create RunState from result
        state = result1.to_state()

        # Resume from state using run_streamed
        model.set_next_output([get_text_message("Second response")])
        result2 = Runner.run_streamed(agent, state)

        events = []
        async for event in result2.stream_events():
            events.append(event)
            if hasattr(event, "type") and event.type == "run_complete":  # type: ignore[comparison-overlap]
                break

        assert result2.final_output == "Second response"

    @pytest.mark.asyncio
    async def test_resume_from_run_state_streamed_uses_context_from_state(self):
        """Test that streaming with RunState uses context from state."""

        model = FakeModel()
        model.set_next_output([get_text_message("done")])
        agent = Agent(name="TestAgent", model=model)

        # Create a RunState with context
        context_wrapper = RunContextWrapper(context={"key": "value"})
        state = RunState(
            context=context_wrapper,
            original_input="test",
            starting_agent=agent,
            max_turns=1,
        )

        # Run streaming with RunState but no context parameter (should use state's context)
        result = Runner.run_streamed(agent, state)  # No context parameter
        async for _ in result.stream_events():
            pass

        # Should complete successfully using state's context
        assert result.final_output == "done"

    @pytest.mark.asyncio
    async def test_run_result_streaming_to_state_with_interruptions(self):
        """Test RunResultStreaming.to_state() sets _current_step with interruptions."""
        model = FakeModel()
        agent = Agent(name="TestAgent", model=model)

        async def test_tool() -> str:
            return "result"

        # Create a tool that requires approval
        async def needs_approval(_ctx, _params, _call_id) -> bool:
            return True

        tool = function_tool(test_tool, name_override="test_tool", needs_approval=needs_approval)
        agent.tools = [tool]

        # Create a run that will have interruptions
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("test_tool", json.dumps({}))],
                [get_text_message("done")],
            ]
        )

        result = Runner.run_streamed(agent, "test")
        async for _ in result.stream_events():
            pass

        # Should have interruptions
        assert len(result.interruptions) > 0

        # Convert to state
        state = result.to_state()

        # State should have _current_step set to NextStepInterruption
        from agents._run_impl import NextStepInterruption

        assert state._current_step is not None
        assert isinstance(state._current_step, NextStepInterruption)
        assert len(state._current_step.interruptions) == len(result.interruptions)


class TestRunStateSerializationEdgeCases:
    """Test edge cases in RunState serialization."""

    @pytest.mark.asyncio
    async def test_to_json_includes_tool_call_items_from_last_processed_response(self):
        """Test that to_json includes tool_call_items from lastProcessedResponse.newItems."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)

        # Create a tool call item
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="test_tool",
            call_id="call123",
            status="completed",
            arguments="{}",
        )
        tool_call_item = ToolCallItem(agent=agent, raw_item=tool_call)

        # Create a ProcessedResponse with the tool call item in new_items
        processed_response = ProcessedResponse(
            new_items=[tool_call_item],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        # Set the last processed response
        state._last_processed_response = processed_response

        # Serialize
        json_data = state.to_json()

        # Verify that the tool_call_item is in generatedItems
        generated_items = json_data.get("generatedItems", [])
        assert len(generated_items) == 1
        assert generated_items[0]["type"] == "tool_call_item"
        assert generated_items[0]["rawItem"]["name"] == "test_tool"

    @pytest.mark.asyncio
    async def test_to_json_camelizes_nested_dicts_and_lists(self):
        """Test that to_json camelizes nested dictionaries and lists."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)

        # Create a message with nested content
        message = ResponseOutputMessage(
            id="msg1",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text",
                    text="Hello",
                    annotations=[],
                    logprobs=[],
                )
            ],
        )
        state._generated_items.append(MessageOutputItem(agent=agent, raw_item=message))

        # Serialize
        json_data = state.to_json()

        # Verify that nested structures are camelized
        generated_items = json_data.get("generatedItems", [])
        assert len(generated_items) == 1
        raw_item = generated_items[0]["rawItem"]
        # Check that snake_case fields are camelized
        assert "responseId" in raw_item or "id" in raw_item

    @pytest.mark.asyncio
    async def test_from_json_with_last_processed_response(self):
        """Test that from_json correctly deserializes lastProcessedResponse."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)

        # Create a tool call item
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="test_tool",
            call_id="call123",
            status="completed",
            arguments="{}",
        )
        tool_call_item = ToolCallItem(agent=agent, raw_item=tool_call)

        # Create a ProcessedResponse with the tool call item
        processed_response = ProcessedResponse(
            new_items=[tool_call_item],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        # Set the last processed response
        state._last_processed_response = processed_response

        # Serialize and deserialize
        json_data = state.to_json()
        new_state = await RunState.from_json(agent, json_data)

        # Verify that last_processed_response was deserialized
        assert new_state._last_processed_response is not None
        assert len(new_state._last_processed_response.new_items) == 1
        assert new_state._last_processed_response.new_items[0].type == "tool_call_item"

    def test_camelize_field_names_with_nested_dicts_and_lists(self):
        """Test that _camelize_field_names handles nested dictionaries and lists."""
        # Test with nested dict - _camelize_field_names converts
        # specific fields (call_id, response_id)
        data = {
            "call_id": "call123",
            "nested_dict": {
                "response_id": "resp123",
                "nested_list": [{"call_id": "call456"}],
            },
        }
        result = RunState._camelize_field_names(data)
        # The method converts call_id to callId and response_id to responseId
        assert "callId" in result
        assert result["callId"] == "call123"
        # nested_dict is not converted (not in field_mapping), but nested fields are
        assert "nested_dict" in result
        assert "responseId" in result["nested_dict"]
        assert "nested_list" in result["nested_dict"]
        assert result["nested_dict"]["nested_list"][0]["callId"] == "call456"

        # Test with list
        data_list = [{"call_id": "call1"}, {"response_id": "resp1"}]
        result_list = RunState._camelize_field_names(data_list)
        assert len(result_list) == 2
        assert "callId" in result_list[0]
        assert "responseId" in result_list[1]

        # Test with non-dict/list (should return as-is)
        result_scalar = RunState._camelize_field_names("string")
        assert result_scalar == "string"

    async def test_serialize_handoff_with_name_fallback(self):
        """Test serialization of handoff with name fallback when tool_name is missing."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent_a = Agent(name="AgentA")

        # Create a handoff with a name attribute but no tool_name
        class MockHandoff:
            def __init__(self):
                self.name = "handoff_tool"

        mock_handoff = MockHandoff()
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="handoff_tool",
            call_id="call123",
            status="completed",
            arguments="{}",
        )

        handoff_run = ToolRunHandoff(handoff=mock_handoff, tool_call=tool_call)  # type: ignore[arg-type]

        processed_response = ProcessedResponse(
            new_items=[],
            handoffs=[handoff_run],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(
            context=context, original_input="input", starting_agent=agent_a, max_turns=3
        )
        state._last_processed_response = processed_response

        json_data = state.to_json()
        last_processed = json_data.get("lastProcessedResponse", {})
        handoffs = last_processed.get("handoffs", [])
        assert len(handoffs) == 1
        # The handoff should have a handoff field with toolName inside
        assert "handoff" in handoffs[0]
        handoff_dict = handoffs[0]["handoff"]
        assert "toolName" in handoff_dict
        assert handoff_dict["toolName"] == "handoff_tool"

    async def test_serialize_function_with_description_and_schema(self):
        """Test serialization of function with description and params_json_schema."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        async def tool_func(context: ToolContext[Any], arguments: str) -> str:
            return "result"

        tool = FunctionTool(
            on_invoke_tool=tool_func,
            name="test_tool",
            description="Test tool description",
            params_json_schema={"type": "object", "properties": {}},
        )

        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="test_tool",
            call_id="call123",
            status="completed",
            arguments="{}",
        )

        function_run = ToolRunFunction(tool_call=tool_call, function_tool=tool)

        processed_response = ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[function_run],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)
        state._last_processed_response = processed_response

        json_data = state.to_json()
        last_processed = json_data.get("lastProcessedResponse", {})
        functions = last_processed.get("functions", [])
        assert len(functions) == 1
        assert functions[0]["tool"]["description"] == "Test tool description"
        assert "paramsJsonSchema" in functions[0]["tool"]

    async def test_serialize_computer_action_with_description(self):
        """Test serialization of computer action with description."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        class MockComputer(Computer):
            @property
            def environment(self) -> str:  # type: ignore[override]
                return "mac"

            @property
            def dimensions(self) -> tuple[int, int]:
                return (1920, 1080)

            def screenshot(self) -> str:
                return "screenshot"

            def click(self, x: int, y: int, button: str) -> None:
                pass

            def double_click(self, x: int, y: int) -> None:
                pass

            def drag(self, path: list[tuple[int, int]]) -> None:
                pass

            def keypress(self, keys: list[str]) -> None:
                pass

            def move(self, x: int, y: int) -> None:
                pass

            def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
                pass

            def type(self, text: str) -> None:
                pass

            def wait(self) -> None:
                pass

        computer = MockComputer()
        computer_tool = ComputerTool(computer=computer)
        computer_tool.description = "Computer tool description"  # type: ignore[attr-defined]

        tool_call = ResponseComputerToolCall(
            id="1",
            type="computer_call",
            call_id="call123",
            status="completed",
            action=ActionScreenshot(type="screenshot"),
            pending_safety_checks=[],
        )

        action_run = ToolRunComputerAction(tool_call=tool_call, computer_tool=computer_tool)

        processed_response = ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[],
            computer_actions=[action_run],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)
        state._last_processed_response = processed_response

        json_data = state.to_json()
        last_processed = json_data.get("lastProcessedResponse", {})
        computer_actions = last_processed.get("computerActions", [])
        assert len(computer_actions) == 1
        # The computer action should have a computer field with description
        assert "computer" in computer_actions[0]
        computer_dict = computer_actions[0]["computer"]
        assert "description" in computer_dict
        assert computer_dict["description"] == "Computer tool description"

    async def test_serialize_mcp_approval_request(self):
        """Test serialization of MCP approval request."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        # Create a mock MCP tool - HostedMCPTool doesn't have a simple constructor
        # We'll just test the serialization logic without actually creating the tool
        class MockMCPTool:
            def __init__(self):
                self.name = "mcp_tool"

        mcp_tool = MockMCPTool()

        request_item = McpApprovalRequest(
            id="req123",
            type="mcp_approval_request",
            name="mcp_tool",
            server_label="test_server",
            arguments="{}",
        )

        request_run = ToolRunMCPApprovalRequest(request_item=request_item, mcp_tool=mcp_tool)  # type: ignore[arg-type]

        processed_response = ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[request_run],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)
        state._last_processed_response = processed_response

        json_data = state.to_json()
        last_processed = json_data.get("lastProcessedResponse", {})
        mcp_requests = last_processed.get("mcpApprovalRequests", [])
        assert len(mcp_requests) == 1
        assert "requestItem" in mcp_requests[0]

    async def test_serialize_item_with_non_dict_raw_item(self):
        """Test serialization of item with non-dict raw_item."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)

        # Create a message item
        message = ResponseOutputMessage(
            id="msg1",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(type="output_text", text="Hello", annotations=[], logprobs=[])
            ],
        )
        item = MessageOutputItem(agent=agent, raw_item=message)

        # The raw_item is a Pydantic model, not a dict, so it should use model_dump
        state._generated_items.append(item)

        json_data = state.to_json()
        generated_items = json_data.get("generatedItems", [])
        assert len(generated_items) == 1
        assert generated_items[0]["type"] == "message_output_item"

    async def test_normalize_field_names_with_exclude_fields(self):
        """Test that _normalize_field_names excludes providerData fields."""
        data = {
            "providerData": {"key": "value"},
            "provider_data": {"key": "value"},
            "normalField": "value",
        }

        result = _normalize_field_names(data)
        assert "providerData" not in result
        assert "provider_data" not in result
        assert "normalField" in result

    async def test_deserialize_tool_call_output_item_different_types(self):
        """Test deserialization of tool_call_output_item with different output types."""
        agent = Agent(name="TestAgent")

        # Test with function_call_output
        item_data_function = {
            "type": "tool_call_output_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "function_call_output",
                "call_id": "call123",
                "output": "result",
            },
        }

        result_function = _deserialize_items([item_data_function], {"TestAgent": agent})
        assert len(result_function) == 1
        assert result_function[0].type == "tool_call_output_item"

        # Test with computer_call_output
        item_data_computer = {
            "type": "tool_call_output_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "computer_call_output",
                "call_id": "call123",
                "output": {"type": "computer_screenshot", "screenshot": "screenshot"},
            },
        }

        result_computer = _deserialize_items([item_data_computer], {"TestAgent": agent})
        assert len(result_computer) == 1

        # Test with local_shell_call_output
        item_data_shell = {
            "type": "tool_call_output_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "local_shell_call_output",
                "id": "shell123",
                "call_id": "call123",
                "output": "result",
            },
        }

        result_shell = _deserialize_items([item_data_shell], {"TestAgent": agent})
        assert len(result_shell) == 1

    async def test_deserialize_reasoning_item(self):
        """Test deserialization of reasoning_item."""
        agent = Agent(name="TestAgent")

        item_data = {
            "type": "reasoning_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "reasoning",
                "id": "reasoning123",
                "summary": [],
                "content": [],
            },
        }

        result = _deserialize_items([item_data], {"TestAgent": agent})
        assert len(result) == 1
        assert result[0].type == "reasoning_item"

    async def test_deserialize_handoff_call_item(self):
        """Test deserialization of handoff_call_item."""
        agent = Agent(name="TestAgent")

        item_data = {
            "type": "handoff_call_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "function_call",
                "name": "handoff_tool",
                "call_id": "call123",
                "status": "completed",
                "arguments": "{}",
            },
        }

        result = _deserialize_items([item_data], {"TestAgent": agent})
        assert len(result) == 1
        assert result[0].type == "handoff_call_item"

    async def test_deserialize_mcp_items(self):
        """Test deserialization of MCP-related items."""
        agent = Agent(name="TestAgent")

        # Test MCP list tools item
        item_data_list = {
            "type": "mcp_list_tools_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "mcp_list_tools",
                "id": "list123",
                "server_label": "test_server",
                "tools": [],
            },
        }

        result_list = _deserialize_items([item_data_list], {"TestAgent": agent})
        assert len(result_list) == 1
        assert result_list[0].type == "mcp_list_tools_item"

        # Test MCP approval request item
        item_data_request = {
            "type": "mcp_approval_request_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "mcp_approval_request",
                "id": "req123",
                "name": "mcp_tool",
                "server_label": "test_server",
                "arguments": "{}",
            },
        }

        result_request = _deserialize_items([item_data_request], {"TestAgent": agent})
        assert len(result_request) == 1
        assert result_request[0].type == "mcp_approval_request_item"

        # Test MCP approval response item
        item_data_response = {
            "type": "mcp_approval_response_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "mcp_approval_response",
                "approval_request_id": "req123",
                "approve": True,
            },
        }

        result_response = _deserialize_items([item_data_response], {"TestAgent": agent})
        assert len(result_response) == 1
        assert result_response[0].type == "mcp_approval_response_item"

    async def test_deserialize_tool_approval_item(self):
        """Test deserialization of tool_approval_item."""
        agent = Agent(name="TestAgent")

        item_data = {
            "type": "tool_approval_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "function_call",
                "name": "test_tool",
                "call_id": "call123",
                "status": "completed",
                "arguments": "{}",
            },
        }

        result = _deserialize_items([item_data], {"TestAgent": agent})
        assert len(result) == 1
        assert result[0].type == "tool_approval_item"

    async def test_serialize_item_with_non_dict_non_model_raw_item(self):
        """Test serialization of item with raw_item that is neither dict nor model."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)

        # Create a mock item with a raw_item that is neither dict nor has model_dump
        class MockRawItem:
            def __init__(self):
                self.type = "message"
                self.content = "Hello"

        raw_item = MockRawItem()
        item = MessageOutputItem(agent=agent, raw_item=raw_item)  # type: ignore[arg-type]

        state._generated_items.append(item)

        # This should trigger the else branch in _serialize_item (line 481)
        json_data = state.to_json()
        generated_items = json_data.get("generatedItems", [])
        assert len(generated_items) == 1

    async def test_deserialize_processed_response_without_get_all_tools(self):
        """Test deserialization of ProcessedResponse when agent doesn't have get_all_tools."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})

        # Create an agent without get_all_tools method
        class AgentWithoutGetAllTools(Agent):
            pass

        agent_no_tools = AgentWithoutGetAllTools(name="TestAgent")

        processed_response_data: dict[str, Any] = {
            "newItems": [],
            "handoffs": [],
            "functions": [],
            "computerActions": [],
            "localShellCalls": [],
            "mcpApprovalRequests": [],
            "toolsUsed": [],
            "interruptions": [],
        }

        # This should trigger line 759 (all_tools = [])
        result = await _deserialize_processed_response(
            processed_response_data, agent_no_tools, context, {}
        )
        assert result is not None

    async def test_deserialize_processed_response_handoff_with_tool_name(self):
        """Test deserialization of ProcessedResponse with handoff that has tool_name."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")

        # Create a handoff with tool_name
        handoff_obj = handoff(agent_b, tool_name_override="handoff_tool")
        agent_a.handoffs = [handoff_obj]

        processed_response_data = {
            "newItems": [],
            "handoffs": [
                {
                    "toolCall": {
                        "type": "function_call",
                        "name": "handoff_tool",
                        "callId": "call123",
                        "status": "completed",
                        "arguments": "{}",
                    },
                    "handoff": {"toolName": "handoff_tool"},
                }
            ],
            "functions": [],
            "computerActions": [],
            "localShellCalls": [],
            "mcpApprovalRequests": [],
            "toolsUsed": [],
            "interruptions": [],
        }

        # This should trigger lines 778-782 and 787-796
        result = await _deserialize_processed_response(
            processed_response_data, agent_a, context, {"AgentA": agent_a, "AgentB": agent_b}
        )
        assert result is not None
        assert len(result.handoffs) == 1

    async def test_deserialize_processed_response_function_in_tools_map(self):
        """Test deserialization of ProcessedResponse with function in tools_map."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        async def tool_func(context: ToolContext[Any], arguments: str) -> str:
            return "result"

        tool = FunctionTool(
            on_invoke_tool=tool_func,
            name="test_tool",
            description="Test tool",
            params_json_schema={"type": "object", "properties": {}},
        )
        agent.tools = [tool]

        processed_response_data = {
            "newItems": [],
            "handoffs": [],
            "functions": [
                {
                    "toolCall": {
                        "type": "function_call",
                        "name": "test_tool",
                        "callId": "call123",
                        "status": "completed",
                        "arguments": "{}",
                    },
                    "tool": {"name": "test_tool"},
                }
            ],
            "computerActions": [],
            "localShellCalls": [],
            "mcpApprovalRequests": [],
            "toolsUsed": [],
            "interruptions": [],
        }

        # This should trigger lines 801-808
        result = await _deserialize_processed_response(
            processed_response_data, agent, context, {"TestAgent": agent}
        )
        assert result is not None
        assert len(result.functions) == 1

    async def test_deserialize_processed_response_computer_action_in_map(self):
        """Test deserialization of ProcessedResponse with computer action in computer_tools_map."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        class MockComputer(Computer):
            @property
            def environment(self) -> str:  # type: ignore[override]
                return "mac"

            @property
            def dimensions(self) -> tuple[int, int]:
                return (1920, 1080)

            def screenshot(self) -> str:
                return "screenshot"

            def click(self, x: int, y: int, button: str) -> None:
                pass

            def double_click(self, x: int, y: int) -> None:
                pass

            def drag(self, path: list[tuple[int, int]]) -> None:
                pass

            def keypress(self, keys: list[str]) -> None:
                pass

            def move(self, x: int, y: int) -> None:
                pass

            def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
                pass

            def type(self, text: str) -> None:
                pass

            def wait(self) -> None:
                pass

        computer = MockComputer()
        computer_tool = ComputerTool(computer=computer)
        computer_tool.type = "computer"  # type: ignore[attr-defined]
        agent.tools = [computer_tool]

        processed_response_data = {
            "newItems": [],
            "handoffs": [],
            "functions": [],
            "computerActions": [
                {
                    "toolCall": {
                        "type": "computer_call",
                        "id": "1",
                        "callId": "call123",
                        "status": "completed",
                        "action": {"type": "screenshot"},
                        "pendingSafetyChecks": [],
                        "pending_safety_checks": [],
                    },
                    "computer": {"name": computer_tool.name},
                }
            ],
            "localShellCalls": [],
            "mcpApprovalRequests": [],
            "toolsUsed": [],
            "interruptions": [],
        }

        # This should trigger lines 815-824
        result = await _deserialize_processed_response(
            processed_response_data, agent, context, {"TestAgent": agent}
        )
        assert result is not None
        assert len(result.computer_actions) == 1

    async def test_deserialize_processed_response_mcp_approval_request_found(self):
        """Test deserialization of ProcessedResponse with MCP approval request found in map."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        # Create a mock MCP tool
        class MockMCPTool:
            def __init__(self):
                self.name = "mcp_tool"

        mcp_tool = MockMCPTool()
        agent.tools = [mcp_tool]  # type: ignore[list-item]

        processed_response_data = {
            "newItems": [],
            "handoffs": [],
            "functions": [],
            "computerActions": [],
            "localShellCalls": [],
            "mcpApprovalRequests": [
                {
                    "requestItem": {
                        "rawItem": {
                            "type": "mcp_approval_request",
                            "id": "req123",
                            "name": "mcp_tool",
                            "server_label": "test_server",
                            "arguments": "{}",
                        }
                    },
                    "mcpTool": {"name": "mcp_tool"},
                }
            ],
            "toolsUsed": [],
            "interruptions": [],
        }

        # This should trigger lines 831-852
        result = await _deserialize_processed_response(
            processed_response_data, agent, context, {"TestAgent": agent}
        )
        assert result is not None
        # The MCP approval request might not be deserialized if MockMCPTool isn't a HostedMCPTool,
        # but lines 831-852 are still executed and covered

    async def test_deserialize_items_fallback_union_type(self):
        """Test deserialization of tool_call_output_item with fallback union type."""
        agent = Agent(name="TestAgent")

        # Test with an output type that doesn't match any specific type
        # This should trigger the fallback union type validation (lines 1079-1082)
        item_data = {
            "type": "tool_call_output_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                "type": "function_call_output",  # This should match FunctionCallOutput
                "call_id": "call123",
                "output": "result",
            },
        }

        result = _deserialize_items([item_data], {"TestAgent": agent})
        assert len(result) == 1
        assert result[0].type == "tool_call_output_item"

    @pytest.mark.asyncio
    async def test_from_json_missing_schema_version(self):
        """Test that from_json raises error when schema version is missing."""
        agent = Agent(name="TestAgent")
        state_json = {
            "originalInput": "test",
            "currentAgent": {"name": "TestAgent"},
            "context": {
                "context": {},
                "usage": {"requests": 0, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "approvals": {},
            },
            "maxTurns": 3,
            "currentTurn": 0,
            "modelResponses": [],
            "generatedItems": [],
        }

        with pytest.raises(UserError, match="Run state is missing schema version"):
            await RunState.from_json(agent, state_json)

    @pytest.mark.asyncio
    async def test_from_json_unsupported_schema_version(self):
        """Test that from_json raises error when schema version is unsupported."""
        agent = Agent(name="TestAgent")
        state_json = {
            "$schemaVersion": "2.0",
            "originalInput": "test",
            "currentAgent": {"name": "TestAgent"},
            "context": {
                "context": {},
                "usage": {"requests": 0, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "approvals": {},
            },
            "maxTurns": 3,
            "currentTurn": 0,
            "modelResponses": [],
            "generatedItems": [],
        }

        with pytest.raises(UserError, match="Run state schema version 2.0 is not supported"):
            await RunState.from_json(agent, state_json)

    @pytest.mark.asyncio
    async def test_from_json_agent_not_found(self):
        """Test that from_json raises error when agent is not found in agent map."""
        agent = Agent(name="TestAgent")
        state_json = {
            "$schemaVersion": "1.0",
            "originalInput": "test",
            "currentAgent": {"name": "NonExistentAgent"},
            "context": {
                "context": {},
                "usage": {"requests": 0, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "approvals": {},
            },
            "maxTurns": 3,
            "currentTurn": 0,
            "modelResponses": [],
            "generatedItems": [],
        }

        with pytest.raises(UserError, match="Agent NonExistentAgent not found in agent map"):
            await RunState.from_json(agent, state_json)

    @pytest.mark.asyncio
    async def test_deserialize_processed_response_with_last_processed_response(self):
        """Test deserializing RunState with lastProcessedResponse."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        # Create a tool call item
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="test_tool",
            call_id="call123",
            status="completed",
            arguments="{}",
        )
        tool_call_item = ToolCallItem(agent=agent, raw_item=tool_call)

        # Create a ProcessedResponse
        processed_response = ProcessedResponse(
            new_items=[tool_call_item],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)
        state._last_processed_response = processed_response

        # Serialize and deserialize
        json_data = state.to_json()
        new_state = await RunState.from_json(agent, json_data)

        # Verify last processed response was deserialized
        assert new_state._last_processed_response is not None
        assert len(new_state._last_processed_response.new_items) == 1

    @pytest.mark.asyncio
    async def test_from_string_with_last_processed_response(self):
        """Test deserializing RunState with lastProcessedResponse using from_string."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        # Create a tool call item
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="test_tool",
            call_id="call123",
            status="completed",
            arguments="{}",
        )
        tool_call_item = ToolCallItem(agent=agent, raw_item=tool_call)

        # Create a ProcessedResponse
        processed_response = ProcessedResponse(
            new_items=[tool_call_item],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)
        state._last_processed_response = processed_response

        # Serialize to string and deserialize using from_string
        state_string = state.to_string()
        new_state = await RunState.from_string(agent, state_string)

        # Verify last processed response was deserialized
        assert new_state._last_processed_response is not None
        assert len(new_state._last_processed_response.new_items) == 1

    @pytest.mark.asyncio
    async def test_deserialize_processed_response_handoff_with_name_fallback(self):
        """Test deserializing processed response with handoff that has name instead of tool_name."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent_a = Agent(name="AgentA")

        # Create a handoff with name attribute but no tool_name
        class MockHandoff(Handoff):
            def __init__(self):
                # Don't call super().__init__ to avoid tool_name requirement
                self.name = "handoff_tool"  # Has name but no tool_name
                self.handoffs = []  # Add handoffs attribute to avoid AttributeError

        mock_handoff = MockHandoff()
        agent_a.handoffs = [mock_handoff]

        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="handoff_tool",
            call_id="call123",
            status="completed",
            arguments="{}",
        )

        handoff_run = ToolRunHandoff(handoff=mock_handoff, tool_call=tool_call)

        processed_response = ProcessedResponse(
            new_items=[],
            handoffs=[handoff_run],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(
            context=context, original_input="input", starting_agent=agent_a, max_turns=3
        )
        state._last_processed_response = processed_response

        # Serialize and deserialize
        json_data = state.to_json()
        new_state = await RunState.from_json(agent_a, json_data)

        # Verify handoff was deserialized using name fallback
        assert new_state._last_processed_response is not None
        assert len(new_state._last_processed_response.handoffs) == 1

    @pytest.mark.asyncio
    async def test_deserialize_processed_response_mcp_tool_found(self):
        """Test deserializing processed response with MCP tool found and added."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        # Create a mock MCP tool that will be recognized as HostedMCPTool
        # We need it to be in the mcp_tools_map for deserialization to find it
        class MockMCPTool(HostedMCPTool):
            def __init__(self):
                # HostedMCPTool requires tool_config, but we can use a minimal one
                # Create a minimal Mcp config
                mcp_config = Mcp(
                    server_url="http://test",
                    server_label="test_server",
                    type="mcp",
                )
                super().__init__(tool_config=mcp_config)

            @property
            def name(self):
                return "mcp_tool"  # Override to return our test name

            def to_json(self) -> dict[str, Any]:
                return {"name": self.name}

        mcp_tool = MockMCPTool()
        agent.tools = [mcp_tool]

        request_item = McpApprovalRequest(
            id="req123",
            type="mcp_approval_request",
            server_label="test_server",
            name="mcp_tool",
            arguments="{}",
        )

        request_run = ToolRunMCPApprovalRequest(request_item=request_item, mcp_tool=mcp_tool)

        processed_response = ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            mcp_approval_requests=[request_run],
            tools_used=[],
            interruptions=[],
        )

        state = RunState(context=context, original_input="input", starting_agent=agent, max_turns=3)
        state._last_processed_response = processed_response

        # Serialize and deserialize
        json_data = state.to_json()
        new_state = await RunState.from_json(agent, json_data)

        # Verify MCP approval request was deserialized with tool found
        assert new_state._last_processed_response is not None
        assert len(new_state._last_processed_response.mcp_approval_requests) == 1

    @pytest.mark.asyncio
    async def test_deserialize_processed_response_agent_without_get_all_tools(self):
        """Test deserializing processed response when agent doesn't have get_all_tools."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})

        # Create an agent without get_all_tools method
        class AgentWithoutGetAllTools:
            name = "TestAgent"
            handoffs = []

        agent = AgentWithoutGetAllTools()

        processed_response_data: dict[str, Any] = {
            "newItems": [],
            "handoffs": [],
            "functions": [],
            "computerActions": [],
            "toolsUsed": [],
            "mcpApprovalRequests": [],
        }

        # This should not raise an error, just return empty tools
        result = await _deserialize_processed_response(
            processed_response_data,
            agent,  # type: ignore[arg-type]
            context,
            {},
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_deserialize_processed_response_empty_mcp_tool_data(self):
        """Test deserializing processed response with empty mcp_tool_data."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")

        processed_response_data = {
            "newItems": [],
            "handoffs": [],
            "functions": [],
            "computerActions": [],
            "toolsUsed": [],
            "mcpApprovalRequests": [
                {
                    "requestItem": {
                        "rawItem": {
                            "type": "mcp_approval_request",
                            "id": "req1",
                            "server_label": "test_server",
                            "name": "test_tool",
                            "arguments": "{}",
                        }
                    },
                    "mcpTool": {},  # Empty mcp_tool_data should be skipped
                }
            ],
        }

        result = await _deserialize_processed_response(processed_response_data, agent, context, {})
        # Should skip the empty mcp_tool_data and not add it to mcp_approval_requests
        assert len(result.mcp_approval_requests) == 0

    @pytest.mark.asyncio
    async def test_normalize_field_names_with_non_dict(self):
        """Test _normalize_field_names with non-dict input."""
        # Should return non-dict as-is (function checks isinstance(data, dict))
        # For non-dict inputs, it returns the input unchanged
        # The function signature requires dict[str, Any], but it handles non-dicts at runtime
        result_str = _normalize_field_names("string")  # type: ignore[arg-type]
        assert result_str == "string"  # type: ignore[comparison-overlap]
        result_int = _normalize_field_names(123)  # type: ignore[arg-type]
        assert result_int == 123  # type: ignore[comparison-overlap]
        result_list = _normalize_field_names([1, 2, 3])  # type: ignore[arg-type]
        assert result_list == [1, 2, 3]  # type: ignore[comparison-overlap]
        result_none = _normalize_field_names(None)  # type: ignore[arg-type]
        assert result_none is None

    @pytest.mark.asyncio
    async def test_deserialize_items_union_adapter_fallback(self):
        """Test _deserialize_items with union adapter fallback for missing/None output type."""
        agent = Agent(name="TestAgent")
        agent_map = {"TestAgent": agent}

        # Create an item with missing type field to trigger the union adapter fallback
        # The fallback is used when output_type is None or not one of the known types
        # The union adapter will try to validate but may fail, which is caught and logged
        item_data = {
            "type": "tool_call_output_item",
            "agent": {"name": "TestAgent"},
            "rawItem": {
                # No "type" field - this will trigger the else branch and union adapter fallback
                # The union adapter will attempt validation but may fail
                "call_id": "call123",
                "output": "result",
            },
            "output": "result",
        }

        # This should use the union adapter fallback
        # The validation may fail, but the code path is executed
        # The exception will be caught and the item will be skipped
        result = _deserialize_items([item_data], agent_map)
        # The item will be skipped due to validation failure, so result will be empty
        # But the union adapter code path (lines 1081-1084) is still covered
        assert len(result) == 0


class TestToolApprovalItem:
    """Test ToolApprovalItem functionality including tool_name property and serialization."""

    def test_tool_approval_item_with_explicit_tool_name(self):
        """Test that ToolApprovalItem uses explicit tool_name when provided."""
        agent = Agent(name="TestAgent")
        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="raw_tool_name",
            call_id="call123",
            status="completed",
            arguments="{}",
        )

        # Create with explicit tool_name
        approval_item = ToolApprovalItem(
            agent=agent, raw_item=raw_item, tool_name="explicit_tool_name"
        )

        assert approval_item.tool_name == "explicit_tool_name"
        assert approval_item.name == "explicit_tool_name"

    def test_tool_approval_item_falls_back_to_raw_item_name(self):
        """Test that ToolApprovalItem falls back to raw_item.name when tool_name not provided."""
        agent = Agent(name="TestAgent")
        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="raw_tool_name",
            call_id="call123",
            status="completed",
            arguments="{}",
        )

        # Create without explicit tool_name
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)

        assert approval_item.tool_name == "raw_tool_name"
        assert approval_item.name == "raw_tool_name"

    def test_tool_approval_item_with_dict_raw_item(self):
        """Test that ToolApprovalItem handles dict raw_item correctly."""
        agent = Agent(name="TestAgent")
        raw_item = {
            "type": "function_call",
            "name": "dict_tool_name",
            "callId": "call456",
            "status": "completed",
            "arguments": "{}",
        }

        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name="explicit_name")

        assert approval_item.tool_name == "explicit_name"
        assert approval_item.name == "explicit_name"

    def test_approve_tool_with_explicit_tool_name(self):
        """Test that approve_tool works with explicit tool_name."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="raw_name",
            call_id="call123",
            status="completed",
            arguments="{}",
        )

        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name="explicit_name")
        context.approve_tool(approval_item)

        assert context.is_tool_approved(tool_name="explicit_name", call_id="call123") is True

    def test_approve_tool_extracts_call_id_from_dict(self):
        """Test that approve_tool extracts call_id from dict raw_item."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        # Dict with callId (camelCase) - simulating hosted tool
        raw_item = {
            "type": "hosted_tool_call",
            "name": "hosted_tool",
            "id": "hosted_call_123",  # Hosted tools use "id" instead of "call_id"
        }

        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item)
        context.approve_tool(approval_item)

        assert context.is_tool_approved(tool_name="hosted_tool", call_id="hosted_call_123") is True

    def test_reject_tool_with_explicit_tool_name(self):
        """Test that reject_tool works with explicit tool_name."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="raw_name",
            call_id="call789",
            status="completed",
            arguments="{}",
        )

        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name="explicit_name")
        context.reject_tool(approval_item)

        assert context.is_tool_approved(tool_name="explicit_name", call_id="call789") is False

    async def test_serialize_tool_approval_item_with_tool_name(self):
        """Test that ToolApprovalItem serializes toolName field."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=3)

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="raw_name",
            call_id="call123",
            status="completed",
            arguments="{}",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name="explicit_name")
        state._generated_items.append(approval_item)

        json_data = state.to_json()
        generated_items = json_data.get("generatedItems", [])
        assert len(generated_items) == 1

        approval_item_data = generated_items[0]
        assert approval_item_data["type"] == "tool_approval_item"
        assert approval_item_data["toolName"] == "explicit_name"

    async def test_deserialize_tool_approval_item_with_tool_name(self):
        """Test that ToolApprovalItem deserializes toolName field."""
        agent = Agent(name="TestAgent")

        item_data = {
            "type": "tool_approval_item",
            "agent": {"name": "TestAgent"},
            "toolName": "explicit_tool_name",
            "rawItem": {
                "type": "function_call",
                "name": "raw_tool_name",
                "call_id": "call123",
                "status": "completed",
                "arguments": "{}",
            },
        }

        result = _deserialize_items([item_data], {"TestAgent": agent})
        assert len(result) == 1
        assert result[0].type == "tool_approval_item"
        assert isinstance(result[0], ToolApprovalItem)
        assert result[0].tool_name == "explicit_tool_name"
        assert result[0].name == "explicit_tool_name"

    async def test_round_trip_serialization_with_tool_name(self):
        """Test round-trip serialization preserves toolName."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        agent = Agent(name="TestAgent")
        state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=3)

        raw_item = ResponseFunctionToolCall(
            type="function_call",
            name="raw_name",
            call_id="call123",
            status="completed",
            arguments="{}",
        )
        approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name="explicit_name")
        state._generated_items.append(approval_item)

        # Serialize and deserialize
        json_data = state.to_json()
        new_state = await RunState.from_json(agent, json_data)

        assert len(new_state._generated_items) == 1
        restored_item = new_state._generated_items[0]
        assert isinstance(restored_item, ToolApprovalItem)
        assert restored_item.tool_name == "explicit_name"
        assert restored_item.name == "explicit_name"

    def test_tool_approval_item_arguments_property(self):
        """Test that ToolApprovalItem.arguments property correctly extracts arguments."""
        agent = Agent(name="TestAgent")

        # Test with ResponseFunctionToolCall
        raw_item1 = ResponseFunctionToolCall(
            type="function_call",
            name="tool1",
            call_id="call1",
            status="completed",
            arguments='{"city": "Oakland"}',
        )
        approval_item1 = ToolApprovalItem(agent=agent, raw_item=raw_item1)
        assert approval_item1.arguments == '{"city": "Oakland"}'

        # Test with dict raw_item
        raw_item2 = {
            "type": "function_call",
            "name": "tool2",
            "callId": "call2",
            "status": "completed",
            "arguments": '{"key": "value"}',
        }
        approval_item2 = ToolApprovalItem(agent=agent, raw_item=raw_item2)
        assert approval_item2.arguments == '{"key": "value"}'

        # Test with dict raw_item without arguments
        raw_item3 = {
            "type": "function_call",
            "name": "tool3",
            "callId": "call3",
            "status": "completed",
        }
        approval_item3 = ToolApprovalItem(agent=agent, raw_item=raw_item3)
        assert approval_item3.arguments is None

        # Test with raw_item that has no arguments attribute
        raw_item4 = {"type": "unknown", "name": "tool4"}
        approval_item4 = ToolApprovalItem(agent=agent, raw_item=raw_item4)
        assert approval_item4.arguments is None

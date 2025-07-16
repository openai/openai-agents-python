"""Tests for realtime handoff functionality."""

from unittest.mock import Mock

import pytest

from agents import Agent
from agents.realtime import RealtimeAgent, realtime_handoff


def test_realtime_handoff_creation():
    """Test basic realtime handoff creation."""
    realtime_agent = RealtimeAgent(name="test_agent")
    handoff_obj = realtime_handoff(realtime_agent)

    assert handoff_obj.agent_name == "test_agent"
    assert handoff_obj.tool_name == "transfer_to_test_agent"
    assert handoff_obj.input_filter is None  # Should not support input filters
    assert handoff_obj.is_enabled is True


def test_realtime_handoff_with_custom_params():
    """Test realtime handoff with custom parameters."""
    realtime_agent = RealtimeAgent(
        name="helper_agent",
        handoff_description="Helps with general tasks",
    )

    handoff_obj = realtime_handoff(
        realtime_agent,
        tool_name_override="custom_handoff",
        tool_description_override="Custom handoff description",
        is_enabled=False,
    )

    assert handoff_obj.agent_name == "helper_agent"
    assert handoff_obj.tool_name == "custom_handoff"
    assert handoff_obj.tool_description == "Custom handoff description"
    assert handoff_obj.is_enabled is False


@pytest.mark.asyncio
async def test_realtime_handoff_execution():
    """Test that realtime handoff returns the correct agent."""
    realtime_agent = RealtimeAgent(name="target_agent")
    handoff_obj = realtime_handoff(realtime_agent)

    # Mock context
    mock_context = Mock()

    # Execute handoff
    result = await handoff_obj.on_invoke_handoff(mock_context, "")

    assert result is realtime_agent
    assert isinstance(result, RealtimeAgent)


def test_realtime_handoff_with_on_handoff_callback():
    """Test realtime handoff with custom on_handoff callback."""
    realtime_agent = RealtimeAgent(name="callback_agent")
    callback_called = []

    def on_handoff_callback(ctx):
        callback_called.append(True)

    handoff_obj = realtime_handoff(
        realtime_agent,
        on_handoff=on_handoff_callback,
    )

    assert handoff_obj.agent_name == "callback_agent"


def test_regular_agent_handoff_still_works():
    """Test that regular Agent handoffs still work with the new generic types."""
    from agents import handoff

    regular_agent = Agent(name="regular_agent")
    handoff_obj = handoff(regular_agent)

    assert handoff_obj.agent_name == "regular_agent"
    assert handoff_obj.tool_name == "transfer_to_regular_agent"
    # Regular agent handoffs should support input filters
    assert hasattr(handoff_obj, "input_filter")


def test_type_annotations_work():
    """Test that type annotations work correctly."""
    from agents.handoffs import Handoff
    from agents.realtime.handoffs import realtime_handoff

    realtime_agent = RealtimeAgent(name="typed_agent")
    handoff_obj = realtime_handoff(realtime_agent)

    # This should be typed as Handoff[Any, RealtimeAgent[Any]]
    assert isinstance(handoff_obj, Handoff)

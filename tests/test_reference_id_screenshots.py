"""Tests for the reference ID screenshot mechanism in ComputerAction."""

from unittest.mock import MagicMock, patch

import pytest
from agents import Agent, RunConfig, RunContextWrapper, RunHooks
from agents._run_impl import ComputerAction, ToolRunComputerAction
from agents.items import ToolCallOutputItem
from agents.tool import ComputerTool
from openai.types.responses.response_computer_tool_call import (
    ActionClick,
    ResponseComputerToolCall,
)
from tests.test_computer_action import LoggingComputer


@pytest.mark.asyncio
@patch("agents.models.openai_provider._openai_shared.get_default_openai_client")
@patch("openai.AsyncOpenAI")
async def test_reference_id_screenshots(mock_openai, mock_get_client):
    """Test that screenshots are sent with reference IDs."""
    # Mock the OpenAI client to avoid API key requirement
    mock_get_client.return_value = MagicMock()
    computer = LoggingComputer(screenshot_return="test_screenshot")
    comptool = ComputerTool(computer=computer)
    # Create a dummy click action
    action = ActionClick(type="click", x=1, y=2, button="left")
    tool_call = ResponseComputerToolCall(
        id="tool1",
        type="computer_call",
        action=action,
        call_id="tool1",
        pending_safety_checks=[],
        status="completed",
    )
    # Setup agent and hooks
    agent = Agent(name="test_agent", tools=[comptool])
    run_hooks = RunHooks()
    context_wrapper = RunContextWrapper(context=None)
    # Execute the computer action
    output_item = await ComputerAction.execute(
        agent=agent,
        action=ToolRunComputerAction(tool_call=tool_call, computer_tool=comptool),
        hooks=run_hooks,
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )
    # Verify that the output item has the correct structure
    assert isinstance(output_item, ToolCallOutputItem)
    assert "data:image/png;base64," in output_item.output
    # Verify that the screenshot was generated
    screenshot_calls = [call for call in computer.calls if call[0] == "screenshot"]
    assert len(screenshot_calls) == 1

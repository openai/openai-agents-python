"""Tests for the screenshot caching mechanism in ComputerAction."""

import pytest
from openai.types.responses.response_computer_tool_call import (
    ActionClick,
    ResponseComputerToolCall,
)

from agents import Agent, RunConfig, RunContextWrapper, RunHooks
from agents._run_impl import ComputerAction, ToolRunComputerAction
from agents.items import ToolCallOutputItem
from agents.tool import ComputerTool
from tests.test_computer_action import LoggingAsyncComputer, LoggingComputer


@pytest.mark.asyncio
async def test_screenshot_cache_sync():
    """Test that the same screenshot is not sent twice."""
    # Clear the cache
    ComputerAction._screenshot_cache = {}

    # Create a computer that always returns the same screenshot
    computer = LoggingComputer(screenshot_return="same_screenshot")
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

    # Execute the computer action twice
    output_item1 = await ComputerAction.execute(
        agent=agent,
        action=ToolRunComputerAction(tool_call=tool_call, computer_tool=comptool),
        hooks=run_hooks,
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    output_item2 = await ComputerAction.execute(
        agent=agent,
        action=ToolRunComputerAction(tool_call=tool_call, computer_tool=comptool),
        hooks=run_hooks,
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    # Verify that the screenshot_hash method was called
    assert ("screenshot_hash", ()) in computer.calls

    # Verify that both output items have the same image_url
    # ToolCallOutputItem has output as a property
    assert isinstance(output_item1, ToolCallOutputItem)
    assert isinstance(output_item2, ToolCallOutputItem)
    assert output_item1.output == output_item2.output

    # Verify that the cache was used (screenshot was only generated once)
    screenshot_calls = [call for call in computer.calls if call[0] == "screenshot"]
    assert len(screenshot_calls) == 1


@pytest.mark.asyncio
async def test_screenshot_cache_async():
    """Test that the same screenshot is not sent twice with AsyncComputer."""
    # Clear the cache
    ComputerAction._screenshot_cache = {}

    # Create a computer that always returns the same screenshot
    computer = LoggingAsyncComputer(screenshot_return="same_async_screenshot")
    comptool = ComputerTool(computer=computer)
    # Create a dummy click action
    action = ActionClick(type="click", x=1, y=2, button="left")
    tool_call = ResponseComputerToolCall(
        id="tool2",
        type="computer_call",
        action=action,
        call_id="tool2",
        pending_safety_checks=[],
        status="completed",
    )

    # Setup agent and hooks
    agent = Agent(name="test_agent", tools=[comptool])
    run_hooks = RunHooks()
    context_wrapper = RunContextWrapper(context=None)

    # Execute the computer action twice
    output_item1 = await ComputerAction.execute(
        agent=agent,
        action=ToolRunComputerAction(tool_call=tool_call, computer_tool=comptool),
        hooks=run_hooks,
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    output_item2 = await ComputerAction.execute(
        agent=agent,
        action=ToolRunComputerAction(tool_call=tool_call, computer_tool=comptool),
        hooks=run_hooks,
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    # Verify that the screenshot_hash method was called
    assert ("screenshot_hash", ()) in computer.calls

    # Verify that both output items have the same image_url
    # ToolCallOutputItem has output as a property
    assert isinstance(output_item1, ToolCallOutputItem)
    assert isinstance(output_item2, ToolCallOutputItem)
    assert output_item1.output == output_item2.output

    # Verify that the cache was used (screenshot was only generated once)
    screenshot_calls = [call for call in computer.calls if call[0] == "screenshot"]
    assert len(screenshot_calls) == 1

from __future__ import annotations

from typing import cast

import pytest

from agents import Agent, RunConfig, Runner
from agents.run import get_current_run_config, set_current_run_config
from agents.tool import function_tool

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_run_config_inheritance_enabled():
    """Test that run_config is inherited when pass_run_config_to_sub_agents=True"""
    inherited_configs = []

    @function_tool
    async def config_capture_tool() -> str:
        """Tool that captures the current run config"""
        current_config = get_current_run_config()
        inherited_configs.append(current_config)
        return "config_captured"

    sub_agent = Agent(
        name="SubAgent",
        instructions="You are a sub agent",
        model=FakeModel(),
        tools=[config_capture_tool],
    )

    sub_fake_model = cast(FakeModel, sub_agent.model)
    sub_fake_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("config_capture_tool", "{}")],
            [get_text_message("sub_agent_response")],
        ]
    )

    parent_agent = Agent(
        name="ParentAgent",
        instructions="You are a parent agent",
        model=FakeModel(),
        tools=[
            sub_agent.as_tool(tool_name="sub_agent_tool", tool_description="Call the sub agent")
        ],
    )

    parent_fake_model = cast(FakeModel, parent_agent.model)
    parent_fake_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("sub_agent_tool", '{"input": "test"}')],
            [get_text_message("parent_response")],
        ]
    )

    run_config = RunConfig(pass_run_config_to_sub_agents=True)

    # Ensure clean state before test
    assert get_current_run_config() is None

    await Runner.run(
        starting_agent=parent_agent,
        input="Use the sub agent tool",
        run_config=run_config,
    )

    # Verify context is cleaned up after execution
    assert get_current_run_config() is None

    # Verify the sub-agent received the inherited config
    assert len(inherited_configs) == 1
    assert inherited_configs[0] is run_config
    assert inherited_configs[0].pass_run_config_to_sub_agents is True


@pytest.mark.asyncio
async def test_run_config_inheritance_disabled():
    """Test that run_config is not inherited when pass_run_config_to_sub_agents=False"""
    inherited_configs = []

    @function_tool
    async def config_capture_tool() -> str:
        """Tool that captures the current run config"""
        current_config = get_current_run_config()
        inherited_configs.append(current_config)
        return "config_captured"

    sub_agent = Agent(
        name="SubAgent",
        instructions="You are a sub agent",
        model=FakeModel(),
        tools=[config_capture_tool],
    )

    sub_fake_model = cast(FakeModel, sub_agent.model)
    sub_fake_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("config_capture_tool", "{}")],
            [get_text_message("sub_agent_response")],
        ]
    )

    parent_agent = Agent(
        name="ParentAgent",
        instructions="You are a parent agent",
        model=FakeModel(),
        tools=[
            sub_agent.as_tool(tool_name="sub_agent_tool", tool_description="Call the sub agent")
        ],
    )

    parent_fake_model = cast(FakeModel, parent_agent.model)
    parent_fake_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("sub_agent_tool", '{"input": "test"}')],
            [get_text_message("parent_response")],
        ]
    )

    # Default RunConfig has pass_run_config_to_sub_agents=False
    run_config = RunConfig()

    await Runner.run(
        starting_agent=parent_agent,
        input="Use the sub agent tool",
        run_config=run_config,
    )

    assert get_current_run_config() is None
    assert len(inherited_configs) == 1
    assert inherited_configs[0] is None


@pytest.mark.asyncio
async def test_context_variable_cleanup_on_error():
    """Test that context variable is cleaned up even when errors occur"""
    failing_model = FakeModel()
    failing_model.set_next_output(RuntimeError("Intentional test failure"))

    failing_agent = Agent(
        name="FailingAgent",
        instructions="Fail",
        model=failing_model,
    )

    run_config = RunConfig(pass_run_config_to_sub_agents=True)

    assert get_current_run_config() is None

    with pytest.raises(RuntimeError, match="Intentional test failure"):
        await Runner.run(
            starting_agent=failing_agent,
            input="This should fail",
            run_config=run_config,
        )

    # Verify context is cleaned up even after error
    assert get_current_run_config() is None


@pytest.mark.asyncio
async def test_context_var_methods_directly():
    """Test the ContextVar methods directly for RunConfig management"""
    run_config = RunConfig(pass_run_config_to_sub_agents=True)

    assert get_current_run_config() is None

    # Test setting and getting
    token = set_current_run_config(run_config)
    assert get_current_run_config() is run_config

    # Test resetting using token - use the proper API
    set_current_run_config(token.old_value)
    assert get_current_run_config() is None

    # Test setting None
    token = set_current_run_config(None)
    assert get_current_run_config() is None
    set_current_run_config(token.old_value)


@pytest.mark.asyncio
async def test_streaming_run_config_inheritance():
    """Test that run_config inheritance works with streaming execution"""
    inherited_configs = []

    @function_tool
    async def config_capture_tool() -> str:
        """Tool that captures the current run config"""
        current_config = get_current_run_config()
        inherited_configs.append(current_config)
        return "config_captured"

    sub_agent = Agent(
        name="SubAgent",
        instructions="You are a sub agent",
        model=FakeModel(),
        tools=[config_capture_tool],
    )

    sub_fake_model = cast(FakeModel, sub_agent.model)
    sub_fake_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("config_capture_tool", "{}")],
            [get_text_message("sub_agent_response")],
        ]
    )

    parent_agent = Agent(
        name="ParentAgent",
        instructions="You are a parent agent",
        model=FakeModel(),
        tools=[
            sub_agent.as_tool(tool_name="sub_agent_tool", tool_description="Call the sub agent")
        ],
    )

    parent_fake_model = cast(FakeModel, parent_agent.model)
    parent_fake_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("sub_agent_tool", '{"input": "test"}')],
            [get_text_message("parent_response")],
        ]
    )

    run_config = RunConfig(pass_run_config_to_sub_agents=True)

    # Test with streaming execution
    result = Runner.run_streamed(
        starting_agent=parent_agent,
        input="Use the sub agent tool",
        run_config=run_config,
    )

    async for _ in result.stream_events():
        pass

    # Verify inheritance worked in streaming mode
    assert get_current_run_config() is None
    assert len(inherited_configs) == 1
    assert inherited_configs[0] is run_config

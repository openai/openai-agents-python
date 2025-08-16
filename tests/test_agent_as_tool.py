import pytest
from pydantic import BaseModel

from agents import Agent, AgentBase, FunctionTool, RunContextWrapper


class BoolCtx(BaseModel):
    enable_tools: bool


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_bool():
    """Test that agent.as_tool() respects static boolean is_enabled parameter."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create tool with is_enabled=False
    disabled_tool = agent.as_tool(
        tool_name="disabled_agent_tool",
        tool_description="A disabled agent tool",
        is_enabled=False,
    )

    # Create tool with is_enabled=True (default)
    enabled_tool = agent.as_tool(
        tool_name="enabled_agent_tool",
        tool_description="An enabled agent tool",
        is_enabled=True,
    )

    # Create another tool with default is_enabled (should be True)
    default_tool = agent.as_tool(
        tool_name="default_agent_tool",
        tool_description="A default agent tool",
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[disabled_tool, enabled_tool, default_tool],
    )

    # Test with any context
    context = RunContextWrapper(BoolCtx(enable_tools=True))

    # Get all tools - should filter out the disabled one
    tools = await orchestrator.get_all_tools(context)
    tool_names = [tool.name for tool in tools]

    assert "enabled_agent_tool" in tool_names
    assert "default_agent_tool" in tool_names
    assert "disabled_agent_tool" not in tool_names


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_callable():
    """Test that agent.as_tool() respects callable is_enabled parameter."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create tool with callable is_enabled
    async def cond_enabled(ctx: RunContextWrapper[BoolCtx], agent: AgentBase) -> bool:
        return ctx.context.enable_tools

    conditional_tool = agent.as_tool(
        tool_name="conditional_agent_tool",
        tool_description="A conditionally enabled agent tool",
        is_enabled=cond_enabled,
    )

    # Create tool with lambda is_enabled
    lambda_tool = agent.as_tool(
        tool_name="lambda_agent_tool",
        tool_description="A lambda enabled agent tool",
        is_enabled=lambda ctx, agent: ctx.context.enable_tools,
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[conditional_tool, lambda_tool],
    )

    # Test with enable_tools=False
    context_disabled = RunContextWrapper(BoolCtx(enable_tools=False))
    tools_disabled = await orchestrator.get_all_tools(context_disabled)
    assert len(tools_disabled) == 0

    # Test with enable_tools=True
    context_enabled = RunContextWrapper(BoolCtx(enable_tools=True))
    tools_enabled = await orchestrator.get_all_tools(context_enabled)
    tool_names = [tool.name for tool in tools_enabled]

    assert len(tools_enabled) == 2
    assert "conditional_agent_tool" in tool_names
    assert "lambda_agent_tool" in tool_names


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_mixed():
    """Test agent.as_tool() with mixed enabled/disabled tools."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create various tools with different is_enabled configurations
    always_enabled = agent.as_tool(
        tool_name="always_enabled",
        tool_description="Always enabled tool",
        is_enabled=True,
    )

    always_disabled = agent.as_tool(
        tool_name="always_disabled",
        tool_description="Always disabled tool",
        is_enabled=False,
    )

    conditionally_enabled = agent.as_tool(
        tool_name="conditionally_enabled",
        tool_description="Conditionally enabled tool",
        is_enabled=lambda ctx, agent: ctx.context.enable_tools,
    )

    default_enabled = agent.as_tool(
        tool_name="default_enabled",
        tool_description="Default enabled tool",
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[always_enabled, always_disabled, conditionally_enabled, default_enabled],
    )

    # Test with enable_tools=False
    context_disabled = RunContextWrapper(BoolCtx(enable_tools=False))
    tools_disabled = await orchestrator.get_all_tools(context_disabled)
    tool_names_disabled = [tool.name for tool in tools_disabled]

    assert len(tools_disabled) == 2
    assert "always_enabled" in tool_names_disabled
    assert "default_enabled" in tool_names_disabled
    assert "always_disabled" not in tool_names_disabled
    assert "conditionally_enabled" not in tool_names_disabled

    # Test with enable_tools=True
    context_enabled = RunContextWrapper(BoolCtx(enable_tools=True))
    tools_enabled = await orchestrator.get_all_tools(context_enabled)
    tool_names_enabled = [tool.name for tool in tools_enabled]

    assert len(tools_enabled) == 3
    assert "always_enabled" in tool_names_enabled
    assert "default_enabled" in tool_names_enabled
    assert "conditionally_enabled" in tool_names_enabled
    assert "always_disabled" not in tool_names_enabled


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_preserves_other_params():
    """Test that is_enabled parameter doesn't interfere with other agent.as_tool() parameters."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that returns a greeting.",
    )

    # Custom output extractor
    async def custom_extractor(result):
        return f"CUSTOM: {result.new_items[-1].text if result.new_items else 'No output'}"

    # Create tool with all parameters including is_enabled
    tool = agent.as_tool(
        tool_name="custom_tool_name",
        tool_description="A custom tool with all parameters",
        custom_output_extractor=custom_extractor,
        is_enabled=True,
    )

    # Verify the tool was created with correct properties
    assert tool.name == "custom_tool_name"
    assert isinstance(tool, FunctionTool)
    assert tool.description == "A custom tool with all parameters"
    assert tool.is_enabled is True

    # Verify tool is included when enabled
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[tool],
    )

    context = RunContextWrapper(BoolCtx(enable_tools=True))
    tools = await orchestrator.get_all_tools(context)
    assert len(tools) == 1
    assert tools[0].name == "custom_tool_name"

from __future__ import annotations

from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import BaseModel

from agents import (
    Agent,
    AgentBase,
    FunctionTool,
    MessageOutputItem,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    Session,
    TResponseInputItem,
)
from agents.tool_context import ToolContext


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


@pytest.mark.asyncio
async def test_agent_as_tool_returns_concatenated_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent tool should use default text aggregation when no custom extractor is provided."""

    agent = Agent(name="storyteller")

    message = ResponseOutputMessage(
        id="msg_1",
        role="assistant",
        status="completed",
        type="message",
        content=[
            ResponseOutputText(
                annotations=[],
                text="Hello world",
                type="output_text",
                logprobs=None,
            )
        ],
    )

    result = type(
        "DummyResult",
        (),
        {"new_items": [MessageOutputItem(agent=agent, raw_item=message)]},
    )()

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        return result

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="story_tool",
        tool_description="Tell a short story",
        is_enabled=True,
    )

    assert isinstance(tool, FunctionTool)
    tool_context = ToolContext(
        context=None,
        tool_name="story_tool",
        tool_call_id="call_1",
        tool_arguments='{"input": "hello"}',
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "hello"}')

    assert output == "Hello world"


@pytest.mark.asyncio
async def test_agent_as_tool_custom_output_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom output extractors should receive the RunResult from Runner.run."""

    agent = Agent(name="summarizer")

    message = ResponseOutputMessage(
        id="msg_2",
        role="assistant",
        status="completed",
        type="message",
        content=[
            ResponseOutputText(
                annotations=[],
                text="Original text",
                type="output_text",
                logprobs=None,
            )
        ],
    )

    class DummySession(Session):
        session_id = "sess_123"

        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            return []

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            return None

        async def pop_item(self) -> TResponseInputItem | None:
            return None

        async def clear_session(self) -> None:
            return None

    dummy_session = DummySession()

    class DummyResult:
        def __init__(self, items: list[MessageOutputItem]) -> None:
            self.new_items = items

    run_result = DummyResult([MessageOutputItem(agent=agent, raw_item=message)])

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "summarize this"
        assert context is None
        assert max_turns == 7
        assert hooks is hooks_obj
        assert run_config is run_config_obj
        assert previous_response_id == "resp_1"
        assert conversation_id == "conv_1"
        assert session is dummy_session
        return run_result

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    async def extractor(result) -> str:
        assert result is run_result
        return "custom output"

    hooks_obj = RunHooks[Any]()
    run_config_obj = RunConfig(model="gpt-4.1-mini")

    tool = agent.as_tool(
        tool_name="summary_tool",
        tool_description="Summarize input",
        custom_output_extractor=extractor,
        is_enabled=True,
        run_config=run_config_obj,
        max_turns=7,
        hooks=hooks_obj,
        previous_response_id="resp_1",
        conversation_id="conv_1",
        session=dummy_session,
    )

    assert isinstance(tool, FunctionTool)
    tool_context = ToolContext(
        context=None,
        tool_name="summary_tool",
        tool_call_id="call_2",
        tool_arguments='{"input": "summarize this"}',
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "summarize this"}')

    assert output == "custom output"

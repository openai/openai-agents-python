import asyncio
import pytest
from pydantic import BaseModel

from agents import Agent, RunContextWrapper, Runner, function_tool


class UserDataType(BaseModel):
    """Test context type with an age attribute."""
    name: str
    age: int


def dynamic_instructions(ctx: RunContextWrapper[UserDataType], agent: Agent[UserDataType]) -> str:
    """Dynamic instructions that access ctx.context.age directly."""
    # This should work - direct access to the generic type
    return f"You are helping {ctx.context.name} who is {ctx.context.age} years old."


@function_tool
def tool_with_context_access(ctx: RunContextWrapper[UserDataType]) -> str:
    """Tool that tries to access ctx.context.age directly."""
    # This currently fails with AttributeError: 'dict' object has no attribute 'age'
    # and requires manual conversion: UserDataType.model_validate(ctx.context)
    return f"Tool called for {ctx.context.name} who is {ctx.context.age} years old."


@function_tool
def tool_with_manual_conversion(ctx: RunContextWrapper[UserDataType]) -> str:
    """Tool that manually converts ctx.context to the proper type."""
    # This is the current workaround
    user_data = UserDataType.model_validate(ctx.context)
    return f"Tool called for {user_data.name} who is {user_data.age} years old."


@pytest.mark.asyncio
async def test_dynamic_instructions_work():
    """Test that dynamic instructions can access ctx.context.age directly."""
    user_context = UserDataType(name="Alice", age=30)

    agent = Agent(
        name="Test Agent",
        instructions=dynamic_instructions,
    )

    # This should work without issues
    result = await agent.get_system_prompt(RunContextWrapper(context=user_context))
    assert result == "You are helping Alice who is 30 years old."


@pytest.mark.asyncio
async def test_tool_validation_fails_with_direct_access():
    """Test that tool validation currently fails when accessing ctx.context.age directly."""
    user_context = UserDataType(name="Alice", age=30)

    agent = Agent(
        name="Test Agent",
        instructions="You are a helpful assistant.",
        tools=[tool_with_context_access],
    )

    # This should fail with AttributeError: 'dict' object has no attribute 'age'
    with pytest.raises(AttributeError, match="'dict' object has no attribute 'age'"):
        await Runner.run(
            agent,
            "Call the tool",
            context=user_context,
        )


@pytest.mark.asyncio
async def test_tool_validation_works_with_manual_conversion():
    """Test that tool validation works when manually converting ctx.context."""
    user_context = UserDataType(name="Alice", age=30)

    agent = Agent(
        name="Test Agent",
        instructions="You are a helpful assistant. Call the tool.",
        tools=[tool_with_manual_conversion],
    )

    # This should work with manual conversion
    result = await Runner.run(
        agent,
        "Call the tool",
        context=user_context,
    )

    # The tool should have been called successfully
    assert "Tool called for Alice who is 30 years old" in str(
        result.final_output)


@pytest.mark.asyncio
async def test_generic_type_consistency_after_fix():
    """Test that both dynamic instructions and tool validation work consistently after the fix."""
    user_context = UserDataType(name="Bob", age=25)

    agent = Agent(
        name="Test Agent",
        instructions=dynamic_instructions,
        tools=[tool_with_context_access],
    )

    # After the fix, this should work without manual conversion
    result = await Runner.run(
        agent,
        "Call the tool",
        context=user_context,
    )

    # Both dynamic instructions and tool should work
    assert "Tool called for Bob who is 25 years old" in str(
        result.final_output)


def test_tool_context_preserves_generic_type():
    """Test that ToolContext.from_agent_context preserves generic type information."""
    # Create a RunContextWrapper with typed context
    user_data = UserDataType(name="Alice", age=30)
    run_context = RunContextWrapper(context=user_data)

    # Create ToolContext using from_agent_context (this is what happens in tool validation)
    tool_context = ToolContext.from_agent_context(
        context=run_context,
        tool_call_id="test_call_123"
    )

    # After the fix, the context should maintain its type
    assert isinstance(tool_context.context, UserDataType)
    assert tool_context.context.age == 30
    assert tool_context.context.name == "Alice"

    # Direct access should work without AttributeError
    assert tool_context.context.age == 30
    assert tool_context.context.name == "Alice"


def test_tool_context_with_none_context():
    """Test that ToolContext.from_agent_context works with None context."""
    # Create a RunContextWrapper with None context
    run_context = RunContextWrapper(context=None)

    # Create ToolContext using from_agent_context
    tool_context = ToolContext.from_agent_context(
        context=run_context,
        tool_call_id="test_call_123"
    )

    # The context should be None
    assert tool_context.context is None


def test_tool_context_with_dict_context():
    """Test that ToolContext.from_agent_context works with dict context."""
    # Create a RunContextWrapper with dict context
    dict_context = {"name": "Charlie", "age": 35}
    run_context = RunContextWrapper(context=dict_context)

    # Create ToolContext using from_agent_context
    tool_context = ToolContext.from_agent_context(
        context=run_context,
        tool_call_id="test_call_123"
    )

    # The context should maintain its dict type
    assert isinstance(tool_context.context, dict)
    assert tool_context.context["name"] == "Charlie"
    assert tool_context.context["age"] == 35


def test_tool_context_usage_preservation():
    """Test that ToolContext.from_agent_context preserves usage information."""
    from agents.usage import Usage

    # Create a RunContextWrapper with custom usage
    user_data = UserDataType(name="Dave", age=40)
    custom_usage = Usage()
    custom_usage.total_tokens = 100
    run_context = RunContextWrapper(context=user_data, usage=custom_usage)

    # Create ToolContext using from_agent_context
    tool_context = ToolContext.from_agent_context(
        context=run_context,
        tool_call_id="test_call_123"
    )

    # The usage should be preserved
    assert tool_context.usage is custom_usage
    assert tool_context.usage.total_tokens == 100

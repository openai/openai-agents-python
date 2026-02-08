import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents import Agent
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext
from tests.utils.hitl import make_context_wrapper


def test_tool_context_requires_fields() -> None:
    ctx: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    with pytest.raises(ValueError):
        ToolContext.from_agent_context(ctx, tool_call_id="call-1")


def test_tool_context_missing_defaults_raise() -> None:
    base_ctx: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    with pytest.raises(ValueError):
        ToolContext(context=base_ctx.context, tool_call_id="call-1", tool_arguments="")
    with pytest.raises(ValueError):
        ToolContext(context=base_ctx.context, tool_name="name", tool_arguments="")
    with pytest.raises(ValueError):
        ToolContext(context=base_ctx.context, tool_name="name", tool_call_id="call-1")


def test_tool_context_from_agent_context_populates_fields() -> None:
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-123",
        arguments='{"a": 1}',
    )
    ctx = make_context_wrapper()

    tool_ctx = ToolContext.from_agent_context(ctx, tool_call_id="call-123", tool_call=tool_call)

    assert tool_ctx.tool_name == "test_tool"
    assert tool_ctx.tool_call_id == "call-123"
    assert tool_ctx.tool_arguments == '{"a": 1}'


def test_tool_context_agent_none_by_default() -> None:
    """Agent field defaults to None for backward compatibility."""
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-1",
        arguments="{}",
    )
    ctx = make_context_wrapper()
    tool_ctx = ToolContext.from_agent_context(ctx, tool_call_id="call-1", tool_call=tool_call)
    assert tool_ctx.agent is None


def test_tool_context_agent_from_agent_context() -> None:
    """Agent is populated when passed to from_agent_context."""
    agent = Agent(name="test-agent", instructions="do stuff")
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-2",
        arguments="{}",
    )
    ctx = make_context_wrapper()
    tool_ctx = ToolContext.from_agent_context(
        ctx, tool_call_id="call-2", tool_call=tool_call, agent=agent
    )
    assert tool_ctx.agent is agent
    assert tool_ctx.agent.name == "test-agent"


def test_tool_context_agent_via_constructor() -> None:
    """Agent is accessible when passed directly to the ToolContext constructor."""
    agent = Agent(name="direct-agent", instructions="hi")
    tool_ctx: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="my_tool",
        tool_call_id="call-3",
        tool_arguments="{}",
        agent=agent,
    )
    assert tool_ctx.agent is agent
    assert tool_ctx.agent.name == "direct-agent"

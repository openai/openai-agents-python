import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents import Agent
from agents.run_config import RunConfig
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
    agent = Agent(name="agent")

    tool_ctx = ToolContext.from_agent_context(
        ctx,
        tool_call_id="call-123",
        tool_call=tool_call,
        agent=agent,
    )

    assert tool_ctx.tool_name == "test_tool"
    assert tool_ctx.tool_call_id == "call-123"
    assert tool_ctx.tool_arguments == '{"a": 1}'
    assert tool_ctx.agent is agent


def test_tool_context_agent_none_by_default() -> None:
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-1",
        arguments="{}",
    )
    ctx = make_context_wrapper()

    tool_ctx = ToolContext.from_agent_context(ctx, tool_call_id="call-1", tool_call=tool_call)

    assert tool_ctx.agent is None


def test_tool_context_constructor_accepts_agent_keyword() -> None:
    agent = Agent(name="direct-agent")
    tool_ctx: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="my_tool",
        tool_call_id="call-2",
        tool_arguments="{}",
        agent=agent,
    )

    assert tool_ctx.agent is agent


def test_tool_context_from_tool_context_inherits_agent() -> None:
    original_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-3",
        arguments="{}",
    )
    derived_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-4",
        arguments="{}",
    )
    agent = Agent(name="origin-agent")
    parent_context: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="test_tool",
        tool_call_id="call-3",
        tool_arguments="{}",
        tool_call=original_call,
        agent=agent,
    )

    derived_context = ToolContext.from_agent_context(
        parent_context,
        tool_call_id="call-4",
        tool_call=derived_call,
    )

    assert derived_context.agent is agent


def test_tool_context_from_tool_context_inherits_run_config() -> None:
    original_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-3",
        arguments="{}",
    )
    derived_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-4",
        arguments="{}",
    )
    parent_run_config = RunConfig(model="gpt-4.1-mini")
    parent_context: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="test_tool",
        tool_call_id="call-3",
        tool_arguments="{}",
        tool_call=original_call,
        run_config=parent_run_config,
    )

    derived_context = ToolContext.from_agent_context(
        parent_context,
        tool_call_id="call-4",
        tool_call=derived_call,
    )

    assert derived_context.run_config is parent_run_config


def test_tool_context_from_agent_context_prefers_explicit_run_config() -> None:
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="test_tool",
        call_id="call-1",
        arguments="{}",
    )
    ctx = make_context_wrapper()
    explicit_run_config = RunConfig(model="gpt-4.1")

    tool_ctx = ToolContext.from_agent_context(
        ctx,
        tool_call_id="call-1",
        tool_call=tool_call,
        run_config=explicit_run_config,
    )

    assert tool_ctx.run_config is explicit_run_config

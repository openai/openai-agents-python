import pytest
from openai.types.responses import ResponseFunctionToolCall

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

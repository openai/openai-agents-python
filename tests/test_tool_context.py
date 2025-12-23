import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents.run_context import RunContextWrapper
from agents.tool_context import (
    ToolContext,
    _assert_must_pass_tool_arguments,
    _assert_must_pass_tool_call_id,
    _assert_must_pass_tool_name,
)
from tests.utils.hitl import make_context_wrapper


def test_tool_context_requires_fields() -> None:
    ctx: RunContextWrapper[dict[str, object]] = RunContextWrapper(context={})
    with pytest.raises(ValueError):
        ToolContext.from_agent_context(ctx, tool_call_id="call-1")


def test_tool_context_missing_defaults_raise() -> None:
    with pytest.raises(ValueError):
        _assert_must_pass_tool_call_id()
    with pytest.raises(ValueError):
        _assert_must_pass_tool_name()
    with pytest.raises(ValueError):
        _assert_must_pass_tool_arguments()


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

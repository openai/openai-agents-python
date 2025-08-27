import json
from dataclasses import fields
from types import SimpleNamespace
from typing import Optional

import pytest

from agents import function_tool
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext


class FakeToolCall:
    def __init__(self, name: str, arguments: Optional[str] = None):
        self.name = name
        self.arguments = arguments


def make_minimal_context_like_runcontext():
    ctx = SimpleNamespace()
    for f in fields(RunContextWrapper):
        setattr(ctx, f.name, None)
    return ctx


def test_from_agent_context_populates_arguments_and_names():
    context_like = make_minimal_context_like_runcontext()
    fake_call = FakeToolCall(name="my_tool", arguments='{"x": 1, "y": 2}')

    tc: ToolContext = ToolContext.from_agent_context(
        context_like, tool_call_id="c-1", tool_call=fake_call
    )

    assert tc.tool_name == "my_tool"
    assert tc.tool_call_id == "c-1"
    assert tc.arguments == '{"x": 1, "y": 2}'


def test_from_agent_context_raises_if_tool_name_missing():
    context_like = make_minimal_context_like_runcontext()

    with pytest.raises(ValueError, match="Tool name must"):
        ToolContext.from_agent_context(context_like, tool_call_id="c-2", tool_call=None)


@pytest.mark.asyncio
async def test_function_tool_accepts_toolcontext_generic_argless():
    def argless_with_context(ctx: ToolContext[str]) -> str:
        return "ok"

    tool = function_tool(argless_with_context)
    assert tool.name == "argless_with_context"

    ctx = ToolContext(context=None, tool_name="argless_with_context", tool_call_id="1")

    result = await tool.on_invoke_tool(ctx, "")
    assert result == "ok"

    result = await tool.on_invoke_tool(ctx, '{"a": 1, "b": 2}')
    assert result == "ok"


@pytest.mark.asyncio
async def test_function_tool_with_context_and_args_parsed():
    class DummyCtx:
        def __init__(self):
            self.data = "xyz"

    def with_ctx_and_name(ctx: ToolContext[DummyCtx], name: str) -> str:
        return f"{name}_{ctx.context.data}"

    tool = function_tool(with_ctx_and_name)
    ctx = ToolContext(context=DummyCtx(), tool_name="with_ctx_and_name", tool_call_id="1")
    payload = json.dumps({"name": "uzair"})
    result = await tool.on_invoke_tool(ctx, payload)

    assert result == "uzair_xyz"

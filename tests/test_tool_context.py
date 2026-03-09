from typing import Annotated, Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents import Agent
from agents.run_config import RunConfig
from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool, invoke_function_tool
from agents.tool_context import ToolContext
from agents.usage import Usage
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


def test_tool_context_constructor_infers_namespace_from_tool_call() -> None:
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="lookup_account",
        call_id="call-2",
        arguments="{}",
        namespace="billing",
    )

    tool_ctx: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="lookup_account",
        tool_call_id="call-2",
        tool_arguments="{}",
        tool_call=tool_call,
    )

    assert tool_ctx.tool_namespace == "billing"
    assert tool_ctx.qualified_tool_name == "billing.lookup_account"


def test_tool_context_qualified_tool_name_collapses_synthetic_namespace() -> None:
    tool_call = ResponseFunctionToolCall(
        type="function_call",
        name="get_weather",
        call_id="call-weather",
        arguments="{}",
        namespace="get_weather",
    )

    tool_ctx: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="get_weather",
        tool_call_id="call-weather",
        tool_arguments="{}",
        tool_call=tool_call,
    )

    assert tool_ctx.tool_namespace == "get_weather"
    assert tool_ctx.qualified_tool_name == "get_weather"


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


@pytest.mark.asyncio
async def test_invoke_function_tool_passes_plain_run_context_when_requested() -> None:
    captured_context: RunContextWrapper[str] | None = None

    async def on_invoke_tool(ctx: RunContextWrapper[str], _input: str) -> str:
        nonlocal captured_context
        captured_context = ctx
        return ctx.context

    function_tool = FunctionTool(
        name="plain_context_tool",
        description="test",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=on_invoke_tool,
    )
    tool_context = ToolContext(
        context="Stormy",
        usage=Usage(),
        tool_name="plain_context_tool",
        tool_call_id="call-1",
        tool_arguments="{}",
        agent=Agent(name="agent"),
        run_config=RunConfig(model="gpt-4.1-mini"),
        tool_input={"city": "Tokyo"},
    )

    result = await invoke_function_tool(
        function_tool=function_tool,
        context=tool_context,
        arguments="{}",
    )

    assert result == "Stormy"
    assert captured_context is not None
    assert not isinstance(captured_context, ToolContext)
    assert captured_context.context == "Stormy"
    assert captured_context.usage is tool_context.usage
    assert captured_context.tool_input == {"city": "Tokyo"}


@pytest.mark.asyncio
async def test_invoke_function_tool_preserves_tool_context_when_requested() -> None:
    captured_context: ToolContext[str] | None = None

    async def on_invoke_tool(ctx: ToolContext[str], _input: str) -> str:
        nonlocal captured_context
        captured_context = ctx
        return ctx.tool_name

    function_tool = FunctionTool(
        name="tool_context_tool",
        description="test",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=on_invoke_tool,
    )
    tool_context = ToolContext(
        context="Stormy",
        usage=Usage(),
        tool_name="tool_context_tool",
        tool_call_id="call-2",
        tool_arguments="{}",
        agent=Agent(name="agent"),
        run_config=RunConfig(model="gpt-4.1-mini"),
    )

    result = await invoke_function_tool(
        function_tool=function_tool,
        context=tool_context,
        arguments="{}",
    )

    assert result == "tool_context_tool"
    assert captured_context is tool_context


@pytest.mark.asyncio
async def test_invoke_function_tool_ignores_context_name_substrings_in_string_annotations() -> None:
    captured_context: object | None = None

    class MyRunContextWrapper:
        pass

    async def on_invoke_tool(ctx: "MyRunContextWrapper", _input: str) -> str:
        nonlocal captured_context
        captured_context = ctx
        return "ok"

    function_tool = FunctionTool(
        name="substring_context_tool",
        description="test",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=cast(Any, on_invoke_tool),
    )
    tool_context = ToolContext(
        context="Stormy",
        usage=Usage(),
        tool_name="substring_context_tool",
        tool_call_id="call-3",
        tool_arguments="{}",
    )

    result = await invoke_function_tool(
        function_tool=function_tool,
        context=tool_context,
        arguments="{}",
    )

    assert result == "ok"
    assert captured_context is tool_context


@pytest.mark.asyncio
async def test_invoke_function_tool_ignores_annotated_string_metadata_when_matching_context() -> (
    None
):
    captured_context: ToolContext[str] | RunContextWrapper[str] | None = None

    async def on_invoke_tool(
        ctx: Annotated[RunContextWrapper[str], "ToolContext note"], _input: str
    ) -> str:
        nonlocal captured_context
        captured_context = ctx
        return ctx.context

    function_tool = FunctionTool(
        name="annotated_string_context_tool",
        description="test",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=on_invoke_tool,
    )
    tool_context = ToolContext(
        context="Stormy",
        usage=Usage(),
        tool_name="annotated_string_context_tool",
        tool_call_id="call-4",
        tool_arguments="{}",
        tool_input={"city": "Tokyo"},
    )

    result = await invoke_function_tool(
        function_tool=function_tool,
        context=tool_context,
        arguments="{}",
    )

    assert result == "Stormy"
    assert captured_context is not None
    assert not isinstance(captured_context, ToolContext)
    assert captured_context.tool_input == {"city": "Tokyo"}

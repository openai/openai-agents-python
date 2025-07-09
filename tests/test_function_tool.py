import json
from typing import Any

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

from agents import (
    Agent,
    AgentBase,
    FunctionTool,
    ModelBehaviorError,
    RunContextWrapper,
    function_tool,
)
from agents.tool import default_tool_error_function
from agents.tool_context import ToolContext


def argless_function() -> str:
    return "ok"


@pytest.mark.asyncio
async def test_argless_function():
    tool = function_tool(argless_function)
    assert tool.name == "argless_function"

    result = await tool.on_invoke_tool(ToolContext(context=None, tool_call_id="1"), "")
    assert result == "ok"


def argless_with_context(ctx: ToolContext[str]) -> str:
    return "ok"


@pytest.mark.asyncio
async def test_argless_with_context():
    tool = function_tool(argless_with_context)
    assert tool.name == "argless_with_context"

    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), "")
    assert result == "ok"

    # Extra JSON should not raise an error
    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), '{"a": 1}')
    assert result == "ok"


def simple_function(a: int, b: int = 5):
    return a + b


@pytest.mark.asyncio
async def test_simple_function():
    tool = function_tool(simple_function, failure_error_function=None)
    assert tool.name == "simple_function"

    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), '{"a": 1}')
    assert result == 6

    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), '{"a": 1, "b": 2}')
    assert result == 3

    # Missing required argument should raise an error
    with pytest.raises(ModelBehaviorError):
        await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), "")


class Foo(BaseModel):
    a: int
    b: int = 5


class Bar(TypedDict):
    x: str
    y: int


def complex_args_function(foo: Foo, bar: Bar, baz: str = "hello"):
    return f"{foo.a + foo.b} {bar['x']}{bar['y']} {baz}"


@pytest.mark.asyncio
async def test_complex_args_function():
    tool = function_tool(complex_args_function, failure_error_function=None)
    assert tool.name == "complex_args_function"

    valid_json = json.dumps(
        {
            "foo": Foo(a=1).model_dump(),
            "bar": Bar(x="hello", y=10),
        }
    )
    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), valid_json)
    assert result == "6 hello10 hello"

    valid_json = json.dumps(
        {
            "foo": Foo(a=1, b=2).model_dump(),
            "bar": Bar(x="hello", y=10),
        }
    )
    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), valid_json)
    assert result == "3 hello10 hello"

    valid_json = json.dumps(
        {
            "foo": Foo(a=1, b=2).model_dump(),
            "bar": Bar(x="hello", y=10),
            "baz": "world",
        }
    )
    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), valid_json)
    assert result == "3 hello10 world"

    # Missing required argument should raise an error
    with pytest.raises(ModelBehaviorError):
        await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), '{"foo": {"a": 1}}')


def test_function_config_overrides():
    tool = function_tool(simple_function, name_override="custom_name")
    assert tool.name == "custom_name"

    tool = function_tool(simple_function, description_override="custom description")
    assert tool.description == "custom description"

    tool = function_tool(
        simple_function,
        name_override="custom_name",
        description_override="custom description",
    )
    assert tool.name == "custom_name"
    assert tool.description == "custom description"


def test_func_schema_is_strict():
    tool = function_tool(simple_function)
    assert tool.strict_json_schema, "Should be strict by default"
    assert (
        "additionalProperties" in tool.params_json_schema
        and not tool.params_json_schema["additionalProperties"]
    )

    tool = function_tool(complex_args_function)
    assert tool.strict_json_schema, "Should be strict by default"
    assert (
        "additionalProperties" in tool.params_json_schema
        and not tool.params_json_schema["additionalProperties"]
    )


@pytest.mark.asyncio
async def test_manual_function_tool_creation_works():
    def do_some_work(data: str) -> str:
        return f"{data}_done"

    class FunctionArgs(BaseModel):
        data: str

    async def run_function(ctx: RunContextWrapper[Any], args: str) -> str:
        parsed = FunctionArgs.model_validate_json(args)
        return do_some_work(data=parsed.data)

    tool = FunctionTool(
        name="test",
        description="Processes extracted user data",
        params_json_schema=FunctionArgs.model_json_schema(),
        on_invoke_tool=run_function,
    )

    assert tool.name == "test"
    assert tool.description == "Processes extracted user data"
    for key, value in FunctionArgs.model_json_schema().items():
        assert tool.params_json_schema[key] == value
    assert tool.strict_json_schema

    result = await tool.on_invoke_tool(ToolContext(None, tool_call_id="1"), '{"data": "hello"}')
    assert result == "hello_done"

    tool_not_strict = FunctionTool(
        name="test",
        description="Processes extracted user data",
        params_json_schema=FunctionArgs.model_json_schema(),
        on_invoke_tool=run_function,
        strict_json_schema=False,
    )

    assert not tool_not_strict.strict_json_schema
    assert "additionalProperties" not in tool_not_strict.params_json_schema

    result = await tool_not_strict.on_invoke_tool(
        ToolContext(None, tool_call_id="1"), '{"data": "hello", "bar": "baz"}'
    )
    assert result == "hello_done"


@pytest.mark.asyncio
async def test_function_tool_default_error_works():
    def my_func(a: int, b: int = 5):
        raise ValueError("test")

    tool = function_tool(my_func)
    ctx = ToolContext(None, tool_call_id="1")

    result = await tool.on_invoke_tool(ctx, "")
    assert "Invalid JSON" in str(result)

    result = await tool.on_invoke_tool(ctx, "{}")
    assert "Invalid JSON" in str(result)

    result = await tool.on_invoke_tool(ctx, '{"a": 1}')
    assert result == default_tool_error_function(ctx, ValueError("test"))

    result = await tool.on_invoke_tool(ctx, '{"a": 1, "b": 2}')
    assert result == default_tool_error_function(ctx, ValueError("test"))


@pytest.mark.asyncio
async def test_sync_custom_error_function_works():
    def my_func(a: int, b: int = 5):
        raise ValueError("test")

    def custom_sync_error_function(ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"error_{error.__class__.__name__}"

    tool = function_tool(my_func, failure_error_function=custom_sync_error_function)
    ctx = ToolContext(None, tool_call_id="1")

    result = await tool.on_invoke_tool(ctx, "")
    assert result == "error_ModelBehaviorError"

    result = await tool.on_invoke_tool(ctx, "{}")
    assert result == "error_ModelBehaviorError"

    result = await tool.on_invoke_tool(ctx, '{"a": 1}')
    assert result == "error_ValueError"

    result = await tool.on_invoke_tool(ctx, '{"a": 1, "b": 2}')
    assert result == "error_ValueError"


@pytest.mark.asyncio
async def test_async_custom_error_function_works():
    async def my_func(a: int, b: int = 5):
        raise ValueError("test")

    def custom_sync_error_function(ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"error_{error.__class__.__name__}"

    tool = function_tool(my_func, failure_error_function=custom_sync_error_function)
    ctx = ToolContext(None, tool_call_id="1")

    result = await tool.on_invoke_tool(ctx, "")
    assert result == "error_ModelBehaviorError"

    result = await tool.on_invoke_tool(ctx, "{}")
    assert result == "error_ModelBehaviorError"

    result = await tool.on_invoke_tool(ctx, '{"a": 1}')
    assert result == "error_ValueError"

    result = await tool.on_invoke_tool(ctx, '{"a": 1, "b": 2}')
    assert result == "error_ValueError"


class BoolCtx(BaseModel):
    enable_tools: bool


@pytest.mark.asyncio
async def test_is_enabled_bool_and_callable():
    @function_tool(is_enabled=False)
    def disabled_tool():
        return "nope"

    async def cond_enabled(ctx: RunContextWrapper[BoolCtx], agent: AgentBase) -> bool:
        return ctx.context.enable_tools

    @function_tool(is_enabled=cond_enabled)
    def another_tool():
        return "hi"

    async def third_tool_on_invoke_tool(ctx: RunContextWrapper[Any], args: str) -> str:
        return "third"

    third_tool = FunctionTool(
        name="third_tool",
        description="third tool",
        on_invoke_tool=third_tool_on_invoke_tool,
        is_enabled=lambda ctx, agent: ctx.context.enable_tools,
        params_json_schema={},
    )

    agent = Agent(name="t", tools=[disabled_tool, another_tool, third_tool])
    context_1 = RunContextWrapper(BoolCtx(enable_tools=False))
    context_2 = RunContextWrapper(BoolCtx(enable_tools=True))

    tools_with_ctx = await agent.get_all_tools(context_1)
    assert tools_with_ctx == []

    tools_with_ctx = await agent.get_all_tools(context_2)
    assert len(tools_with_ctx) == 2
    assert tools_with_ctx[0].name == "another_tool"
    assert tools_with_ctx[1].name == "third_tool"

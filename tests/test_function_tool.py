import asyncio
import contextlib
import json
import time
from typing import Any, Callable

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

import agents.tool as tool_module
from agents import (
    Agent,
    AgentBase,
    FunctionTool,
    ModelBehaviorError,
    RunContextWrapper,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    function_tool,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.tool import default_tool_error_function
from agents.tool_context import ToolContext


def argless_function() -> str:
    return "ok"


@pytest.mark.asyncio
async def test_argless_function():
    tool = function_tool(argless_function)
    assert tool.name == "argless_function"

    result = await tool.on_invoke_tool(
        ToolContext(context=None, tool_name=tool.name, tool_call_id="1", tool_arguments=""), ""
    )
    assert result == "ok"


def argless_with_context(ctx: ToolContext[str]) -> str:
    return "ok"


@pytest.mark.asyncio
async def test_argless_with_context():
    tool = function_tool(argless_with_context)
    assert tool.name == "argless_with_context"

    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=""), ""
    )
    assert result == "ok"

    # Extra JSON should not raise an error
    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments='{"a": 1}'),
        '{"a": 1}',
    )
    assert result == "ok"


def simple_function(a: int, b: int = 5):
    return a + b


@pytest.mark.asyncio
async def test_simple_function():
    tool = function_tool(simple_function, failure_error_function=None)
    assert tool.name == "simple_function"

    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments='{"a": 1}'),
        '{"a": 1}',
    )
    assert result == 6

    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments='{"a": 1, "b": 2}'),
        '{"a": 1, "b": 2}',
    )
    assert result == 3

    # Missing required argument should raise an error
    with pytest.raises(ModelBehaviorError):
        await tool.on_invoke_tool(
            ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=""), ""
        )


@pytest.mark.asyncio
async def test_sync_function_runs_via_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"to_thread": 0, "func": 0}

    def sync_func() -> str:
        calls["func"] += 1
        return "ok"

    async def fake_to_thread(
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        calls["to_thread"] += 1
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    tool = function_tool(sync_func)
    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=""), ""
    )
    assert result == "ok"
    assert calls["to_thread"] == 1
    assert calls["func"] == 1


@pytest.mark.asyncio
async def test_sync_function_does_not_block_event_loop() -> None:
    def sync_func() -> str:
        time.sleep(0.2)
        return "ok"

    tool = function_tool(sync_func)

    async def run_tool() -> Any:
        return await tool.on_invoke_tool(
            ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=""), ""
        )

    tool_task: asyncio.Task[Any] = asyncio.create_task(run_tool())
    background_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0.01))

    done, pending = await asyncio.wait(
        {tool_task, background_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    try:
        assert background_task in done
        assert tool_task in pending
        assert await tool_task == "ok"
    finally:
        if not background_task.done():
            background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_task
        if not tool_task.done():
            tool_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tool_task


class Foo(BaseModel):
    a: int
    b: int = 5


class Bar(TypedDict):
    x: str
    y: int


def complex_args_function(foo: Foo, bar: Bar, baz: str = "hello"):
    return f"{foo.a + foo.b} {bar['x']}{bar['y']} {baz}"


@tool_input_guardrail
def reject_args_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Reject tool calls for test purposes."""
    return ToolGuardrailFunctionOutput.reject_content(
        message="blocked",
        output_info={"tool": data.context.tool_name},
    )


@tool_output_guardrail
def allow_output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Allow tool outputs for test purposes."""
    return ToolGuardrailFunctionOutput.allow(output_info={"echo": data.output})


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
    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=valid_json),
        valid_json,
    )
    assert result == "6 hello10 hello"

    valid_json = json.dumps(
        {
            "foo": Foo(a=1, b=2).model_dump(),
            "bar": Bar(x="hello", y=10),
        }
    )
    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=valid_json),
        valid_json,
    )
    assert result == "3 hello10 hello"

    valid_json = json.dumps(
        {
            "foo": Foo(a=1, b=2).model_dump(),
            "bar": Bar(x="hello", y=10),
            "baz": "world",
        }
    )
    result = await tool.on_invoke_tool(
        ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments=valid_json),
        valid_json,
    )
    assert result == "3 hello10 world"

    # Missing required argument should raise an error
    with pytest.raises(ModelBehaviorError):
        await tool.on_invoke_tool(
            ToolContext(
                None, tool_name=tool.name, tool_call_id="1", tool_arguments='{"foo": {"a": 1}}'
            ),
            '{"foo": {"a": 1}}',
        )


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

    result = await tool.on_invoke_tool(
        ToolContext(
            None, tool_name=tool.name, tool_call_id="1", tool_arguments='{"data": "hello"}'
        ),
        '{"data": "hello"}',
    )
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
        ToolContext(
            None,
            tool_name=tool_not_strict.name,
            tool_call_id="1",
            tool_arguments='{"data": "hello", "bar": "baz"}',
        ),
        '{"data": "hello", "bar": "baz"}',
    )
    assert result == "hello_done"


@pytest.mark.asyncio
async def test_function_tool_default_error_works():
    def my_func(a: int, b: int = 5):
        raise ValueError("test")

    tool = function_tool(my_func)
    ctx = ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments="")

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
    ctx = ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments="")

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
    ctx = ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments="")

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


@pytest.mark.asyncio
async def test_async_failure_error_function_is_awaited() -> None:
    async def failure_handler(ctx: RunContextWrapper[Any], exc: Exception) -> str:
        return f"handled:{exc}"

    @function_tool(failure_error_function=lambda ctx, exc: failure_handler(ctx, exc))
    def boom() -> None:
        """Always raises to trigger the failure handler."""
        raise RuntimeError("kapow")

    ctx = ToolContext(None, tool_name=boom.name, tool_call_id="boom", tool_arguments="{}")
    result = await boom.on_invoke_tool(ctx, "{}")
    assert result.startswith("handled:")


@pytest.mark.asyncio
async def test_default_failure_error_function_is_resolved_at_invoke_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(a: int) -> None:
        raise ValueError(f"boom:{a}")

    tool = function_tool(boom)

    def patched_default(_ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"patched:{error}"

    monkeypatch.setattr(tool_module, "default_tool_error_function", patched_default)

    ctx = ToolContext(None, tool_name=tool.name, tool_call_id="1", tool_arguments='{"a": 7}')
    result = await tool.on_invoke_tool(ctx, '{"a": 7}')
    assert result == "patched:boom:7"


def test_function_tool_accepts_guardrail_arguments():
    tool = function_tool(
        simple_function,
        tool_input_guardrails=[reject_args_guardrail],
        tool_output_guardrails=[allow_output_guardrail],
    )

    assert tool.tool_input_guardrails == [reject_args_guardrail]
    assert tool.tool_output_guardrails == [allow_output_guardrail]


def test_function_tool_decorator_accepts_guardrail_arguments():
    @function_tool(
        tool_input_guardrails=[reject_args_guardrail],
        tool_output_guardrails=[allow_output_guardrail],
    )
    def guarded(a: int) -> int:
        return a

    assert guarded.tool_input_guardrails == [reject_args_guardrail]
    assert guarded.tool_output_guardrails == [allow_output_guardrail]

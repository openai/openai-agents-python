import asyncio
import contextlib
import copy
import dataclasses
import json
import time
from collections.abc import Callable
from typing import Any, cast

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

import agents.tool as tool_module
from agents import (
    Agent,
    AgentBase,
    FunctionTool,
    HostedMCPTool,
    ModelBehaviorError,
    RunContextWrapper,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    ToolSearchTool,
    ToolTimeoutError,
    UserError,
    function_tool,
    tool_input_guardrail,
    tool_namespace,
    tool_output_guardrail,
)
from agents.tool import default_tool_error_function
from agents.tool_context import ToolContext


def argless_function() -> str:
    return "ok"


def test_tool_namespace_copies_tools_with_metadata() -> None:
    tool = function_tool(argless_function)

    namespaced_tools = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[tool],
    )

    assert len(namespaced_tools) == 1
    assert namespaced_tools[0] is not tool
    assert namespaced_tools[0]._tool_namespace == "crm"
    assert namespaced_tools[0]._tool_namespace_description == "CRM tools"
    assert namespaced_tools[0].qualified_name == "crm.argless_function"
    assert tool._tool_namespace is None
    assert tool.qualified_name == "argless_function"


def test_tool_namespace_requires_keyword_arguments() -> None:
    tool = function_tool(argless_function)

    with pytest.raises(TypeError):
        tool_namespace("crm", "CRM tools", [tool])  # type: ignore[misc]


def test_tool_namespace_requires_non_empty_description() -> None:
    tool = function_tool(argless_function)

    with pytest.raises(UserError, match="non-empty description"):
        tool_namespace(
            name="crm",
            description=None,
            tools=[tool],
        )

    with pytest.raises(UserError, match="non-empty description"):
        tool_namespace(
            name="crm",
            description="   ",
            tools=[tool],
        )


def test_tool_namespace_rejects_reserved_same_name_shape() -> None:
    tool = function_tool(argless_function, name_override="lookup_account")

    with pytest.raises(UserError, match="synthetic namespace `lookup_account.lookup_account`"):
        tool_namespace(
            name="lookup_account",
            description="Same-name namespace",
            tools=[tool],
        )


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
async def test_get_all_tools_preserves_explicit_tool_search_when_deferred_tools_are_disabled():
    async def deferred_enabled(ctx: RunContextWrapper[BoolCtx], agent: AgentBase) -> bool:
        return ctx.context.enable_tools

    @function_tool(defer_loading=True, is_enabled=deferred_enabled)
    def deferred_lookup() -> str:
        return "loaded"

    agent = Agent(name="t", tools=[deferred_lookup, ToolSearchTool()])

    tools_with_disabled_context = await agent.get_all_tools(
        RunContextWrapper(BoolCtx(enable_tools=False))
    )
    assert len(tools_with_disabled_context) == 1
    assert isinstance(tools_with_disabled_context[0], ToolSearchTool)

    tools_with_enabled_context = await agent.get_all_tools(
        RunContextWrapper(BoolCtx(enable_tools=True))
    )
    assert tools_with_enabled_context[0] is deferred_lookup
    assert isinstance(tools_with_enabled_context[1], ToolSearchTool)


@pytest.mark.asyncio
async def test_get_all_tools_keeps_tool_search_for_namespace_only_tools():
    namespaced_lookup = tool_namespace(
        name="crm",
        description="CRM tools",
        tools=[function_tool(lambda account_id: account_id, name_override="lookup_account")],
    )[0]

    agent = Agent(name="t", tools=[namespaced_lookup, ToolSearchTool()])

    tools = await agent.get_all_tools(RunContextWrapper(BoolCtx(enable_tools=False)))

    assert tools[0] is namespaced_lookup
    assert isinstance(tools[1], ToolSearchTool)


@pytest.mark.asyncio
async def test_get_all_tools_keeps_tool_search_for_deferred_hosted_mcp() -> None:
    hosted_mcp = HostedMCPTool(
        tool_config=cast(
            Any,
            {
                "type": "mcp",
                "server_label": "crm_server",
                "server_url": "https://example.com/mcp",
                "defer_loading": True,
            },
        )
    )
    agent = Agent(name="t", tools=[hosted_mcp, ToolSearchTool()])

    tools = await agent.get_all_tools(RunContextWrapper(BoolCtx(enable_tools=False)))

    assert tools[0] is hosted_mcp
    assert isinstance(tools[1], ToolSearchTool)


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
async def test_failure_error_function_normalizes_cancelled_error_to_exception() -> None:
    seen_error: Exception | None = None

    def failure_handler(_ctx: RunContextWrapper[Any], error: Exception) -> str:
        nonlocal seen_error
        assert isinstance(error, Exception)
        assert not isinstance(error, asyncio.CancelledError)
        seen_error = error
        return f"handled:{error}"

    tool = function_tool(lambda: "ok", failure_error_function=failure_handler)

    result = await tool_module.maybe_invoke_function_tool_failure_error_function(
        function_tool=tool,
        context=RunContextWrapper(None),
        error=asyncio.CancelledError(),
    )

    assert result == "handled:Tool execution cancelled."
    assert seen_error is not None
    assert str(seen_error) == "Tool execution cancelled."


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


@pytest.mark.asyncio
async def test_manual_function_tool_uses_default_failure_error_function() -> None:
    async def on_invoke_tool(_ctx: ToolContext[Any], _args: str) -> str:
        raise asyncio.CancelledError("manual-tool-cancelled")

    manual_tool = FunctionTool(
        name="manual_cancel_tool",
        description="manual cancel",
        params_json_schema={},
        on_invoke_tool=on_invoke_tool,
    )

    result = await tool_module.maybe_invoke_function_tool_failure_error_function(
        function_tool=manual_tool,
        context=RunContextWrapper(None),
        error=asyncio.CancelledError("manual-tool-cancelled"),
    )

    expected = (
        "An error occurred while running the tool. Please try again. Error: manual-tool-cancelled"
    )
    assert result == expected
    assert (
        tool_module.resolve_function_tool_failure_error_function(manual_tool)
        is default_tool_error_function
    )


@pytest.mark.asyncio
async def test_failure_error_function_survives_dataclasses_replace() -> None:
    def failure_handler(_ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"handled:{error}"

    tool = function_tool(lambda: "ok", failure_error_function=failure_handler)
    copied_tool = dataclasses.replace(tool, name="copied_tool")

    result = await tool_module.maybe_invoke_function_tool_failure_error_function(
        function_tool=copied_tool,
        context=RunContextWrapper(None),
        error=asyncio.CancelledError(),
    )

    assert result == "handled:Tool execution cancelled."
    assert tool_module.resolve_function_tool_failure_error_function(copied_tool) is failure_handler


@pytest.mark.asyncio
async def test_replaced_function_tool_normal_failure_uses_replaced_policy() -> None:
    def boom() -> None:
        raise RuntimeError("kapow")

    replaced_tool = dataclasses.replace(
        function_tool(boom),
        name="replaced_tool",
        _failure_error_function=None,
        _use_default_failure_error_function=False,
    )

    with pytest.raises(RuntimeError, match="kapow"):
        await replaced_tool.on_invoke_tool(
            ToolContext(None, tool_name=replaced_tool.name, tool_call_id="1", tool_arguments=""),
            "",
        )


@pytest.mark.asyncio
async def test_shallow_copied_function_tool_normal_failure_uses_copied_policy() -> None:
    def boom() -> None:
        raise RuntimeError("kapow")

    original_tool = function_tool(boom)
    custom_state = {"cache": ["alpha"]}
    cast(Any, original_tool).custom_state = custom_state

    copied_tool = copy.copy(original_tool)
    copied_tool.name = "copied_tool"
    copied_tool._failure_error_function = None
    copied_tool._use_default_failure_error_function = False

    with pytest.raises(RuntimeError, match="kapow"):
        await copied_tool.on_invoke_tool(
            ToolContext(None, tool_name=copied_tool.name, tool_call_id="1", tool_arguments=""),
            "",
        )

    assert cast(Any, copied_tool).custom_state is custom_state


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_style", ["replace", "shallow_copy"])
async def test_copied_function_tool_invalid_input_uses_current_name(copy_style: str) -> None:
    def echo(value: str) -> str:
        return value

    original_tool = function_tool(
        echo,
        name_override="original_tool",
        failure_error_function=None,
    )
    if copy_style == "replace":
        copied_tool = dataclasses.replace(original_tool, name="copied_tool")
    else:
        copied_tool = copy.copy(original_tool)
        copied_tool.name = "copied_tool"

    with pytest.raises(ModelBehaviorError, match="Invalid JSON input for tool copied_tool"):
        await copied_tool.on_invoke_tool(
            ToolContext(
                None,
                tool_name=copied_tool.name,
                tool_call_id="1",
                tool_arguments="{}",
            ),
            "{}",
        )


def test_function_tool_does_not_mutate_params_json_schema() -> None:
    async def noop(ctx: ToolContext[Any], input: str) -> str:
        return ""

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    schema_snapshot = copy.deepcopy(schema)

    tool = FunctionTool(
        name="t",
        description="d",
        params_json_schema=schema,
        on_invoke_tool=noop,
        strict_json_schema=True,
    )

    assert schema == schema_snapshot
    assert tool.params_json_schema is not schema
    assert tool.params_json_schema["additionalProperties"] is False
    assert tool.params_json_schema["required"] == ["x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("input_json", ["[]", '"value"', "123", "null", "true"])
async def test_function_tool_rejects_non_object_json_input(input_json: str) -> None:
    def echo(value: str) -> str:
        return value

    tool = function_tool(
        echo,
        name_override="echo_tool",
        failure_error_function=None,
    )

    with pytest.raises(
        ModelBehaviorError,
        match="Invalid JSON input for tool echo_tool: expected a JSON object",
    ):
        await tool.on_invoke_tool(
            ToolContext(
                None,
                tool_name="echo_tool",
                tool_call_id="1",
                tool_arguments=input_json,
            ),
            input_json,
        )


@pytest.mark.asyncio
async def test_default_failure_error_function_survives_deepcopy() -> None:
    def boom() -> None:
        raise RuntimeError("kapow")

    tool = function_tool(boom)
    copied_tool = copy.deepcopy(tool)

    result = await tool_module.maybe_invoke_function_tool_failure_error_function(
        function_tool=copied_tool,
        context=RunContextWrapper(None),
        error=asyncio.CancelledError(),
    )

    expected = (
        "An error occurred while running the tool. Please try again. "
        "Error: Tool execution cancelled."
    )
    assert result == expected
    assert (
        tool_module.resolve_function_tool_failure_error_function(copied_tool)
        is default_tool_error_function
    )


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


@pytest.mark.asyncio
async def test_invoke_function_tool_timeout_returns_default_message() -> None:
    @function_tool(timeout=0.01)
    async def slow_tool() -> str:
        await asyncio.sleep(0.2)
        return "slow"

    ctx = ToolContext(None, tool_name=slow_tool.name, tool_call_id="slow", tool_arguments="{}")
    result = await tool_module.invoke_function_tool(
        function_tool=slow_tool,
        context=ctx,
        arguments="{}",
    )

    assert isinstance(result, str)
    assert "timed out" in result.lower()
    assert "0.01" in result


@pytest.mark.asyncio
async def test_invoke_function_tool_timeout_uses_custom_error_function() -> None:
    def custom_timeout_error(_ctx: RunContextWrapper[Any], error: Exception) -> str:
        assert isinstance(error, ToolTimeoutError)
        return f"custom_timeout:{error.tool_name}:{error.timeout_seconds:g}"

    @function_tool(timeout=0.01, timeout_error_function=custom_timeout_error)
    async def slow_tool() -> str:
        await asyncio.sleep(0.2)
        return "slow"

    ctx = ToolContext(None, tool_name=slow_tool.name, tool_call_id="slow", tool_arguments="{}")
    result = await tool_module.invoke_function_tool(
        function_tool=slow_tool,
        context=ctx,
        arguments="{}",
    )

    assert result == "custom_timeout:slow_tool:0.01"


@pytest.mark.asyncio
async def test_invoke_function_tool_timeout_can_raise_exception() -> None:
    @function_tool(timeout=0.01, timeout_behavior="raise_exception")
    async def slow_tool() -> str:
        await asyncio.sleep(0.2)
        return "slow"

    ctx = ToolContext(None, tool_name=slow_tool.name, tool_call_id="slow", tool_arguments="{}")
    with pytest.raises(ToolTimeoutError, match="timed out"):
        await tool_module.invoke_function_tool(
            function_tool=slow_tool,
            context=ctx,
            arguments="{}",
        )


@pytest.mark.asyncio
async def test_invoke_function_tool_does_not_rewrite_tool_raised_timeout_error() -> None:
    @function_tool(timeout=1.0, failure_error_function=None)
    async def timeout_tool() -> str:
        raise TimeoutError("tool_internal_timeout")

    ctx = ToolContext(
        None, tool_name=timeout_tool.name, tool_call_id="timeout", tool_arguments="{}"
    )
    with pytest.raises(TimeoutError, match="tool_internal_timeout"):
        await tool_module.invoke_function_tool(
            function_tool=timeout_tool,
            context=ctx,
            arguments="{}",
        )


@pytest.mark.asyncio
async def test_invoke_function_tool_does_not_rewrite_manual_tool_raised_timeout_error() -> None:
    async def on_invoke_tool(_ctx: ToolContext[Any], _args: str) -> str:
        raise TimeoutError("manual_tool_internal_timeout")

    manual_tool = FunctionTool(
        name="manual_timeout_tool",
        description="manual timeout",
        params_json_schema={},
        on_invoke_tool=on_invoke_tool,
        timeout_seconds=1.0,
    )

    ctx = ToolContext(None, tool_name=manual_tool.name, tool_call_id="timeout", tool_arguments="{}")
    with pytest.raises(TimeoutError, match="manual_tool_internal_timeout"):
        await tool_module.invoke_function_tool(
            function_tool=manual_tool,
            context=ctx,
            arguments="{}",
        )


async def _noop_on_invoke_tool(_ctx: ToolContext[Any], _args: str) -> str:
    return "ok"


def test_function_tool_timeout_seconds_must_be_positive_number() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        FunctionTool(
            name="bad_timeout",
            description="bad",
            params_json_schema={},
            on_invoke_tool=_noop_on_invoke_tool,
            timeout_seconds=0.0,
        )

    with pytest.raises(TypeError, match="positive number"):
        FunctionTool(
            name="bad_timeout_type",
            description="bad",
            params_json_schema={},
            on_invoke_tool=_noop_on_invoke_tool,
            timeout_seconds=cast(Any, "1"),
        )

    with pytest.raises(ValueError, match="finite number"):
        FunctionTool(
            name="bad_timeout_inf",
            description="bad",
            params_json_schema={},
            on_invoke_tool=_noop_on_invoke_tool,
            timeout_seconds=float("inf"),
        )

    with pytest.raises(ValueError, match="finite number"):
        FunctionTool(
            name="bad_timeout_nan",
            description="bad",
            params_json_schema={},
            on_invoke_tool=_noop_on_invoke_tool,
            timeout_seconds=float("nan"),
        )


def test_function_tool_timeout_not_supported_for_sync_handlers() -> None:
    def sync_tool() -> str:
        return "ok"

    with pytest.raises(ValueError, match="only supported for async @function_tool handlers"):
        function_tool(sync_tool, timeout=1.0)

    with pytest.raises(ValueError, match="only supported for async @function_tool handlers"):

        @function_tool(timeout=1.0)
        def sync_tool_decorator_style() -> str:
            return "ok"


def test_function_tool_timeout_behavior_must_be_supported() -> None:
    with pytest.raises(ValueError, match="timeout_behavior must be one of"):
        FunctionTool(
            name="bad_timeout_behavior",
            description="bad",
            params_json_schema={},
            on_invoke_tool=_noop_on_invoke_tool,
            timeout_behavior=cast(Any, "unsupported"),
        )


def test_function_tool_timeout_error_function_must_be_callable() -> None:
    with pytest.raises(TypeError, match="timeout_error_function must be callable"):
        FunctionTool(
            name="bad_timeout_error_function",
            description="bad",
            params_json_schema={},
            on_invoke_tool=_noop_on_invoke_tool,
            timeout_error_function=cast(Any, "not-callable"),
        )

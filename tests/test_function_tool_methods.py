"""@function_tool support on instance methods (#94)."""

from __future__ import annotations

import json

from agents import Agent, FunctionTool, RunContextWrapper, Runner, function_tool
from agents.tool_context import ToolContext
from tests.fake_model import FakeModel
from tests.test_responses import get_function_tool_call, get_text_message


class Calculator:
    def __init__(self, base: int) -> None:
        self.base = base

    @function_tool
    def add_to_base(self, x: int) -> int:
        """Add x to the calculator's base."""
        return self.base + x

    @function_tool
    def add_with_context(self, ctx: RunContextWrapper[int], x: int) -> int:
        """Add x to the base and the run context value."""
        return self.base + x + ctx.context


def _ctx(tool: FunctionTool) -> ToolContext:
    return ToolContext(context=None, tool_name=tool.name, tool_call_id="1", tool_arguments="")


def test_instance_access_binds_self_and_drops_it_from_schema() -> None:
    calc = Calculator(10)
    tool = calc.add_to_base  # descriptor __get__ -> instance-bound tool

    assert isinstance(tool, FunctionTool)
    properties = tool.params_json_schema.get("properties", {})
    assert "self" not in properties
    assert "x" in properties


async def test_instance_method_tool_invokes_with_self() -> None:
    calc = Calculator(10)
    tool = calc.add_to_base
    result = await tool.on_invoke_tool(_ctx(tool), json.dumps({"x": 5}))
    assert result == 15


async def test_distinct_instances_bind_independently() -> None:
    ten, twenty = Calculator(10), Calculator(20)
    assert await ten.add_to_base.on_invoke_tool(_ctx(ten.add_to_base), json.dumps({"x": 1})) == 11
    assert (
        await twenty.add_to_base.on_invoke_tool(_ctx(twenty.add_to_base), json.dumps({"x": 1}))
        == 21
    )


async def test_context_taking_method_binds_self_and_context() -> None:
    # A method that takes RunContextWrapper after self must not raise at decoration
    # and must receive both self and the run context when invoked.
    calc = Calculator(10)
    tool = calc.add_with_context
    assert "self" not in tool.params_json_schema.get("properties", {})
    assert "ctx" not in tool.params_json_schema.get("properties", {})
    assert "x" in tool.params_json_schema.get("properties", {})

    ctx: ToolContext[int] = ToolContext(
        context=5, tool_name=tool.name, tool_call_id="1", tool_arguments=""
    )
    result = await tool.on_invoke_tool(ctx, json.dumps({"x": 2}))
    assert result == 17  # base 10 + x 2 + context 5


def test_module_level_self_named_function_is_not_treated_as_method() -> None:
    # A plain function whose first arg happens to be named `self` is unaffected:
    # `self` stays in the schema and is supplied by the model.
    @function_tool
    def weird(self: int, x: int) -> int:
        """A free function with an unfortunate first argument name."""
        return self + x

    assert "self" in weird.params_json_schema.get("properties", {})


def test_class_access_returns_unbound_tool() -> None:
    # Accessing via the class (no instance) returns the original tool unchanged.
    assert isinstance(Calculator.add_to_base, FunctionTool)


def test_module_level_function_tool_unaffected() -> None:
    @function_tool
    def free(x: int) -> int:
        """A free function."""
        return x

    assert isinstance(free, FunctionTool)
    assert "x" in free.params_json_schema.get("properties", {})


async def test_instance_method_tool_runs_in_agent() -> None:
    calc = Calculator(100)
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("add_to_base", json.dumps({"x": 5}))],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="A", instructions="x", model=model, tools=[calc.add_to_base])
    result = await Runner.run(agent, "add 5")
    assert result.final_output == "done"

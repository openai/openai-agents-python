"""Tests for @function_tool applied to class instance methods (issue #94).

Covers the descriptor protocol (__get__), schema generation (self/cls stripped),
receiver binding at call time, and edge cases flagged in previous review rounds.
"""

from __future__ import annotations

import pytest

from agents import RunContextWrapper
from agents.exceptions import UserError
from agents.function_schema import function_schema
from agents.tool import FunctionTool, function_tool
from agents.tool_context import ToolContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_context(tool_name: str = "test_tool") -> ToolContext:
    return ToolContext(context=None, tool_name=tool_name, tool_call_id="1", tool_arguments="")


# ---------------------------------------------------------------------------
# function_schema unit tests
# ---------------------------------------------------------------------------


class _BasicMethodClass:
    multiplier: int

    def __init__(self, multiplier: int) -> None:
        self.multiplier = multiplier

    def multiply(self, x: int) -> int:
        """Multiply x by the instance multiplier.

        Args:
            x: The value to multiply.
        """
        return x * self.multiplier


def test_function_schema_strips_self() -> None:
    """function_schema must not include self in the JSON schema."""
    schema = function_schema(_BasicMethodClass.multiply)
    assert schema.skips_receiver is True
    assert schema.takes_context is False
    assert "self" not in schema.params_json_schema.get("properties", {})
    assert "x" in schema.params_json_schema.get("properties", {})


def test_function_schema_strips_self_from_stored_signature() -> None:
    """The stored signature must not include self so to_call_args never fetches it."""
    schema = function_schema(_BasicMethodClass.multiply)
    assert "self" not in schema.signature.parameters


def test_function_schema_to_call_args_without_receiver() -> None:
    """to_call_args must return only the non-receiver arguments."""
    schema = function_schema(_BasicMethodClass.multiply)
    parsed = schema.params_pydantic_model(x=7)
    args, kwargs = schema.to_call_args(parsed)
    # args should be [7]; no None placeholder for self
    assert args == [7]
    assert kwargs == {}


def test_function_schema_cls_is_stripped() -> None:
    """Leading cls parameter (unannotated) must also be treated as a receiver.

    We test the undecorated classmethod function directly (before @classmethod
    binds cls) because @classmethod already hides cls from the signature.
    """

    # Define an unbound function that uses cls as its first unannotated param,
    # as if it were the raw underlying function of a classmethod.
    def greet(cls, name: str) -> str:
        """Say hi.

        Args:
            name: Who to greet.
        """
        return f"hi {name}"

    schema = function_schema(greet)
    assert schema.skips_receiver is True
    assert "cls" not in schema.params_json_schema.get("properties", {})
    assert "name" in schema.params_json_schema.get("properties", {})


def test_function_schema_annotated_self_not_stripped() -> None:
    """A first parameter named self *with* a type annotation must not be stripped."""

    def weird(self: int, y: int) -> int:
        return self + y

    schema = function_schema(weird)
    assert schema.skips_receiver is False
    assert "self" in schema.params_json_schema.get("properties", {})


def test_function_schema_self_with_context_param() -> None:
    """self followed immediately by RunContextWrapper must set both flags correctly."""

    class _WithCtx:
        def act(self, ctx: RunContextWrapper[None], value: int) -> int:
            """Do something.

            Args:
                value: The input value.
            """
            return value

    schema = function_schema(_WithCtx.act)
    assert schema.skips_receiver is True
    assert schema.takes_context is True
    assert "self" not in schema.params_json_schema.get("properties", {})
    assert "ctx" not in schema.params_json_schema.get("properties", {})
    assert "value" in schema.params_json_schema.get("properties", {})


def test_function_schema_context_in_wrong_position_raises() -> None:
    """RunContextWrapper after self but not in position 1 must raise UserError."""

    class _Bad:
        def bad(self, x: int, ctx: RunContextWrapper[None]) -> int:
            return x

    with pytest.raises(UserError, match="non-first position"):
        function_schema(_Bad.bad)


def test_function_schema_regular_function_unchanged() -> None:
    """function_schema on a plain function must behave exactly as before."""

    def add(a: int, b: int) -> int:
        """Add two numbers.

        Args:
            a: First.
            b: Second.
        """
        return a + b

    schema = function_schema(add)
    assert schema.skips_receiver is False
    assert schema.takes_context is False
    assert "a" in schema.params_json_schema.get("properties", {})
    assert "b" in schema.params_json_schema.get("properties", {})


# ---------------------------------------------------------------------------
# FunctionTool descriptor / __get__ tests
# ---------------------------------------------------------------------------


class _Calculator:
    def __init__(self, base: int) -> None:
        self.base = base

    @function_tool
    def add(self, x: int) -> int:
        """Add x to the base.

        Args:
            x: Value to add.
        """
        return self.base + x

    @function_tool
    async def async_add(self, x: int) -> int:
        """Async-add x to the base.

        Args:
            x: Value to add.
        """
        return self.base + x


def test_class_level_access_returns_function_tool() -> None:
    """Accessing the tool on the class should return a FunctionTool (unbound)."""
    assert isinstance(_Calculator.add, FunctionTool)


def test_instance_access_returns_function_tool() -> None:
    """Accessing the tool on an instance should also return a FunctionTool."""
    calc = _Calculator(base=10)
    assert isinstance(calc.add, FunctionTool)


def test_instance_access_returns_different_object() -> None:
    """Each instance access should produce a new (bound) FunctionTool."""
    calc = _Calculator(base=10)
    bound1 = calc.add
    bound2 = calc.add
    assert bound1 is not bound2
    assert bound1 is not _Calculator.add


def test_bound_tool_schema_unchanged() -> None:
    """The schema of the bound tool must be identical to the class-level tool."""
    calc = _Calculator(base=10)
    assert calc.add.params_json_schema == _Calculator.add.params_json_schema
    assert calc.add.name == _Calculator.add.name


@pytest.mark.asyncio
async def test_bound_tool_invokes_correct_instance() -> None:
    """The bound tool must call the method on the correct instance."""
    calc5 = _Calculator(base=5)
    calc20 = _Calculator(base=20)

    ctx = _make_tool_context("add")
    result5 = await calc5.add.on_invoke_tool(ctx, '{"x": 3}')
    result20 = await calc20.add.on_invoke_tool(ctx, '{"x": 3}')

    assert result5 == 8  # 5 + 3
    assert result20 == 23  # 20 + 3


@pytest.mark.asyncio
async def test_async_bound_tool_invokes_correct_instance() -> None:
    """The async variant also dispatches to the right instance."""
    calc = _Calculator(base=100)
    ctx = _make_tool_context("async_add")
    result = await calc.async_add.on_invoke_tool(ctx, '{"x": 1}')
    assert result == 101


@pytest.mark.asyncio
async def test_unbound_tool_returns_error_message() -> None:
    """Calling the class-level (unbound) tool must produce an error message.

    The UserError raised internally is caught by the failure error handler and
    returned as a string so the LLM receives a meaningful error rather than
    crashing the run.
    """
    ctx = _make_tool_context("add")
    result = await _Calculator.add.on_invoke_tool(ctx, '{"x": 1}')
    assert isinstance(result, str)
    assert "class instance" in result


# ---------------------------------------------------------------------------
# Method tool with RunContextWrapper
# ---------------------------------------------------------------------------


class _ContextAwareService:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    @function_tool
    def greet(self, ctx: RunContextWrapper[None], name: str) -> str:
        """Greet someone.

        Args:
            name: The person's name.
        """
        return f"{self.prefix}: hello {name}"


@pytest.mark.asyncio
async def test_method_tool_with_context() -> None:
    """Method tool that also takes RunContextWrapper must pass ctx correctly."""
    svc = _ContextAwareService(prefix="BOT")
    tool_ctx = _make_tool_context("greet")

    result = await svc.greet.on_invoke_tool(tool_ctx, '{"name": "Alice"}')
    assert result == "BOT: hello Alice"


# ---------------------------------------------------------------------------
# Only the leading self/cls is stripped (not all params named self)
# ---------------------------------------------------------------------------


def test_only_leading_self_is_stripped() -> None:
    """A parameter named 'self' that is NOT the first parameter must appear in the schema."""

    class _Tricky:
        def method(self, value: int, self_count: int = 0) -> int:
            """Do something.

            Args:
                value: Main value.
                self_count: Extra count (not a receiver).
            """
            return value + self_count

    schema = function_schema(_Tricky.method)
    props = schema.params_json_schema.get("properties", {})
    assert "self" not in props
    assert "value" in props
    assert "self_count" in props


# ---------------------------------------------------------------------------
# Decorator with arguments still works for methods
# ---------------------------------------------------------------------------


class _Described:
    @function_tool(name_override="my_described_tool", description_override="A described tool.")
    def compute(self, n: int) -> int:
        """Fallback docstring.

        Args:
            n: Input.
        """
        return n * 2


def test_function_tool_with_args_on_method() -> None:
    assert _Described.compute.name == "my_described_tool"
    assert _Described.compute.description == "A described tool."


@pytest.mark.asyncio
async def test_function_tool_with_args_on_method_binding() -> None:
    obj = _Described()
    ctx = _make_tool_context("my_described_tool")
    result = await obj.compute.on_invoke_tool(ctx, '{"n": 4}')
    assert result == 8

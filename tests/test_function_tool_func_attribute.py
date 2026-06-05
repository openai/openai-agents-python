"""Tests for the public `FunctionTool.func` attribute (issue #3381).

`@function_tool` historically captured the original callable only inside the
closure of `_on_invoke_tool_impl`. There was no public, stable way to reach
it, so downstream code had to walk
`tool.on_invoke_tool._invoke_tool_impl.__closure__` for the `the_func` free
variable, which silently breaks any time the internal indirection changes.

`FunctionTool.func` exposes the underlying callable as a public, stable
handle so introspection, sandboxed re-execution, direct unit testing, and
framework migration no longer need to spelunk private attributes.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from typing import Any, cast

import pytest

from agents import FunctionTool, function_tool
from agents.tool_context import ToolContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_sync(a: int, b: int) -> int:
    """Return the sum of two ints."""
    return a + b


async def _sample_async(text: str) -> str:
    """Echo the given text after upper-casing."""
    return text.upper()


def _sample_with_context(ctx: ToolContext[str], value: int) -> int:
    return value * 2


# ---------------------------------------------------------------------------
# Decorator wires `.func` to the original callable
# ---------------------------------------------------------------------------


def test_func_attribute_set_by_bare_decorator() -> None:
    """`@function_tool` (no parens) exposes the underlying callable on `.func`."""
    tool = function_tool(_sample_sync)
    assert tool.func is _sample_sync


def test_func_attribute_set_by_decorator_with_options() -> None:
    """`@function_tool(...)` (with parens) also wires `.func` to the original."""
    tool = function_tool(name_override="adder")(_sample_sync)
    assert tool.func is _sample_sync
    # The override should not affect the underlying callable identity.
    assert tool.name == "adder"


def test_func_attribute_set_for_async_function() -> None:
    tool = function_tool(_sample_async)
    assert tool.func is _sample_async


def test_func_attribute_set_for_context_function() -> None:
    tool = function_tool(_sample_with_context)
    assert tool.func is _sample_with_context


def test_func_attribute_invokes_original_directly() -> None:
    """`.func(...)` calls the bare function, bypassing schema and ToolContext."""
    tool = function_tool(_sample_sync)
    assert tool.func is not None  # for type narrowing
    # `tool.func` is typed as the `ToolFunction[...]` union (with-context,
    # with-tool-context, or without). The decorator was applied to a
    # no-context callable, so cast to the matching shape for the direct call.
    direct = cast(Callable[[int, int], int], tool.func)
    assert direct(2, 3) == 5


# ---------------------------------------------------------------------------
# Manual constructor: `.func` defaults to None and stays kw-only
# ---------------------------------------------------------------------------


async def _noop_invoker(ctx: ToolContext[Any], input: str) -> str:
    return ""


def test_manual_construction_defaults_func_to_none() -> None:
    """A `FunctionTool` built manually without `@function_tool` has `func is None`."""
    tool = FunctionTool(
        name="manual",
        description="",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=_noop_invoker,
    )
    assert tool.func is None


def test_func_remains_keyword_only_for_positional_compat() -> None:
    """`.func` is `kw_only` so v0.7.0 positional `FunctionTool(...)` callers
    keep working — this is the same contract documented in AGENTS.md
    ("Public API Positional Compatibility")."""
    tool = FunctionTool(
        "tool_name",
        "tool_description",
        {"type": "object", "properties": {}},
        _noop_invoker,
        True,
        True,
        None,
        None,
    )
    # Underlying callable defaults to None when not supplied.
    assert tool.func is None
    # Sanity-check that the positional call path still binds the way callers expect.
    assert tool.name == "tool_name"
    assert tool.description == "tool_description"


# ---------------------------------------------------------------------------
# `.func` survives the standard FunctionTool clone paths
# ---------------------------------------------------------------------------


def test_func_preserved_by_dataclass_replace() -> None:
    tool = function_tool(_sample_sync)
    clone = dataclasses.replace(tool, name="renamed")
    assert clone.func is _sample_sync
    assert clone.name == "renamed"


def test_func_preserved_by_copy() -> None:
    tool = function_tool(_sample_sync)
    clone = copy.copy(tool)
    assert clone.func is _sample_sync


def test_func_hidden_from_repr() -> None:
    """`repr=False` keeps the (potentially noisy) callable out of `repr(tool)`."""
    tool = function_tool(_sample_sync)
    assert "func=" not in repr(tool)


# ---------------------------------------------------------------------------
# Backward-compat sanity: the closure-walk workaround stays equivalent
# ---------------------------------------------------------------------------


def test_func_matches_closure_walk_workaround() -> None:
    """The new `.func` returns the same callable that today's closure-walking
    workaround finds at `on_invoke_tool._invoke_tool_impl.__closure__`. This
    guards against future refactors silently changing identity."""
    tool = function_tool(_sample_sync)
    invoke_impl = getattr(tool.on_invoke_tool, "_invoke_tool_impl", None)
    if invoke_impl is None or invoke_impl.__closure__ is None:
        pytest.skip("Internal closure layout changed; the workaround is no longer applicable.")

    free_vars = dict(zip(invoke_impl.__code__.co_freevars, invoke_impl.__closure__, strict=False))
    closure_func = free_vars.get("the_func")
    if closure_func is None:
        pytest.skip("`the_func` is no longer captured in the closure.")

    assert tool.func is closure_func.cell_contents
